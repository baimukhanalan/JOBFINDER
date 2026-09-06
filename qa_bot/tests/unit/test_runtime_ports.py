import os
import unittest
from unittest.mock import patch
from qa_bot.live_session import bundle
from qa_bot.runtime_ports import loopback_port


class RuntimePortsTests(unittest.TestCase):
    def test_two_workers_have_distinct_bridge_endpoints(self):
        for base in (21000,21010):
            with patch.dict(os.environ,{'QA_SPEECH_PORT':str(base),'QA_PROMPT_CAPTURE_PORT':str(base+2),'QA_RECORDING_CAPTURE_PORT':str(base+3)}):
                script=bundle('a'*30,'test-profile','test-run')
            for port in (base,base+2,base+3):self.assertIn('http://127.0.0.1:'+str(port),script)
            self.assertNotIn('127.0.0.1:1877',script)

    def test_malformed_or_privileged_ports_rejected(self):
        for value in ('80','65536','1234/path','１２３４','-1000'):
            with patch.dict(os.environ,{'QA_SPEECH_PORT':value}),self.assertRaises(ValueError):
                loopback_port('QA_SPEECH_PORT',18769)
