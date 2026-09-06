import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from qa_bot.knowledge.computer_cache import ComputerActionCache,identity
from qa_bot.knowledge.computer_backfill import audit_question
from qa_bot.knowledge.computer_protocol import ORIGIN,PATH

class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.run=Path(self.temp.name)/'row';self.owned=self.run/'evidence/owned';self.owned.mkdir(parents=True)
        self.cache=ComputerActionCache(Path(self.temp.name)/'cache.sqlite3')
        shot=self.owned/'step.png';shot.write_bytes(b'synthetic screenshot')
        self.key,self.canonical=identity('Synthetic task',[],hashlib.sha256(shot.read_bytes()).hexdigest(),ORIGIN+PATH)
        self.action={'question':'1','question_id':'1','action':'click','id':'go','confidence':.9,'key':self.key,'original_test':'first','screenshot':'step.png'}
        self.event={'question':'1','sequence':1,'time':1500,'fields':{'message':{'score':1,'log':[]}}}
        self.write('computer-actions',[self.action]);self.write('computer-observations',[{'question':'1','key':self.key,'screenshot':'step.png','state':json.loads(self.canonical)}])
        self.write('computer',[{'number':'1','advanced':True,'time':2}]);self.write('computer-events',[self.event])
        (self.run/'browser.log').write_text('\n'.join(json.dumps(x) for x in [{'state':{'number':'1'}},{'computer_step':'1','action':'click'},{'state':{'number':'2'}}])+'\n')
        self.cache.observe(self.key,self.canonical,'first','1')
        self.cache.promote([(self.key,self.canonical,self.action)],'first')
        self.cache.record_outcome('first','1',[self.event])
    def write(self,name,data):
        (self.owned/(name+'.jsonl')).write_text(''.join(json.dumps(x)+'\n' for x in data))
    def tearDown(self):self.cache.close();self.temp.cleanup()
    def test_complete_causal_legacy_evidence_verifies_without_fabricated_times(self):
        result=audit_question(self.cache,self.run,'first','1',apply=True)
        self.assertEqual(result['verification'],'verified_success')
        self.assertEqual(result['evidence_basis'],'runtime_question_outcome_no_per_action_clock')
        self.assertEqual(self.cache.lookup(self.key,self.canonical)[2],.9)
        self.assertNotIn('action_started_ms',self.action)
    def test_snapshot_tampering_is_rejected(self):
        (self.owned/'step.png').write_bytes(b'changed')
        result=audit_question(self.cache,self.run,'first','1',apply=True)
        self.assertEqual(result['reason'],'screenshot_changed');self.assertIsNone(self.cache.lookup(self.key,self.canonical))
    def test_extra_cached_pending_step_prevents_partial_import(self):
        key,canonical=identity('Synthetic task',[{'id':'other'}],'b'*64,ORIGIN+PATH)
        self.cache.observe(key,canonical,'first','1');self.cache.promote([(key,canonical,dict(self.action,id='other'))],'first')
        result=audit_question(self.cache,self.run,'first','1',apply=True)
        self.assertEqual(result['reason'],'incomplete_pending_action_sequence')
    def test_no_runtime_causal_outcome_does_not_guess_from_event_log(self):
        with self.cache.db:self.cache.db.execute("DELETE FROM question_outcomes WHERE question='1'")
        result=audit_question(self.cache,self.run,'first','1',apply=True)
        self.assertEqual(result['reason'],'one_runtime_causal_outcome_required')

    def test_later_failure_disables_previously_verified_sequence(self):
        self.assertEqual(audit_question(self.cache,self.run,'first','1',apply=True)['verification'],'verified_success')
        failed=json.loads(json.dumps(self.event));failed['fields']['message']['score']=0
        self.cache.record_outcome('first','1',[failed])
        result=audit_question(self.cache,self.run,'first','1',apply=True)
        self.assertEqual(result['reason'],'provider_reported_failure')
        self.assertIsNone(self.cache.lookup(self.key,self.canonical))
