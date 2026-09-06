"""Inspect initial states from a fixed local authorization list, without answers."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from playwright.async_api import async_playwright
from qa_bot.jobfinder_source import selected_invitation
from qa_bot.live_session import Session,STATE_SCRIPT,safe_text

async def inspect(args):
    scope=json.loads(args.scope.read_text())['profiles']
    by_row={p['original_row']:p['name'] for p in scope}
    rows=[int(x) for x in args.rows.split(',')]
    if len(by_row)!=30 or any(row not in by_row for row in rows):raise ValueError('fixed thirty-profile scope required')
    args.output.mkdir(parents=True,exist_ok=True)
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(channel='chrome',headless=False)
        try:
            for row in rows:
                profile=by_row[row];context=None
                report={'original_row':row,'profile':profile,'answers_submitted':0,'speech_bridge':False}
                try:
                    url=await asyncio.to_thread(selected_invitation,profile,os.environ.get('JOBFINDER_LOGIN',''),os.environ.get('JOBFINDER_PASSWORD',''))
                    context=await browser.new_context();page=await context.new_page()
                    page.on('dialog',lambda d:asyncio.create_task(d.accept() if d.type=='beforeunload' else d.dismiss()))
                    session=Session(page,args.output/f'row-{row}');session.preflight=True;session.cdp=await context.new_cdp_session(page)
                    await page.goto(url)
                    end=time.monotonic()+45
                    while time.monotonic()<end:
                        state=await page.evaluate(STATE_SCRIPT);text=state['text']
                        if 'To resume your assessment' in text:
                            try:await session.click('button','CONTINUE')
                            except ValueError:pass
                        elif 'I confirm I have read and understood this Notice.' in text:
                            try:await session.command({'action':'notice'})
                            except ValueError:pass
                        elif state.get('number') or any(x in text for x in ('Assessments\n','ASSESSMENT DESCRIPTION','Your test is now complete','Terms and Conditions','Terms & Conditions','I agree')):
                            report.update(number=state.get('number'),screen=text[:1800],status='observed')
                            report['fresh_candidate']=(state.get('number')=='1' and 'Section A: Read and Speak' in text) or ('Assessments\n' in text and 'Complete' not in text) or any(x in text for x in ('Terms and Conditions','Terms & Conditions','I agree'))
                            await page.screenshot(path=str(session.output/'initial.png'))
                            break
                        await asyncio.sleep(.25)
                    else:report.update(status='unknown',screen=(await page.evaluate(STATE_SCRIPT))['text'][:1800])
                except Exception as error:report.update(status='unavailable',reason=safe_text(error)[:250])
                finally:
                    if context:await context.close()
                with (args.output/'results.jsonl').open('a') as f:f.write(json.dumps(report)+'\n')
                print(json.dumps(report),flush=True)
                if report.get('fresh_candidate'):break
        finally:await browser.close()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--scope',type=Path,required=True);p.add_argument('--rows',required=True);p.add_argument('--output',type=Path,required=True)
    asyncio.run(inspect(p.parse_args()))
if __name__=='__main__':main()
