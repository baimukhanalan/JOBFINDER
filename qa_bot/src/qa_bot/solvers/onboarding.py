"""Observed onboarding controls; terms require an explicit caller command."""
import json
import time
from qa_bot.live_session import STATE_SCRIPT

async def accept_terms(session):
    # This command is explicit only; automatic navigation never accepts terms.
    if session.preflight:raise ValueError('preflight cannot accept terms')
    state=await session.page.evaluate(STATE_SCRIPT)
    if 'TERMS & CONDITIONS' not in state['text'] or 'I agree to Terms and Conditions' not in state['text']:
        raise ValueError('expected reviewed terms screen')
    boxes=await session.controls('checkbox')
    if len(boxes)!=1:raise ValueError('one terms checkbox required')
    checked=any(p['name']=='checked' and p['value'].get('value') in (True,'true') for p in boxes[0].get('properties',[]))
    if not checked:await session.click('checkbox','No')
    boxes=await session.controls('checkbox')
    if len(boxes)!=1 or not any(p['name']=='checked' and p['value'].get('value') in (True,'true') for p in boxes[0].get('properties',[])):
        raise ValueError('terms selection did not apply')
    session.log('navigation.jsonl',{'action':'explicit_terms_acceptance','profile':session.profile_id,'test':session.test_id,'time':time.time()})
    await session.click('button','CONTINUE')
    print(json.dumps({'terms_accepted':True}),flush=True)
    return

async def run_diagnostic(session):
    if session.preflight:raise ValueError('preflight cannot start diagnostic')
    state=await session.page.evaluate(STATE_SCRIPT)
    if 'System Diagnostic Tool.' not in state['text']:raise ValueError('diagnostic screen required')
    start=session.page.locator('.system-diag .init-button').filter(has_text='START')
    if await start.count()==1:await start.click()
    submit=session.page.locator('#submitBtn a[role="button"]:not(.disabled)')
    await submit.wait_for(state='visible',timeout=180000)
    state=await session.page.evaluate(STATE_SCRIPT)
    if 'System Diagnostic Tool.' not in state['text'] or 'Assessment Time out' in state['text']:
        raise ValueError('diagnostic state changed')
    rows=await session.page.locator('.diag-table tr').evaluate_all('(rows)=>rows.map(r=>({name:r.querySelector(".title")?.innerText,value:r.querySelector(".value")?.innerText,indicatorClasses:[...r.querySelectorAll(".result *")].map(n=>n.className)}))')
    session.log('diagnostic.jsonl',{'time':time.time(),'checks':rows})
    await session.click('button','SUBMIT')
    print(json.dumps({'diagnostic_submitted':True}),flush=True)
