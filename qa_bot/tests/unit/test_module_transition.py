import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from urllib.parse import quote, urlencode
import unittest

from qa_bot.module_transition import transition_evidence
from qa_bot.live_session import Session


class ModuleTransitionTests(unittest.TestCase):
    def test_cutoff_is_observed_without_recording_answers_or_auth(self):
        response = {'data': {'cutOffData': {'cutOffCleared': False, 'cutOffMsg': 'private'},
                             'moduleId': 42, 'moduleSwitched': True,
                             'preloadableQuestionData': ['private'], 'token': 'secret'}}
        result = transition_evidence('/api/v1/test/switch-module',
            b'prevModuleId=41&moduleStatus=2&answerObject=private&amcatId=private',
            json.dumps(response).encode())
        self.assertEqual(result['request']['prevModuleId'], '41')
        self.assertEqual(result['request']['moduleStatus'], '2')
        self.assertFalse(result['response']['cutoff_cleared'])
        self.assertNotIn('private', json.dumps(result))
        self.assertNotIn('secret', json.dumps(result))

    def test_end_reason_and_reuse_are_distinct_from_cutoff(self):
        result = transition_evidence('/api/v1/test/end', b'exitType=102&prevModuleId=42',
            b'{"data":{"isResponseReUsed":true,"cutOffData":null}}')
        self.assertEqual(result['request']['exitType'], '102')
        self.assertTrue(result['response']['isResponseReUsed'])
        self.assertFalse(result['response']['cutoff_present'])

    def test_malformed_or_unexpected_values_do_not_leak(self):
        result = transition_evidence('/api/v1/test/end', b'exitType=secret&moduleStatus=1&moduleStatus=2',
            b'{"data":{"cutOffData":{"cutOffCleared":"secret"},"moduleId":"secret"}}')
        self.assertEqual(result['request'], {'answer_object_present': False})
        self.assertNotIn('secret', json.dumps(result))
        self.assertTrue(transition_evidence('/api/v1/test/end', b'', b'bad')['response_parse_error'])
        with self.assertRaises(ValueError):
            transition_evidence('/api/v1/question', b'', b'{}')

    def test_simulation_result_is_present_in_actual_save_payload(self):
        answer = {'answerResponse': {'message': {'score': 1, 'log': ['private']},
                                     'wrongAttempts': 0}, 'timeLapsed': False, 'sid': 'private'}
        encoded = base64.b64encode(quote(json.dumps(answer)).encode()).decode()
        result = transition_evidence('/api/v1/qb/get-next-question',
            urlencode({'answerObject': encoded, 'moduleId': 55}).encode(),
            b'{"data":{"questionNumber":16,"moduleStatus":6,"questionDetails":"private"}}')
        self.assertEqual(result['request']['simulation_score'], 1)
        self.assertEqual(result['response']['questionNumber'], 16)
        self.assertFalse(result['request']['time_lapsed'])
        self.assertNotIn('private', json.dumps(result))


class TransitionObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_api_origin_is_observed_and_other_origin_is_excluded(self):
        class Response:
            status = 200
            def __init__(self, host):
                self.request = SimpleNamespace(method='POST',
                    url=f'https://{host}/api/v1/test/end?token=private',
                    post_data_buffer=b'exitType=101&amcatId=private')
            async def body(self):
                return b'{"data":{"testEndMessage":"private"}}'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session(None, root)
            await session.record_response(Response('amcatglobalapi.aspiringminds.com'))
            await session.record_response(Response('unrelated.example'))
            rows = [json.loads(line) for line in (root/'module-transitions.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['request']['exitType'], '101')
            self.assertNotIn('private', json.dumps(rows))
