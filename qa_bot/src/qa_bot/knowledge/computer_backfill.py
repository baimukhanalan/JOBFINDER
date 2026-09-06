"""Audit and optionally verify one explicit run's recorded simulation outcomes."""
import argparse
import hashlib
import json
from pathlib import Path
from qa_bot.knowledge.computer_cache import ComputerActionCache,ACTION_FIELDS
from qa_bot.knowledge.computer_protocol import CONTRACT,ORIGIN,PATH,SAFE


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def audit_question(cache,run,source_test,question,*,apply=False):
    """Legacy causal evidence: no invented or inferred per-action timestamps."""
    run=Path(run).resolve();owned=run/'evidence/owned';question=str(question)
    result={'question':question,'verification':'unverified','evidence_basis':'runtime_question_outcome_no_per_action_clock'}
    def stop(reason):return dict(result,reason=reason)
    try:
        actions=[r for r in read_rows(owned/'computer-actions.jsonl') if str(r.get('question'))==question]
        observed=read_rows(owned/'computer-observations.jsonl')
        advanced=[r for r in read_rows(owned/'computer.jsonl') if str(r.get('number'))==question and r.get('advanced') is True]
        browser=read_rows(run/'browser.log')
    except (OSError,ValueError):return stop('incomplete_run_logs')
    if len(advanced)!=1 or not actions:return stop('one_recorded_advance_and_actions_required')
    keys=[a.get('key') for a in actions]
    if None in keys or len(set(keys))!=len(keys):return stop('ambiguous_repeated_action_keys')
    step_rows=[(i,r) for i,r in enumerate(browser) if 'computer_step' in r]
    matching=[(i,r) for i,r in step_rows if str(r['computer_step'])==question]
    if len(matching)!=len(actions) or [r['action'] for _,r in matching]!=[r['action'] for r in actions]:return stop('browser_action_order_mismatch')
    if any(str(r['computer_step'])!=question for i,r in step_rows if matching[0][0]<=i<=matching[-1][0]):return stop('interleaved_question_execution')
    after=[r['state'].get('number') for r in browser[matching[-1][0]+1:] if isinstance(r.get('state'),dict)]
    if not any(n!=question for n in after):return stop('browser_advance_not_recorded')
    evidence=[]
    for action in actions:
        candidates=[o for o in observed if o.get('key')==action['key'] and str(o.get('question'))==question and o.get('screenshot')==action.get('screenshot')]
        if len(candidates)!=1:return stop('one_bound_snapshot_required')
        observation=candidates[0];state=observation['state']
        canonical=json.dumps(state,sort_keys=True,separators=(',',':'),ensure_ascii=False)
        if hashlib.sha256(canonical.encode()).hexdigest()!=action['key']:return stop('snapshot_key_mismatch')
        if state.get('frame_origin')!=ORIGIN or state.get('frame_path')!=PATH:return stop('unverified_frame_protocol')
        image=(owned/action['screenshot']).resolve()
        if not image.is_relative_to(owned.resolve()) or not image.is_file():return stop('missing_bound_screenshot')
        if hashlib.sha256(image.read_bytes()).hexdigest()!=state['screenshot_sha256']:return stop('screenshot_changed')
        if action.get('original_test')!=source_test:return stop('action_source_mismatch')
        evidence.append((action,canonical))
    with cache.db:
        cache.db.execute('BEGIN IMMEDIATE')
        records=cache.db.execute('SELECT advanced,events FROM question_outcomes WHERE source_test=? AND question=?', (source_test,question)).fetchall()
        # Exact old-runtime format is a raw list, emitted only at advancement.
        outcomes=[]
        for advanced_flag,payload in records:
            value=json.loads(payload)
            if advanced_flag==1 and isinstance(value,list) and value not in outcomes:outcomes.append(value)
        failures=[event for group in outcomes for event in group if event.get('question')==question
            and isinstance(event.get('fields',{}).get('message'),dict)
            and type(event['fields']['message'].get('score')) in (int,float)
            and event['fields']['message']['score']!=1]
        if failures:
            if apply:
                for key in keys:
                    cache.db.execute("UPDATE actions SET conflict=CASE WHEN verification='verified_success' THEN 1 ELSE conflict END,verification='reported_failure' WHERE key=? AND source_test=?",(key,source_test))
            return stop('provider_reported_failure')
        if len(outcomes)!=1:return stop('one_runtime_causal_outcome_required')
        events=outcomes[0];scores=[]
        for event in events:
            if event.get('question')!=question:return stop('outcome_question_mismatch')
            fields=event.get('fields',{});message=fields.get('message')
            if message is None:continue
            if not isinstance(message,dict) or set(message)!={'score','log'}:return stop('unknown_result_protocol')
            if type(message['score']) not in (int,float) or not isinstance(message['log'],list) or not all(isinstance(x,str) and SAFE.fullmatch(x) for x in message['log']):return stop('invalid_result_types')
            scores.append(event)
        if len(scores)!=1 or scores[0]['fields']['message']['score']!=1:return stop('one_success_and_no_failed_result_required')
        event=scores[0]
        if type(event.get('time')) not in (int,float) or event['time']>advanced[0]['time']*1000:return stop('result_after_advance')
        # The cache was populated only from the runtime's executed pending list.
        bound_keys=set()
        for action,canonical in evidence:
            row=cache.db.execute('SELECT canonical,action,source_test,conflict FROM actions WHERE key=?',(action['key'],)).fetchone()
            expected=json.dumps({k:action.get(k) for k in ACTION_FIELDS},sort_keys=True,separators=(',',':'))
            if not row or row!=(canonical,expected,source_test,0):return stop('executed_cache_binding_missing_or_conflicting')
            qs=cache.db.execute('SELECT DISTINCT question FROM observations WHERE source_test=? AND key=?',(source_test,action['key'])).fetchall()
            if qs!=[(question,)]:return stop('ambiguous_cache_question_binding')
            bound_keys.add(action['key'])
        actual={r[0] for r in cache.db.execute('SELECT DISTINCT a.key FROM actions a JOIN observations o ON a.key=o.key WHERE a.source_test=? AND o.source_test=? AND o.question=?',(source_test,source_test,question))}
        if actual!=bound_keys:return stop('incomplete_pending_action_sequence')
        proof={'contract':CONTRACT,'source_test':source_test,'question':question,'evidence_basis':result['evidence_basis'],
            'keys':keys,'event':event,'advanced_record':advanced[0],
            'browser_step_lines':[i+1 for i,_ in matching],
            'files':{name:hashlib.sha256((owned/name).read_bytes()).hexdigest() for name in ('computer-actions.jsonl','computer-observations.jsonl','computer.jsonl','computer-events.jsonl')}}
        # Explicitly verify that the captured event also exists in its raw log.
        if event not in read_rows(owned/'computer-events.jsonl'):return stop('provider_event_missing_from_log')
        if apply:
            cache.db.execute('CREATE TABLE IF NOT EXISTS cache_verifications(id TEXT PRIMARY KEY,proof TEXT NOT NULL)')
            payload=json.dumps(proof,sort_keys=True,separators=(',',':'))
            cache.db.execute('INSERT OR IGNORE INTO cache_verifications VALUES(?,?)',(hashlib.sha256(payload.encode()).hexdigest(),payload))
            for key in keys:cache.db.execute("UPDATE actions SET verification='verified_success' WHERE key=?",(key,))
        return dict(result,verification='verified_success',applied=apply,verified_actions=len(keys),proof=proof)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True);parser.add_argument('--source-test',required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args(argv);project=Path(__file__).resolve().parents[3];runs=(project/'runs').resolve()
    if not args.output.resolve().is_relative_to(runs):parser.error('output must remain under ignored runs')
    cache=ComputerActionCache(runs/'computer-actions.sqlite3')
    try:
        questions=sorted({str(r['number']) for r in read_rows(args.run/'evidence/owned/computer.jsonl')},key=int)
        report=[audit_question(cache,args.run,args.source_test,q,apply=args.apply) for q in questions]
    finally:cache.close()
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps([{'question':r['question'],'verification':r['verification'],'reason':r.get('reason'),'verified_actions':r.get('verified_actions',0)} for r in report]))

if __name__=='__main__':main()
