import json
from pathlib import Path
import tempfile
import unittest

from qa_bot.run_audit import audit, module_menu, final_text_status


MENU = '''Assessments
System Diagnostic Tool
1 Task in 5 Minutes
Complete
SVAR - Spoken English (U.S.)
4 Sections
Complete
Typing
1 QUESTION
Complete
Personality
72 question(s) in 15 Minutes
Complete
Basic Analytical Ability
19 question(s) in 10 Minutes
Complete
{extra}
Sales Competency Test
20 question(s) in 35 Minutes
Upcoming
NEXT'''
COMPUTER = 'Basic Computer Literacy Simulation (Windows 10)\n16 question(s) in 20 Minutes\nComplete'
WRITEX = 'WriteX - Email Writing\n1 Email in 15 Minutes\nComplete'
FINAL = 'Your test is now complete. Thank you!'


class RunAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/'evidence'/'owned'
        self.root.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def log(self, name, rows):
        (self.root/name).write_text(''.join(json.dumps(r)+'\n' for r in rows))

    def full(self, extra=COMPUTER):
        self.log('states.jsonl',[{'text':MENU.format(extra=extra),'time':1},{'text':FINAL,'time':100}])
        self.log('final-handshake.jsonl',[{'time':101,'source':'owned_state_response','visible_text':FINAL},
                                         {'time':102.1,'source':'owned_state_response','visible_text':FINAL}])
        self.log('actions.jsonl',[{'action':'submit_speech','number':str(n)} for n in [1,*range(2,23),28]])
        self.log('choices.jsonl',[{'number':str(n),'confidence':.99,'correctness_verified':False} for n in range(23,28)])
        self.log('typing.jsonl',[{'number':'1','practice':True,'exact_match':True},{'number':'2','practice':False,'exact_match':True}])
        self.log('personality.jsonl',[{'number':str(n),'answer':'QA answer'} for n in range(1,73)])
        self.log('analytical.jsonl',[{'number':str(n),'selected':'QA option'} for n in range(1,20)])
        self.log('sales.jsonl',[{'number':str(n),'roles':[{'role':'best'},{'role':'worst'}]} for n in range(1,21)])
        if extra==COMPUTER:
            self.log('computer.jsonl',[{'number':str(n),'advanced':True} for n in range(1,17)])
            self.log('computer-actions.jsonl',[{'question':str(n),'action':'click'} for n in range(1,17)])
            self.log('computer-final-submit.jsonl',[{'number':'16','confirmed':True}])
        else:
            self.log('writex.jsonl',[{'number':'1','exact_fields':True,'body_words':80}])

    def test_full_computer_path_is_155_scored_but_not_proof_of_correctness_or_autonomy(self):
        self.full();report=audit(self.root)
        self.assertTrue(report['completion_verified'])
        self.assertEqual(report['expected_scored'],155)
        self.assertEqual(report['submitted_scored_unique'],155)
        self.assertFalse(report['correctness_verified'])
        self.assertFalse(report['autonomy_verified'])
        self.assertFalse(report['modules']['computer']['success_verified'])
        self.assertEqual(report['modules']['svar']['submitted_unique'],27)

    def test_writex_variant_changes_required_modules_and_count(self):
        self.full(WRITEX);report=audit(self.root)
        self.assertTrue(report['completion_verified'])
        self.assertEqual(report['expected_scored'],140)
        self.assertNotIn('computer',report['requirements']['expected'])

    def test_final_page_with_missing_late_modules_cannot_be_complete(self):
        self.full()
        self.log('analytical.jsonl',[{'number':str(n),'selected':'QA option'} for n in range(1,16)])
        self.log('sales.jsonl',[])
        report=audit(self.root)
        self.assertFalse(report['completion_verified'])
        self.assertTrue(report['platform_final_observed'])
        self.assertEqual(report['modules']['analytical']['missing'],[16,17,18,19])
        self.assertEqual(report['modules']['sales']['submitted_unique'],0)
        self.assertIn('required_module_has_no_submissions:sales',report['warnings'])

    def test_advancement_is_not_enough_without_actual_actions_and_final_confirmation(self):
        self.full();self.log('computer-actions.jsonl',[]);self.log('computer-final-submit.jsonl',[])
        report=audit(self.root)
        self.assertEqual(report['modules']['computer']['submitted_unique'],16)
        self.assertFalse(report['modules']['computer']['coverage_complete'])
        self.assertFalse(report['completion_verified'])

    def test_timeout_stays_failure_even_if_final_and_all_submission_logs_exist(self):
        self.full();self.log('run-failures.jsonl',[{'reason':'assessment_time_out','time':50}])
        report=audit(self.root)
        self.assertFalse(report['completion_verified'])
        self.assertTrue(report['submission_coverage_complete'])
        self.assertTrue(report['timeouts']['observed'])
        self.assertEqual(report['submitted_scored_unique'],155)

    def test_duplicate_submission_counts_once_and_requires_review(self):
        self.full()
        with (self.root/'choices.jsonl').open('a') as stream:stream.write('{"number":"23"}\n')
        report=audit(self.root)
        self.assertEqual(report['submitted_scored_unique'],155)
        self.assertEqual(report['modules']['svar']['duplicates'],{'23':2})
        self.assertFalse(report['completion_verified'])

    def test_missing_menu_cannot_be_reconstructed_from_completed_answer_logs(self):
        self.full();self.log('states.jsonl',[{'text':FINAL}])
        report=audit(self.root)
        self.assertFalse(report['completion_verified'])
        self.assertIn('complete_module_requirements_not_verified',report['warnings'])

    def test_unknown_module_and_changed_question_counts_are_not_silently_ignored(self):
        states=[{'text':MENU.format(extra='New Assessment\n8 question(s) in 10 Minutes\nLater').replace('19 question(s)','24 question(s)')}]
        result=module_menu(states)
        self.assertEqual(result['unknown_modules'],['New Assessment'])
        self.assertEqual(result['count_mismatches'],[{'module':'analytical','observed':24,'supported':19}])

    def test_prepared_topic_does_not_count_as_submitted_speech(self):
        self.full();self.log('actions.jsonl',[{'action':'submit_speech','number':str(n)} for n in range(2,23)])
        self.log('topics.jsonl',[{'number':'28','status':'ready','source':'replay'}])
        report=audit(self.root)
        self.assertEqual(report['modules']['svar']['sections']['D']['missing'],[28])
        self.assertFalse(report['completion_verified'])

    def test_partial_log_and_bad_typing_evidence_do_not_pass(self):
        self.full();self.log('typing.jsonl',[{'number':'2','practice':False,'exact_match':False}])
        with (self.root/'sales.jsonl').open('a') as stream:stream.write('{"number":')
        report=audit(self.root)
        self.assertFalse(report['completion_verified'])
        self.assertEqual(report['malformed_records'],{'sales.jsonl':1})
        self.assertEqual(report['modules']['typing']['submitted_unique'],0)

    def test_page_diagnostics_and_real_module_errors_are_reported_separately(self):
        self.full();self.log('page-errors.jsonl',[{'error':'benign animation'}]);self.log('analytical-errors.jsonl',[{'error':'missing answer'}])
        report=audit(self.root)
        self.assertEqual(report['diagnostic_errors'],{'page-errors.jsonl':1})
        self.assertEqual(report['module_errors'],{'analytical-errors.jsonl':1})
        self.assertTrue(report['completion_verified'])

    def test_observed_operator_mutations_are_not_hidden_by_terms_control(self):
        self.full()
        (self.root.parent.parent/'control-0001.applied.json').write_text(json.dumps({'sequence':1,'action':'accept_reviewed_terms'}))
        self.log('operator-commands.jsonl',[{'action':'auto_modules','module':'accept_terms'},{'action':'state'},{'action':'retry_read'}])
        report=audit(self.root)
        self.assertEqual(report['operator']['observed_non_terms_mutations'],[{'action':'retry_read','module':None}])
        self.assertEqual(len(report['operator']['explicit_controls']),1)
        self.assertFalse(report['operator']['full_command_coverage_verified'])
        self.assertFalse(report['operator']['no_logged_manual_answer_controls'])

    def test_no_logged_answer_controls_is_positive_but_does_not_prove_absent_physical_input(self):
        self.full()
        report=audit(self.root)
        self.assertTrue(report['operator']['no_logged_manual_answer_controls'])
        self.assertFalse(report['operator']['operator_command_file_present'])
        self.assertFalse(report['operator']['full_command_coverage_verified'])
        self.assertFalse(report['autonomy_verified'])
        self.log('operator-commands.jsonl',[{'action':'state'},{'action':'verify_recording'},
                                          {'action':'auto_modules','module':'accept_terms'}])
        report=audit(self.root)
        self.assertTrue(report['operator']['no_logged_manual_answer_controls'])
        self.assertEqual(report['operator']['operator_command_records'],3)
        self.assertIn('physical browser interaction',report['operator']['evidence_scope'])

    def test_final_phrase_embedded_in_other_content_is_not_terminal(self):
        self.assertEqual(final_text_status('Question: '+FINAL),'absent')
        self.assertEqual(final_text_status('Skip to main content\n'+FINAL),'clean')
        self.assertEqual(final_text_status(FINAL+'\nWarning!\nSUBMIT\nCANCEL'),'blocked')

    def test_old_clean_frame_followed_by_modal_cannot_pass_even_with_full_coverage(self):
        self.full()
        with (self.root/'states.jsonl').open('a') as stream:
            stream.write(json.dumps({'time':103,'text':FINAL+'\nAre you ready to submit this assessment?'})+'\n')
        report=audit(self.root)
        self.assertTrue(report['completion_coverage_eligible'])
        self.assertFalse(report['platform_final_clean'])
        self.assertFalse(report['completion_verified'])

    def test_single_clean_final_frame_without_fresh_handshake_is_unproven(self):
        self.full();self.log('final-handshake.jsonl',[])
        report=audit(self.root)
        self.assertTrue(report['completion_coverage_eligible'])
        self.assertFalse(report['final_stability_verified'])
        self.assertFalse(report['completion_verified'])

    def test_old_handshake_cannot_verify_a_later_final_transition(self):
        self.full()
        with (self.root/'states.jsonl').open('a') as stream:
            stream.write(json.dumps({'time':103,'text':'Question still active'})+'\n')
            stream.write(json.dumps({'time':104,'text':FINAL})+'\n')
        report=audit(self.root)
        self.assertTrue(report['platform_final_clean'])
        self.assertFalse(report['final_stability_verified'])
        self.assertFalse(report['completion_verified'])


if __name__=='__main__':unittest.main()
