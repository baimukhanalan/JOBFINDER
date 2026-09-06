import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from qa_bot.parallel_supervisor import ProfileLock, make_plan, write_json
from qa_bot.preflight_inventory import (browser_command, classify, main, make_inventory_plan,
    observe_one, run_inventory, validate_inventory_plan, run_worker, source_error_reason)
from qa_bot.jobfinder_source import InvitationSelectionError

TERMS='TERMS & CONDITIONS\nNo\nI agree to Terms and Conditions\nCANCEL\nCONTINUE'
MENU='Assessments\nSystem Diagnostic Tool\n1 Task in 5 Minutes\nUpcoming\nSVAR - Spoken English (U.S.)\n4 Sections\nLater\nNEXT'
RESUME='To resume your assessment, press CONTINUE.'
DONE='This assessment has either been completed or submitted. No further action is needed. Message code TC100.'
FAKE = r'''
import json,os,pathlib,sys,time
root=pathlib.Path(sys.argv[1]);state=json.loads(sys.argv[2]);unsafe=sys.argv[3]=='True'
evidence=root/'evidence'/'owned';evidence.mkdir(parents=True)
evidence.joinpath('session-metadata.jsonl').write_text(json.dumps(dict(preflight=True,auto=False))+'\n')
root.joinpath('lifetime.json').write_text(json.dumps(dict(start=time.time(),pid=os.getpid())))
print(json.dumps(dict(ready=True)),flush=True)
for line in sys.stdin:
 c=json.loads(line)
 with evidence.joinpath('operator-commands.jsonl').open('a') as f:f.write(json.dumps(c)+'\n')
 if c['action']=='state':
  if unsafe:evidence.joinpath('actions.jsonl').write_text(json.dumps(dict(action='submit_speech',number='2'))+'\n')
  print(json.dumps(dict(result=state)),flush=True)
 elif c['action']=='close':
  data=json.loads(root.joinpath('lifetime.json').read_text());data['end']=time.time();root.joinpath('lifetime.json').write_text(json.dumps(data));break
 else:raise RuntimeError('unexpected mutation command')
'''


class PreflightInventoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.scope=self.root/'scope.json'
        self.scope.write_text(json.dumps({'profiles':[{'original_row':i,'name':f'QA Person {i}'} for i in range(1,31)]}))
        self.worker=self.root/'worker.py';self.worker.write_text(FAKE)
        self.plan=make_inventory_plan(self.scope,[4,5,6,10,11,12,13,14,15,16],self.root/'inventory')

    def tearDown(self):self.temp.cleanup()

    def factory(self,states=None,unsafe=False):
        states=states or {}
        return lambda spec,plan:[sys.executable,str(self.worker),spec['directory'],json.dumps(states.get(spec['row'],{'text':TERMS,'number':None})),str(unsafe)]

    def test_scope_validation_and_max_two_are_strict(self):
        validate_inventory_plan(self.plan)
        for rows,concurrency in (([31],2),([4,4],2),([4],3),([],1)):
            with self.assertRaises(ValueError):make_inventory_plan(self.scope,rows,self.root/'bad',concurrency=concurrency)
        self.plan['profiles'][0]['name']='Other profile'
        with self.assertRaises(ValueError):validate_inventory_plan(self.plan)

    def test_only_terms_or_completely_unstarted_menu_are_fresh(self):
        self.assertEqual(classify({'text':TERMS,'number':None}),'fresh_terms')
        self.assertEqual(classify({'text':MENU,'number':None}),'fresh_menu')
        self.assertEqual(classify({'text':MENU.replace('Upcoming','Complete'),'number':None}),'in_progress')
        self.assertEqual(classify({'text':'Section A: Read and Speak','number':'1'}),'in_progress')
        self.assertEqual(classify({'text':'ASSESSMENT DESCRIPTION\nTyping','number':None}),'in_progress')

    def test_completed_resume_notice_and_unknown_remain_separate(self):
        self.assertEqual(classify({'text':DONE}),'completed_tc100')
        self.assertEqual(classify({'text':RESUME}),'resume_required')
        self.assertEqual(classify({'text':'I confirm I have read and understood this Notice.'}),'notice_required')
        self.assertEqual(classify({'text':'TC100 but no completion evidence'}),'unknown')
        self.assertEqual(classify({'text':'Your test is now complete. Thank you!\nWarning!\nSUBMIT'}),'confirmation_required')

    def test_child_command_is_preflight_only_without_service_or_resume_flags(self):
        args=browser_command(self.plan['profiles'][0],self.plan)
        self.assertIn('--preflight',args)
        self.assertNotIn('--auto',args)
        self.assertEqual(args[args.index('--selected-profile')+1],'QA Person 4')
        self.assertNotIn('CONTINUE',args)
        self.assertIn('qa_bot.preflight_inventory',args)
        self.assertIn('worker',args)

    def test_source_error_parser_keeps_only_exact_machine_reasons(self):
        data={'source_error':{'phase':'invitation_selection','reason':'no_tp_invitation'}}
        self.assertEqual(source_error_reason([data]),'no_tp_invitation')
        self.assertEqual(source_error_reason([], 'qa_bot.jobfinder_source.InvitationSelectionError: ambiguous_tp_invitation\n'),'ambiguous_tp_invitation')
        self.assertIsNone(source_error_reason([], 'ValueError: one TP invitation required\n'))
        self.assertIsNone(source_error_reason([{'source_error':{'phase':'invitation_selection','reason':['secret']}}]))

    def test_worker_wraps_source_errors_without_changing_live_session_or_exposing_secrets(self):
        original=list(sys.argv)
        with patch('qa_bot.live_session.main',side_effect=InvitationSelectionError('no_tp_invitation')),patch('builtins.print') as output:
            self.assertEqual(run_worker(['--','--preflight']),2)
        self.assertEqual(json.loads(output.call_args.args[0]),{'source_error':{'phase':'invitation_selection','reason':'no_tp_invitation'}})
        self.assertEqual(sys.argv,original)
        for args in (['--auto'],['--preflight','--auto=True'],['--preflight','--source-browser=https://host.invalid']):
            with self.assertRaises(ValueError):run_worker(args)

    async def test_missing_invitation_survives_child_exit_as_machine_report_reason(self):
        reason={'source_error':{'phase':'invitation_selection','reason':'no_tp_invitation'}}
        def factory(spec,plan):
            return [sys.executable,'-c','import json,sys;print(json.dumps('+repr(reason)+'),flush=True);sys.exit(2)']
        result=await observe_one(self.plan['profiles'][0],self.plan,command_factory=factory,sample_interval=.08)
        self.assertEqual(result['classification'],'unavailable')
        self.assertEqual(result['reason'],'no_tp_invitation')
        self.assertEqual(result['error_phase'],'invitation_selection')
        self.assertFalse(result['fresh_candidate'])
        self.assertTrue(result['worker_closed'])

    async def test_ten_fake_visits_are_bounded_to_two_and_close_their_workers(self):
        states={5:{'text':DONE},6:{'text':RESUME},10:{'text':'Question in progress','number':'8'},11:{'text':MENU}}
        with patch('builtins.print'):
            report=await run_inventory(self.plan,command_factory=self.factory(states),sample_interval=.08,check_processes=False)
        self.assertEqual(len(report['profiles']),10)
        self.assertEqual({r['row']:r['classification'] for r in report['profiles']}[5],'completed_tc100')
        self.assertNotIn(5,report['fresh_rows']);self.assertNotIn(6,report['fresh_rows']);self.assertNotIn(10,report['fresh_rows'])
        events=[]
        for spec in self.plan['profiles']:
            root=Path(spec['directory']);lifetime=json.loads((root/'lifetime.json').read_text())
            events.extend([(lifetime['start'],1),(lifetime['end'],-1)])
            commands=[json.loads(l)['action'] for l in (root/'evidence/owned/operator-commands.jsonl').read_text().splitlines()]
            self.assertEqual(set(commands),{'state','close'})
            with self.assertRaises(ProcessLookupError):os.kill(lifetime['pid'],0)
        active=peak=0
        for _time,event in sorted(events):active+=event;peak=max(peak,active)
        self.assertEqual((peak,active),(2,0))
        self.assertTrue(all(r['worker_closed'] and r['recorded_actions_read_only'] for r in report['profiles']))

    async def test_active_profile_lock_prevents_its_visit(self):
        spec=self.plan['profiles'][0]
        lock=ProfileLock(self.scope.parent/'.parallel-locks',spec['profile_id'])
        try:
            with patch('builtins.print'):
                report=await run_inventory(self.plan,command_factory=self.factory(),sample_interval=.08,check_processes=False)
            row=next(r for r in report['profiles'] if r['row']==4)
            self.assertEqual(row['classification'],'blocked_active_profile')
            self.assertFalse(Path(spec['directory']).exists())
        finally:lock.close()

    async def test_unexpected_answer_record_prevents_fresh_result(self):
        result=await observe_one(self.plan['profiles'][0],self.plan,command_factory=self.factory(unsafe=True),sample_interval=.08)
        self.assertEqual(result['classification'],'unsafe_preflight_action_observed')
        self.assertFalse(result['fresh_candidate'])
        self.assertTrue(result['worker_closed'])

    async def test_report_excludes_invitation_urls_and_screen_content(self):
        secret='https://assessment.invalid/start?token=do-not-report'
        result=await observe_one(self.plan['profiles'][0],self.plan,command_factory=self.factory({4:{'text':RESUME+'\n'+secret}}),sample_interval=.08)
        self.assertEqual(result['classification'],'resume_required')
        self.assertNotIn(secret,json.dumps(result))
        self.assertNotIn('token=',json.dumps(result))

    async def test_cancellation_closes_child_before_releasing_inventory(self):
        task=asyncio.create_task(observe_one(self.plan['profiles'][0],self.plan,command_factory=self.factory({4:{'text':'Loading'}}),sample_interval=.08))
        path=Path(self.plan['profiles'][0]['directory'])/'lifetime.json'
        for _ in range(50):
            if path.exists():break
            await asyncio.sleep(.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        record=json.loads(path.read_text())
        self.assertIn('end',record)
        with self.assertRaises(ProcessLookupError):os.kill(record['pid'],0)

    def test_offline_plan_can_derive_exact_rows_from_reviewed_manifest_without_launching(self):
        manifest=self.root/'source-manifest.json'
        write_json(manifest,make_plan(self.scope,self.root/'unused',[4,5,6,10,11,12,13,14,15,16]))
        output=self.root/'from-manifest'
        with patch('builtins.print'),patch('qa_bot.preflight_inventory.run_inventory') as runner:
            main(['plan','--manifest',str(manifest),'--output',str(output)])
        plan=json.loads((output/'preflight-plan.json').read_text())
        self.assertEqual([r['row'] for r in plan['profiles']],[4,5,6,10,11,12,13,14,15,16])
        self.assertEqual(plan['concurrency'],2)
        runner.assert_not_called()


if __name__=='__main__':unittest.main()
