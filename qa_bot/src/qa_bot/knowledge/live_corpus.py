"""Offline corpus of explicitly selected QA runs; never discovers other profiles."""
import argparse
from collections import Counter,defaultdict
import hashlib
import itertools
import json
from pathlib import Path
import re

MODULES={'System Diagnostic Tool':'diagnostic','SVAR - Spoken English (U.S.)':'svar',
 'Typing':'typing','Personality':'personality','Basic Analytical Ability':'analytical',
 'Basic Computer Literacy Simulation (Windows 10)':'computer','WriteX - Email Writing':'writex','Sales Competency Test':'sales'}
SPEECH={'A':'svar_a','B':'svar_b','C':'svar_c','D':'svar_d'}
LOGS={'personality':'personality','analytical':'analytical','choices':'svar_c','topics':'svar_d',
 'typing':'typing','writex':'writex','sales':'sales','computer':'computer'}

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def number(value):
    match=re.match(r'^\s*(\d+)(?:\s|$)',str(value or ''))
    return match[1] if match else None

def clean_text(text,section=None):
    """Keep numbers, negations and option order; remove only recognized UI."""
    navigation_count={'svar_a':28,'svar_b':28,'svar_c':28,'svar_d':28,'typing':2,'personality':72,'analytical':19,'computer':16,'sales':20}.get(section)
    lines=text.splitlines()
    if lines and lines[0]=='Skip to main content' and 'Exit' in lines:
        lines=lines[lines.index('Exit')+1:]
    # Consecutive 1..N navigation is removable only at a UI boundary.
    for i in range(len(lines)-1,-1,-1):
        if lines[i]!='1':continue
        j=i
        while j<len(lines) and lines[j]==str(j-i+1):j+=1
        preceding=next((x.strip() for x in reversed(lines[:i]) if x.strip()),'')
        if preceding.lower()=='options':continue
        if navigation_count and j-i==navigation_count and (j==len(lines) or lines[j] in ('SUBMIT ANSWER','Assessment Time out')):
            lines=lines[:i]+lines[j:]
    result=[];remaining=False
    for line in lines:
        if line.strip()=='Remaining Time :':remaining=True;continue
        if remaining and not line.strip():continue
        if remaining and re.fullmatch(r'\d{2}:\d{2}',line.strip()):remaining=False;continue
        remaining=False
        if re.fullmatch(r'\d{2}\s*:\s*\d{2} Time Left',line.strip()):continue
        result.append(line.rstrip())
    # Extra simulation countdown precedes its explicit QUESTION heading.
    if 'QUESTION' in result:
        i=result.index('QUESTION')
        if i and all(not x.strip() or re.fullmatch(r'(?:\d{2}:\d{2}:\d{2}|\d+[ms]|::|NaNm|s)',x.strip()) for x in result[:i]):result=result[i:]
    while result and (not result[-1].strip() or result[-1] in ('NEXT','BACK','SKIP','SUBMIT ANSWER','SUBMIT')):result.pop()
    return '\n'.join(result).strip()

def infer(text,previous):
    lines=text.splitlines()
    if 'Assessments' in lines:
        current=None
        for line in lines[lines.index('Assessments')+1:]:
            if line in MODULES:current=MODULES[line]
            if line=='Upcoming' and current:return current,'module_list'
        return previous,'module_list'
    if 'ASSESSMENT DESCRIPTION' in lines:
        for line in lines[lines.index('ASSESSMENT DESCRIPTION')+1:]:
            if line in MODULES:return MODULES[line],'description'
        return previous,'description'
    speech=re.search(r'Section ([ABCD]):',text)
    if speech:return SPEECH[speech[1]],'question'
    if 'Type the given paragraph EXACTLY' in text:return 'typing','question'
    if 'Select an option with which you agree the most.' in text:return 'personality','question'
    if "Choose the 'best' and the 'worst' action" in text:return 'sales','question'
    if re.search(r'QUESTION\n\d+ out of 16\n',text):return 'computer','question'
    if 'Choose the correct option.' in text or 'Refer to the data' in text:return 'analytical','question'
    if 'System Diagnostic Tool' in text and 'START' in text:return 'diagnostic','question'
    if 'Your test is now complete' in text:return previous,'complete_screen'
    return previous,'inferred_question'

