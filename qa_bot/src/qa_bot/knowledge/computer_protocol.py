"""Observed TP simulation task-result contract, not an overall assessment grade.

Validated against authorized row26 questions 1 and 2: score=1 coincided with
opening Notepad and maximizing it (before/after screenshots), then advancement.
Only the exact typed message shape and fully bound action chronology qualify.
"""
import json
import re

CONTRACT='tp-windows-task-score-v1'
ORIGIN='https://amcatglobal.aspiringminds.com'
PATH='/assets/msOfficeSimulation/run.html'
SAFE=re.compile(r'^[A-Za-z0-9 _.,:-]{1,100}$')


def outcome(question,steps,events,advanced_at_ms):
    result={'verification':'unverified','contract':CONTRACT,'reason':'missing_action_evidence','event':None}
    if not steps or not str(question).isdigit() or not 1<=int(question)<=16:return result
    if type(advanced_at_ms) not in (int,float):return dict(result,reason='missing_advance_time')
    for _,canonical,action in steps:
        state=json.loads(canonical)
        if state.get('frame_origin')!=ORIGIN or state.get('frame_path')!=PATH:return dict(result,reason='unverified_frame_protocol')
        if str(action.get('question_id'))!=str(question):return dict(result,reason='action_question_mismatch')
        start=action.get('action_started_ms');finish=action.get('action_finished_ms');boundary=action.get('event_sequence_before_action')
        if type(start) not in (int,float) or type(finish) not in (int,float) or type(boundary) is not int:return dict(result,reason='missing_action_timing')
        if not start<=finish<=advanced_at_ms:return dict(result,reason='invalid_action_timing')
    candidates=[]
    for event in events:
        if event.get('question')!=str(question):continue
        message=event.get('fields',{}).get('message')
        if not isinstance(message,dict) or set(message)!= {'score','log'}:continue
        score=message['score'];log=message['log']
        if type(score) not in (int,float) or not isinstance(log,list) or not all(isinstance(x,str) and SAFE.fullmatch(x) for x in log):continue
        stamp=event.get('time');sequence=event.get('sequence')
        if type(stamp) not in (int,float) or type(sequence) is not int:continue
        if not steps[0][2]['action_started_ms']<=stamp<=advanced_at_ms:continue
        candidates.append(event)
    if not candidates:return dict(result,reason='missing_typed_result')
    if any(e['fields']['message']['score']!=1 for e in candidates):
        return dict(result,verification='reported_failure',reason='provider_score_not_one',event=candidates[-1])
    last=steps[-1][2]
    qualifying=[e for e in candidates if e['time']>=last['action_started_ms'] and e['sequence']>last['event_sequence_before_action']]
    if len(qualifying)!=1:return dict(result,reason='result_not_uniquely_after_last_action')
    return dict(result,verification='verified_success',reason='provider_task_score_one_after_actions',event=qualifying[0])
