import json
from pathlib import Path
import tempfile
import unittest
from qa_bot.knowledge.live_corpus import clean_text,collect_run,summarize,main

class CorpusTests(unittest.TestCase):
    def write_run(self,root,name,states,logs=None):
        run=root/name;owned=run/'evidence/owned';owned.mkdir(parents=True)
        (owned/'states.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in states))
        for file,data in (logs or {}).items():(owned/(file+'.jsonl')).write_text(''.join(json.dumps(x)+'\n' for x in data))
        return run
    def test_module_boundaries_repeated_numbers_and_timer_dedup(self):
        base='Skip to main content\n{timer}\nHelp\nExit\nChoose the correct option.\nDo NOT select 12.\nOPTIONS\n12\n21\nSUBMIT ANSWER\n'+ '\n'.join(str(i) for i in range(1,20))
        states=[{'number':None,'text':'Assessments\nBasic Analytical Ability\n19 question(s)\nUpcoming'},
            {'number':'1','text':base.format(timer='10 : 00')},{'number':'1','text':base.format(timer='09 : 59')},
            {'number':None,'text':'ASSESSMENT DESCRIPTION\nTyping\n1 QUESTION'},
            {'number':'1\nTooltip','text':'Type the given paragraph EXACTLY as shown in the space provided.\nDo NOT select 12.\n00 : 32 Time Left\nSUBMIT ANSWER\n1\n2'}]
        with tempfile.TemporaryDirectory() as d:
            run=self.write_run(Path(d),'first',states)
            records,meta=collect_run(run)
            analytical=[r for r in records if r['section']=='analytical' and r['kind']=='question']
            self.assertEqual(len(analytical),1);self.assertEqual(len(analytical[0]['evidence']),2)
            self.assertIn('NOT select 12',analytical[0]['content']['text'])
            self.assertIn('12\n21',analytical[0]['content']['text'])
            self.assertEqual(len([r for r in records if r['section']=='typing' and r['kind']=='question']),1)
    def test_changed_prompt_is_not_merged_and_unanswered_not_invented(self):
        with tempfile.TemporaryDirectory() as d:
            run=self.write_run(Path(d),'first',[{'number':'1','text':'Choose the correct option.\nDo NOT buy 2 items.'},
                {'number':'1','text':'Choose the correct option.\nDo buy 2 items.'},
                {'number':'2','text':'Skip to main content\nHelp\nExit\nSKIP'}],{'analytical':[{'number':'1','selected':'A'}]})
            records,meta=collect_run(run)
            self.assertEqual(len([r for r in records if r['kind']=='question']),2)
            self.assertTrue(any(r['kind']=='ambiguous_transition' and not r['answer_observed'] for r in records))
            report=summarize(records,[meta]);section=report['sections']['first']['analytical']
            self.assertEqual(section['question_numbers_observed'],1)
            self.assertEqual(section['question_numbers_with_answer_evidence'],1)
            self.assertFalse(section['complete_variant_proven'])
    def test_audio_identity_and_order_are_retained(self):
        state={'number':'14','text':'Section B: Listen and Repeat\nIn this section, listen and repeat.','heard':[
            {'id':'14','section':'Section B: Listen and Repeat','sha256':'a'*64,'order':1}]}
        with tempfile.TemporaryDirectory() as d:
            a,ma=collect_run(self.write_run(Path(d),'first',[state]))
            state['heard'][0]['sha256']='b'*64
            b,mb=collect_run(self.write_run(Path(d),'second',[state]))
            report=summarize(a+b,[ma,mb]);overlap=[r for r in report['overlaps'] if r['section']=='svar_b'][0]
            self.assertEqual(overlap['shared_exact_signatures'],0)
            self.assertFalse(overlap['same_observed_order'])
    def test_empty_explicit_input_and_nonignored_output_rejected(self):
        with self.assertRaises(SystemExit):main(['--output','/tmp/corpus'])
        with self.assertRaises(SystemExit):main(['--run','anything','--output','/tmp/corpus'])
    def test_numeric_options_and_option_order_survive(self):
        text='Choose the correct option.\nOPTIONS\n1\n2\n3\nSUBMIT ANSWER'
        self.assertIn('1\n2\n3',clean_text(text))
        options='\n'.join(str(i) for i in range(1,20))
        self.assertIn(options,clean_text('Choose the correct option.\nOPTIONS\n'+options+'\nSUBMIT ANSWER','analytical'))