def rows(path):
    if not path.exists():return []
    result=[]
    for index,line in enumerate(path.read_text().splitlines(),1):
        try:result.append((index,json.loads(line)))
        except json.JSONDecodeError:result.append((index,{'_invalid_json':True}))
    return result

def collect_run(run):
    run=Path(run).resolve();owned=run/'evidence/owned'
    if not (owned/'states.jsonl').is_file():raise ValueError(f'missing states.jsonl in explicitly selected run {run.name}')
    observations={};phase=None;modules=[];assets=defaultdict(dict);answered=defaultdict(set);malformed=[]
    def add(section,n,kind,content,file,line,**extra):
        signature=digest(content);key=(section,n,kind,signature)
        if key not in observations:
            observations[key]={'source_run':run.name,'section':section or 'unknown','number':n,'kind':kind,
                'signature':signature,'content':content,'evidence':[],**extra}
        observations[key]['evidence'].append({'file':file,'line':line})
    for line,row in rows(owned/'states.jsonl'):
        if row.get('_invalid_json'):malformed.append({'file':'states.jsonl','line':line});continue
        text=row.get('text','');phase,kind=infer(text,phase);n=number(row.get('number'));clean=clean_text(text,phase)
        if kind in ('module_list','description'):
            for title in text.splitlines():
                if title in MODULES and MODULES[title] not in modules:modules.append(MODULES[title])
        for audio in row.get('heard',[]):
            m=re.search(r'Section ([ABCD]):',audio.get('section',''))
            if m and audio.get('sha256'):
                a={'sha256':audio['sha256'],'number':number(audio.get('id')),'order':audio.get('order')}
                assets[SPEECH[m[1]]].setdefault((a['number'],a['sha256']),(a,line))
        if not clean or clean in ('SKIP','NEXT','SUBMIT ANSWER'):kind='ambiguous_transition'
        elif kind.endswith('question'):
            if phase in ('svar_a','svar_b','svar_c','svar_d') and 'In this section,' in clean:kind='instructions'
            elif not n:kind='ambiguous_unbound'
            else:kind='question'
        if 'Assessment Time out' in text:kind='timeout_observation'
        add(phase,n,kind,{'text':clean},'states.jsonl',line,phase_inferred='Section ' not in text)
    for filename,section in LOGS.items():
        for line,row in rows(owned/(filename+'.jsonl')):
            if row.get('_invalid_json'):malformed.append({'file':filename+'.jsonl','line':line});continue
            n=number(row.get('number'))
            # The log is evidence of an action; it is not an official grade.
            answered[section].add(n)
            evidence={k:row[k] for k in ('advanced','exact_match','practice','source','confidence','correctness_verified','official_answer_key') if k in row}
            add(section,n,'answer_evidence',evidence,filename+'.jsonl',line)
            prompt=row.get('question') or row.get('prompt')
            if isinstance(prompt,str):add(section,n,'logged_question',{'text':clean_text(prompt,section)},filename+'.jsonl',line)
    for line,row in rows(owned/'actions.jsonl'):
        if row.get('action')=='submit_speech':
            n=number(row.get('number'))
            matches={r['section'] for r in observations.values() if r['number']==n and r['section'] in SPEECH.values()}
            if len(matches)==1:answered[matches.pop()].add(n)
    for section,items in assets.items():
        for a,line in sorted(items.values(),key=lambda x:(x[0]['order'] or 0,x[0]['sha256'])):
            add(section,a['number'],'audio_prompt',a,'states.jsonl',line)
    for line,row in rows(owned/'transcripts.jsonl'):
        m=re.search(r'Section ([ABCD]):',row.get('section',''))
        if m and row.get('transcript'):
            add(SPEECH[m[1]],number(row.get('id')),'transcript',{'text':row['transcript'],'audio_sha256':row.get('sha256'),'order':row.get('order')},'transcripts.jsonl',line)
    result=list(observations.values())
    for item in result:item['answer_observed']=item['number'] in answered[item['section']]
    return result,{'source_run':run.name,'module_order':modules,'malformed_lines':malformed}

