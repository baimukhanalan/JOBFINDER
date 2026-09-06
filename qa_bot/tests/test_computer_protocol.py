import json
import tempfile
import unittest
from pathlib import Path
from qa_bot.knowledge.computer_cache import ComputerActionCache,identity
from qa_bot.knowledge.computer_protocol import outcome,ORIGIN,PATH

class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.key,self.canonical=identity('Synthetic task',[],'a'*64,ORIGIN+PATH)
        self.action={'question_id':'1','action':'click','id':'go','confidence':.93,'action_started_ms':1000,'action_finished_ms':1100,'event_sequence_before_action':0}
        self.steps=[(self.key,self.canonical,self.action)]
        self.event={'question':'1','sequence':1,'time':1050,'fields':{'message':{'score':1,'log':[]}}}
    def test_typed_result_verifies_without_inflating_confidence(self):
        with tempfile.TemporaryDirectory() as d:
            c=ComputerActionCache(Path(d)/'actions.sqlite3')
            try:
                result=c.promote(self.steps,'first',question='1',events=[self.event],advanced_at_ms=1200)
                self.assertEqual(result['verification'],'verified_success')
                cached=c.lookup(self.key,self.canonical);self.assertEqual(cached[2],.93)
                failed=json.loads(json.dumps(self.event));failed['fields']['message']['score']=0
                c.promote(self.steps,'second',question='1',events=[failed],advanced_at_ms=1200)
                self.assertIsNone(c.lookup(self.key,self.canonical))
            finally:c.close()
    def test_wrong_question_early_or_ambiguous_result_never_verifies(self):
        variants=[dict(self.event,question='2'),dict(self.event,time=999),dict(self.event,sequence=0),
            dict(self.event,fields={'message':{'score':True,'log':[]}}),dict(self.event,fields={'ready':True})]
        for event in variants:self.assertNotEqual(outcome('1',self.steps,[event],1200)['verification'],'verified_success')
        self.assertNotEqual(outcome('1',self.steps,[self.event,self.event],1200)['verification'],'verified_success')
    def test_absent_action_clock_is_not_invented(self):
        action={k:v for k,v in self.action.items() if k!='action_started_ms'}
        result=outcome('1',[(self.key,self.canonical,action)],[self.event],1200)
        self.assertEqual(result['reason'],'missing_action_timing')

    def test_completed_scene_wait_requires_same_task_and_causal_success(self):
        from qa_bot.solvers.computer import completed_task_result
        self.assertTrue(completed_task_result('1','Synthetic task',self.steps,[self.event],1200))
        self.assertFalse(completed_task_result('1','Changed task',self.steps,[self.event],1200))
        self.assertFalse(completed_task_result('2','Synthetic task',self.steps,[self.event],1200))
        self.assertFalse(completed_task_result('1','Synthetic task',[],[self.event],1200))
        variants=[dict(self.event,time=999),dict(self.event,sequence=0),
                  dict(self.event,fields={'message':{'score':0,'log':[]}}),
                  dict(self.event,fields={'message':{'score':1,'log':[],'error':'failed'}})]
        for event in variants:
            self.assertFalse(completed_task_result('1','Synthetic task',self.steps,[event],1200))
        self.assertFalse(completed_task_result('1','Synthetic task',self.steps,[self.event,variants[2]],1200))
