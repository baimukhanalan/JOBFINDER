import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from qa_bot.parallel_supervisor import (Attempt, ProfileLock, commands, conflicting_processes,
    make_plan, ports_available, supervise, validate_plan, write_json, main)


FAKE_WORKER = r'''
import json, os, pathlib, sys, time
root=pathlib.Path(sys.argv[1]); mode=sys.argv[2]
evidence=root/'evidence'/'owned';evidence.mkdir(parents=True)
current_text='';current_number=None
def state(text, number=None):
 global current_text,current_number
 current_text,current_number=text,number
 with (evidence/'states.jsonl').open('a') as f:f.write(json.dumps(dict(text=text,number=number,time=time.time()))+'\n')
root.joinpath('worker.json').write_text(json.dumps(dict(pid=os.getpid(),started=time.time(),token_length=len(os.environ.get('QA_LOCAL_BRIDGE_TOKEN','')),ports=[os.environ.get(n) for n in ('QA_SPEECH_PORT','QA_SCRIPT_PORT','QA_PROMPT_CAPTURE_PORT','QA_RECORDING_CAPTURE_PORT')])))
if mode in ('complete','concurrent','delayed_modal','partial_final'):
 state('Assessments\nTyping\n1 QUESTION\nComplete\nNEXT')
 if mode!='partial_final':evidence.joinpath('typing.jsonl').write_text(json.dumps(dict(number='2',practice=False,exact_match=True))+'\n')
if mode=='complete':
 state('Question',1);time.sleep(.15);state('Your test is now complete. Thank you!')
elif mode=='delayed_modal':
 state('Your test is now complete. Thank you!');time.sleep(.12)
 state('Your test is now complete. Thank you!\nWarning!\nAre you ready to submit this assessment?\nSUBMIT\nCANCEL')
elif mode=='partial_final':state('Your test is now complete. Thank you!')
elif mode=='concurrent':
 state('Question',1);deadline=time.monotonic()+3
 while len(list(root.parent.glob('row-*/worker.json')))<10 and time.monotonic()<deadline:time.sleep(.01)
 root.joinpath('simultaneous.json').write_text(json.dumps(len(list(root.parent.glob('row-*/worker.json')))))
 state('Your test is now complete. Thank you!')
elif mode=='timeout':state('Assessment Time out')
elif mode=='error':
 state('Question',2)
 evidence.joinpath('computer-errors.jsonl').write_text(json.dumps(dict(error='needs review'))+'\n')
elif mode=='terms':state('TERMS & CONDITIONS')
elif mode=='exit':sys.exit(7)
for line in sys.stdin:
 command=json.loads(line)
 with (root/'received.jsonl').open('a') as f:f.write(json.dumps(command)+'\n')
 if command==dict(action='state'):print(json.dumps(dict(result=dict(text=current_text,number=current_number))),flush=True)
 if command==dict(action='auto_modules',module='accept_terms'):state('Question',1)
'''


class ParallelSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scope = self.root / 'scope.json'
        self.scope.write_text(json.dumps({'profiles': [{'original_row': i, 'name': f'QA Person {i}'} for i in range(1,31)]}))
        self.worker = self.root / 'fake_worker.py'
        self.worker.write_text(FAKE_WORKER)
        self.plan = make_plan(self.scope, self.root / 'batch', [1,2,3], concurrency=2)
        self.open_attempts = []

    def tearDown(self):
        for attempt in self.open_attempts:
            attempt.close()
        self.temp.cleanup()

    def factory(self, mode='complete'):
        return lambda spec, _plan: [('browser', [sys.executable, str(self.worker), spec['directory'], mode])]

    def attempt(self, mode):
        result = Attempt(self.plan['attempts'][0], self.plan, self.factory(mode), require_ports=False)
        self.open_attempts.append(result)
        deadline = time.monotonic() + 3
        while not (result.path/'worker.json').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        return result

    def poll_until(self, attempt, state):
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            if attempt.poll()==state:
                return
            time.sleep(.01)
        self.fail(f'expected {state}, got {attempt.state}')

    def test_plan_selects_original_identity_and_ten_isolated_port_sets(self):
        plan = make_plan(self.scope,self.root/'ten',list(range(1,11)), concurrency=10)
        validate_plan(plan)
        self.assertEqual(len({p for a in plan['attempts'] for p in a['ports'].values()}),40)
        self.assertEqual(plan['attempts'][9]['name'],'QA Person 10')
        self.assertTrue(plan['replay_only'])
        self.assertNotIn('token',json.dumps(plan))
        self.assertFalse((self.root/'ten').exists())

    def test_outside_scope_duplicates_and_excess_concurrency_rejected(self):
        for rows,concurrency in (([31],1),([1,1],1),([1],11),([],1)):
            with self.assertRaises(ValueError):
                make_plan(self.scope,self.root/'invalid',rows,concurrency=concurrency)

    def test_changed_scope_or_tampered_manifest_never_launches(self):
        self.plan['attempts'][0]['name']='Not authorized'
        with self.assertRaises(ValueError):validate_plan(self.plan)
        self.plan['attempts'][0]['name']='QA Person 1'
        self.scope.write_text(self.scope.read_text()+'\n')
        with self.assertRaises(ValueError):validate_plan(self.plan)

    def test_profile_lock_prevents_duplicate_batches(self):
        lock=ProfileLock(self.root/'locks','authorized-row-1')
        try:
            with self.assertRaises(ValueError):ProfileLock(self.root/'locks','authorized-row-1')
        finally:lock.close()
        ProfileLock(self.root/'locks','authorized-row-1').close()

    def test_duplicate_owner_cannot_overwrite_running_batch_status(self):
        output=Path(self.plan['output']);output.mkdir()
        status=output/'status.json';write_json(status,{'state':'running','supervisor_pid':123})
        original=status.read_bytes()
        owner=ProfileLock(output,'supervisor-owner')
        try:
            with self.assertRaises(ValueError):
                supervise(self.plan,command_factory=self.factory(),require_ports=False)
            self.assertEqual(status.read_bytes(),original)
        finally:owner.close()

    def test_existing_interactive_worker_is_detected(self):
        rows=self.plan['attempts']
        self.assertTrue(conflicting_processes(rows,'python -m qa_bot.live_session --selected-profile QA Person 1 --auto'))
        self.assertTrue(conflicting_processes(rows,'python -m qa_bot.live_session --selected-profile "QA Person 1" --auto'))
        self.assertTrue(conflicting_processes(rows,'python -m qa_bot.live_session --profile-id authorized-row-1 --auto'))
        self.assertFalse(conflicting_processes(rows,'python -m qa_bot.live_session --profile-id authorized-row-10 --auto'))

    def test_busy_port_is_rejected_before_worker_start(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            self.plan['attempts'][0]['ports']['speech']=sock.getsockname()[1]
            with self.assertRaises(OSError):ports_available(self.plan['attempts'])

    def test_commands_use_replay_only_shared_bank_and_separate_output(self):
        self.plan['speech_bank_dir']=str(self.root/'bank')
        built=dict(commands(self.plan['attempts'][0],self.plan))
        for role in ('speech','browser'):
            self.assertIn('--replay-only',built[role])
            self.assertEqual(built[role][built[role].index('--speech-bank-dir')+1],str(self.root/'bank'))
        self.assertNotIn('accept_terms',json.dumps(built))

    def test_environment_ports_do_not_depend_on_json_object_key_order(self):
        spec=self.plan['attempts'][0]
        spec['ports']=dict(reversed(list(spec['ports'].items())))
        validate_plan(self.plan)
        attempt=self.attempt('terms')
        worker=json.loads((attempt.path/'worker.json').read_text())
        self.assertEqual(worker['ports'],['19769','19770','19771','19772'])

    def test_learning_requires_explicit_boolean_and_omits_replay_only_flag(self):
        plan=make_plan(self.scope,self.root/'learning',[26],concurrency=1,replay_only=False)
        validate_plan(plan)
        self.assertFalse(plan['replay_only'])
        for _role,command in commands(plan['attempts'][0],plan):
            self.assertNotIn('--replay-only',command)
        for invalid in (0,1,'false',None):
            with self.assertRaises(ValueError):
                make_plan(self.scope,self.root/'invalid-mode',[1],replay_only=invalid)
        plan['replay_only']=0
        with self.assertRaises(ValueError):validate_plan(plan)

    def test_read_tail_does_not_lose_future_record_after_empty_poll(self):
        attempt=self.attempt('terms')
        path=attempt.path/'append.jsonl';path.touch()
        self.assertEqual(attempt.tail(path),[])
        path.write_text('{"a":1}\n{"b":')
        self.assertEqual(attempt.tail(path),[{'a':1}])
        self.assertEqual(attempt.tail(path),[])
        with path.open('a') as f:f.write('2}\n')
        self.assertEqual(attempt.tail(path),[{'b':2}])

    def test_terms_require_explicit_control_and_worker_stdin_remains_open(self):
        attempt=self.attempt('terms')
        self.poll_until(attempt,'awaiting_terms')
        time.sleep(.1)
        self.assertIsNone(attempt.children['browser'].poll())
        self.assertFalse((attempt.path/'received.jsonl').exists())
        write_json(attempt.path/'control.json',{'sequence':1,'action':'accept_reviewed_terms'})
        attempt.control()
        self.poll_until(attempt,'running')
        self.assertEqual(json.loads((attempt.path/'received.jsonl').read_text()),{'action':'auto_modules','module':'accept_terms'})

    def test_inspect_sends_only_fixed_read_only_commands_through_owner(self):
        attempt=self.attempt('terms');self.poll_until(attempt,'awaiting_terms')
        write_json(attempt.path/'control.json',{'sequence':1,'action':'inspect'})
        attempt.control()
        deadline=time.monotonic()+3;received=[]
        while time.monotonic()<deadline:
            path=attempt.path/'received.jsonl'
            if path.exists():received=[json.loads(line) for line in path.read_text().splitlines()]
            if len(received)==4:break
            time.sleep(.01)
        self.assertEqual(received,[{'action':action} for action in ('state','controls','screenshot','device_diagnostics')])
        self.assertEqual(attempt.state,'awaiting_terms')
        self.assertIsNone(attempt.children['browser'].poll())
        self.assertTrue((attempt.path/'control-0001.applied.json').exists())

    def test_inspect_rejects_arbitrary_payloads_and_terminal_attempt(self):
        attempt=self.attempt('terms');self.poll_until(attempt,'awaiting_terms')
        write_json(attempt.path/'control.json',{'sequence':1,'action':'inspect','command':{'action':'click'}})
        with self.assertRaises(ValueError):attempt.control()
        self.assertFalse((attempt.path/'received.jsonl').exists())
        write_json(attempt.path/'control.json',{'sequence':1,'action':'inspect'})
        attempt.state='cancelled'
        with self.assertRaises(ValueError):attempt.control()
        self.assertFalse((attempt.path/'received.jsonl').exists())

    def test_control_cli_accepts_inspect_and_does_not_allow_reload(self):
        manifest=self.root/'manifest.json';write_json(manifest,self.plan)
        directory=Path(self.plan['attempts'][0]['directory']);directory.mkdir(parents=True)
        with patch('builtins.print'):
            main(['control',str(manifest),'--row','1','--action','inspect'])
        self.assertEqual(json.loads((directory/'control.json').read_text())['action'],'inspect')
        with patch('sys.stderr'),self.assertRaises(SystemExit):
            main(['control',str(manifest),'--row','1','--action','reload'])

    def test_module_error_preserves_live_attempt_without_restart(self):
        attempt=self.attempt('error')
        self.poll_until(attempt,'needs_attention')
        original=attempt.children['browser'].pid
        for _ in range(3):attempt.poll()
        self.assertEqual(attempt.error_count,1)
        self.assertEqual(attempt.children['browser'].pid,original)
        self.assertIsNone(attempt.children['browser'].poll())

    def test_diagnostic_errors_do_not_block_reviewed_terms_control(self):
        attempt=self.attempt('terms');self.poll_until(attempt,'awaiting_terms')
        evidence=attempt.path/'evidence'/'owned'
        (evidence/'page-errors.jsonl').write_text('{"error":"site animation"}\n')
        (evidence/'observer-errors.jsonl').write_text('{"error":"optional observer"}\n')
        self.assertEqual(attempt.poll(),'awaiting_terms')
        self.assertEqual(attempt.error_count,0)
        self.assertEqual(attempt.diagnostic_count,2)
        # A genuine module failure stays visible, while the separately observed
        # terms state still permits the existing DOM-guarded explicit command.
        (evidence/'choice-errors.jsonl').write_text('{"error":"missing exact answer"}\n')
        self.assertEqual(attempt.poll(),'needs_attention')
        self.assertTrue(attempt.terms_visible)
        write_json(attempt.path/'control.json',{'sequence':1,'action':'accept_reviewed_terms'})
        attempt.control()
        deadline=time.monotonic()+3
        while attempt.terms_visible and time.monotonic()<deadline:
            attempt.poll();time.sleep(.01)
        self.assertFalse(attempt.terms_visible)
        self.assertEqual(attempt.state,'needs_attention')
        self.assertEqual(attempt.summary()['diagnostics'],2)

    def test_timeout_and_child_exit_are_not_reported_as_completion(self):
        attempt=self.attempt('timeout');self.poll_until(attempt,'timed_out')
        with (attempt.path/'evidence'/'owned'/'states.jsonl').open('a') as f:
            f.write(json.dumps({'text':'Your test is now complete. Thank you!'})+'\n')
        self.assertEqual(attempt.poll(),'timed_out')
        attempt.close()
        self.open_attempts.remove(attempt)
        self.plan['attempts'][0]['directory']=str(self.root/'different-attempt')
        attempt=self.attempt('exit');self.poll_until(attempt,'failed')
        self.assertEqual(attempt.reason,'browser_exit_7')

    def test_final_text_with_missing_required_submissions_preserves_worker(self):
        attempt=self.attempt('partial_final')
        self.poll_until(attempt,'needs_attention')
        self.assertEqual(attempt.reason,'final_incomplete_required_modules')
        self.assertIsNone(attempt.children['browser'].poll())
        self.assertFalse(attempt.completion_audit['completion_coverage_eligible'])

    def test_clean_final_then_delayed_confirmation_cannot_close_worker(self):
        attempt=self.attempt('delayed_modal')
        deadline=time.monotonic()+.6
        while time.monotonic()<deadline:
            attempt.poll();time.sleep(.01)
        self.assertEqual(attempt.state,'needs_attention')
        self.assertEqual(attempt.reason,'final_confirmation_or_other_visible_content')
        self.assertIsNone(attempt.children['browser'].poll())

    def test_complete_requires_two_fresh_final_reads_in_addition_to_coverage(self):
        attempt=self.attempt('complete')
        self.poll_until(attempt,'platform_complete')
        records=[json.loads(line) for line in (attempt.path/'evidence'/'owned'/'final-handshake.jsonl').read_text().splitlines()]
        self.assertGreaterEqual(len(records),2)
        self.assertGreaterEqual(records[-1]['time']-records[-2]['time'],1)
        self.assertTrue(attempt.completion_audit['completion_verified'])

    def test_bounded_scheduler_finishes_fake_workers_and_does_not_restart_output(self):
        with patch('qa_bot.parallel_supervisor.conflicting_processes',return_value=[]):
            supervise(self.plan,command_factory=self.factory(),require_ports=False,poll_seconds=.01)
            status=json.loads((self.root/'batch'/'status.json').read_text())
            self.assertEqual(status['state'],'finished')
            self.assertEqual([a['state'] for a in status['attempts']],['platform_complete']*3)
            workers=[json.loads((Path(a['directory'])/'worker.json').read_text()) for a in self.plan['attempts']]
            self.assertGreater(workers[2]['started'],max(w['started'] for w in workers[:2])+.1)
            self.assertTrue(all(w['token_length']>=24 for w in workers))
            self.assertEqual(len({tuple(w['ports']) for w in workers}),3)
            for worker in workers:
                with self.assertRaises(ProcessLookupError):os.kill(worker['pid'],0)
            with self.assertRaises(ValueError):
                supervise(self.plan,command_factory=self.factory(),require_ports=False)

    def test_detached_owner_survives_launcher_exit_with_devnull_stdin(self):
        manifest=self.root/'manifest.json';write_json(manifest,self.plan)
        owner=self.root/'owner.py'
        owner.write_text("import json,sys\nfrom pathlib import Path\nfrom qa_bot.parallel_supervisor import supervise\nplan=json.loads(Path(sys.argv[1]).read_text())\ndef factory(spec,plan): return [('browser',[sys.executable,sys.argv[2],spec['directory'],'complete'])]\nsupervise(plan,command_factory=factory,require_ports=False,poll_seconds=.01)\n")
        logpath=self.root/'owner.log'
        # This launcher exits immediately. Its detached child must continue.
        launcher="import subprocess,sys\nwith open(sys.argv[3],'wb') as f:\n p=subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2],sys.argv[4]],stdin=subprocess.DEVNULL,stdout=f,stderr=f,start_new_session=True)\n print(p.pid)\n"
        pid=int(subprocess.check_output([sys.executable,'-c',launcher,str(owner),str(manifest),str(logpath),str(self.worker)],text=True))
        deadline=time.monotonic()+12
        status={}
        try:
            while time.monotonic()<deadline:
                path=self.root/'batch'/'status.json'
                if path.exists():status=json.loads(path.read_text())
                if status.get('state')=='finished':break
                time.sleep(.03)
            self.assertEqual(status.get('state'),'finished',logpath.read_text())
            self.assertEqual(len(status['attempts']),3)
        finally:
            try:os.kill(pid,signal.SIGTERM)
            except ProcessLookupError:pass

    def test_ten_local_workers_reach_barrier_without_sharing_attempt_state(self):
        plan=make_plan(self.scope,self.root/'ten-local',list(range(1,11)),concurrency=10)
        with patch('qa_bot.parallel_supervisor.conflicting_processes',return_value=[]):
            supervise(plan,command_factory=self.factory('concurrent'),require_ports=False,poll_seconds=.01)
        status=json.loads((self.root/'ten-local'/'status.json').read_text())
        self.assertEqual(len(status['attempts']),10)
        self.assertTrue(all(a['state']=='platform_complete' for a in status['attempts']))
        self.assertEqual(len({a['pids']['browser'] for a in status['attempts']}),10)
        for spec in plan['attempts']:
            self.assertEqual(json.loads((Path(spec['directory'])/'simultaneous.json').read_text()),10)


if __name__=='__main__':unittest.main()