def summarize(observations,metadata):
    sections={};overlaps=[]
    for run in metadata:
        name=run['source_run'];per={}
        available=[o for o in observations if o['source_run']==name]
        for section in sorted({o['section'] for o in available}):
            records=[o for o in available if o['section']==section]
            bynumber=defaultdict(list)
            for r in records:
                if r['kind'] in ('question','logged_question','audio_prompt') and r['number']:bynumber[r['number']].append(r)
            ordered=[]
            for n,items in sorted(bynumber.items(),key=lambda x:int(x[0])):
                # Prefer visible states; use logged prompt only if none was saved.
                state=[i for i in items if i['kind']=='question'];chosen=state or [i for i in items if i['kind']=='logged_question']
                chosen+= [i for i in items if i['kind']=='audio_prompt']
                ordered.append({'number':n,'signatures':sorted({i['signature'] for i in chosen}),'answer_observed':any(i['answer_observed'] for i in items)})
            per[section]={'question_numbers_observed':len(ordered),'question_numbers_with_answer_evidence':sum(i['answer_observed'] for i in ordered),
                'ambiguous_observations':sum(r['kind'].startswith('ambiguous') for r in records),'ordered_questions':ordered,
                'observed_variant_sha256':digest([{'number':i['number'],'signatures':i['signatures']} for i in ordered]),
                'complete_variant_proven':False}
        sections[name]=per
    for left,right in itertools.combinations(sections,2):
        for section in sorted(set(sections[left])&set(sections[right])):
            a={s for q in sections[left][section]['ordered_questions'] for s in q['signatures']};b={s for q in sections[right][section]['ordered_questions'] for s in q['signatures']}
            overlaps.append({'left':left,'right':right,'section':section,'shared_exact_signatures':len(a&b),
                'left_signatures':len(a),'right_signatures':len(b),'same_observed_order':sections[left][section]['observed_variant_sha256']==sections[right][section]['observed_variant_sha256']})
    return {'schema_version':1,'runs':metadata,'sections':sections,'overlaps':overlaps,
        'observation_count':len(observations),'kind_counts':dict(Counter(o['kind'] for o in observations)),
        'limits':['Observed subsets do not prove complete test variants.','An answer action or advancement does not prove correctness.','Image-dependent text alone does not establish equivalent questions.']}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,action='append',required=True,help='Explicit authorized run directory; repeat for each run')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    project=Path(__file__).resolve().parents[3];runs=(project/'runs').resolve();output=args.output.resolve()
    if not output.is_relative_to(runs):parser.error('output must remain under ignored qa_bot/runs')
    if len({r.resolve() for r in args.run})!=len(args.run):parser.error('duplicate run directory')
    if len({r.resolve().name for r in args.run})!=len(args.run):parser.error('run directory names must be distinct')
    observations=[];metadata=[]
    for run in args.run:
        records,meta=collect_run(run);observations.extend(records);metadata.append(meta)
    summary=summarize(observations,metadata);output.mkdir(parents=True,exist_ok=True)
    (output/'observations.jsonl').write_text(''.join(json.dumps(o,ensure_ascii=False)+'\n' for o in observations))
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'output':str(output),'runs':[m['source_run'] for m in metadata],'observations':len(observations),'kind_counts':summary['kind_counts']}))

if __name__=='__main__':main()
