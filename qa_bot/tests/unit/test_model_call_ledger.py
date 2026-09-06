import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from qa_bot.adapters.llm.codex_cli import CodexCLIClient


class ModelCallLedgerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        executable=self.root/'codex';executable.touch()
        schema=self.root/'schema.json';schema.write_text('{}')
        self.client=CodexCLIClient(executable,schema,self.root,reasoning_effort='low',best_effort=True)
        self.question={'question_id':'analytical-3','content_hash':'a'*64,'question_text':'PRIVATE QUESTION'}

    def tearDown(self):
        self.temp.cleanup()

    def records(self):
        return [json.loads(line) for line in (self.root/'model-calls.jsonl').read_text().splitlines()]

    def worker(self, status='answer', returncode=0):
        process=MagicMock();process.returncode=returncode
        event={'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'status':status,'text':'PRIVATE ANSWER'})}}
        process.communicate=AsyncMock(return_value=(json.dumps(event).encode(),b'PRIVATE STDERR'))
        process.wait=AsyncMock()
        return process

    async def test_completed_abstain_and_failure_all_count_actual_launches(self):
        for status,code,outcome in (('answer',0,'completed'),('abstain',0,'abstain'),('answer',7,'error')):
            process=self.worker(status,code)
            with patch('asyncio.create_subprocess_exec',new_callable=AsyncMock,return_value=process):
                if code:
                    with self.assertRaises(ValueError):await self.client.complete(self.question,timeout=1)
                else:await self.client.complete(self.question,timeout=1)
            start,end=self.records()[-2:]
            self.assertEqual(start['event'],'started');self.assertTrue(start['process_started'])
            self.assertEqual(start['call_id'],end['call_id']);self.assertEqual(end['outcome'],outcome)
            self.assertEqual(end['returncode'],code)
            self.assertGreaterEqual(end['completed_at'],end['requested_at'])
            self.assertGreaterEqual(end['elapsed_seconds'],0)
        records=self.records()
        self.assertEqual(len({r['call_id'] for r in records if r['event']=='started'}),3)
        self.assertEqual(records[0]['question_id'],'analytical-3')
        self.assertEqual(records[0]['reasoning_effort'],'low');self.assertTrue(records[0]['best_effort'])

    async def test_timeout_retains_started_event_and_kills_owned_child(self):
        process=self.worker();process.returncode=None
        async def pending(_prompt):await asyncio.sleep(10)
        process.communicate.side_effect=pending
        with patch('asyncio.create_subprocess_exec',new_callable=AsyncMock,return_value=process):
            with self.assertRaises(TimeoutError):await self.client.complete(self.question,timeout=.01)
        start,end=self.records();self.assertEqual(start['event'],'started')
        self.assertEqual(end['outcome'],'timeout');self.assertEqual(end['error_type'],'TimeoutError')
        process.kill.assert_called_once();process.wait.assert_awaited_once()

    async def test_launch_failure_does_not_claim_process_started_or_log_exception_text(self):
        with patch('asyncio.create_subprocess_exec',new_callable=AsyncMock,side_effect=OSError('PRIVATE TOKEN')):
            with self.assertRaises(OSError):await self.client.complete(self.question,timeout=1)
        records=self.records();self.assertEqual(len(records),1)
        self.assertFalse(records[0]['process_started']);self.assertEqual(records[0]['error_type'],'OSError')
        self.assertNotIn('PRIVATE TOKEN',json.dumps(records))

    async def test_metadata_excludes_sensitive_content_and_paths(self):
        image=self.root/'PRIVATE-ATTACHMENT.png';image.write_bytes(b'image')
        digest=hashlib.sha256(b'image').hexdigest()
        question={**self.question,'question_id':'unsafe private id / path '+'x'*100,
                  'content_hash':'PRIVATE INVALID HASH','_image_attachments':[{'location':str(image),'sha256':digest}]}
        with patch('asyncio.create_subprocess_exec',new_callable=AsyncMock,return_value=self.worker()):
            await self.client.complete(question,timeout=1)
        records=self.records();raw=json.dumps(records)
        for forbidden in ('PRIVATE','unsafe private',str(self.root),'QUESTION_DATA','_image_attachments'):
            self.assertNotIn(forbidden,raw)
        self.assertTrue(records[0]['question_id'].startswith('sha256:'))
        self.assertIsNone(records[0]['content_hash']);self.assertEqual(records[0]['image_count'],1)
        self.assertEqual(records[0]['image_sha256'],[digest])

    async def test_telemetry_write_failure_does_not_change_model_result(self):
        with patch('asyncio.create_subprocess_exec',new_callable=AsyncMock,return_value=self.worker()), \
             patch('qa_bot.adapters.llm.codex_cli.os.write',side_effect=OSError('disk unavailable')):
            result=await self.client.complete(self.question,timeout=1)
        self.assertEqual(result['status'],'answer')

    async def test_image_validation_failure_does_not_count_an_llm_call(self):
        question={**self.question,'_image_attachments':[{'location':str(self.root/'absent.png'),'sha256':'PRIVATE HASH'}]}
        with patch('asyncio.create_subprocess_exec',new_callable=AsyncMock) as launch:
            with self.assertRaises(ValueError):await self.client.complete(question,timeout=1)
            launch.assert_not_called()
        records=self.records();self.assertFalse(records[0]['process_started'])
        self.assertEqual(records[0]['image_sha256'],[None])
