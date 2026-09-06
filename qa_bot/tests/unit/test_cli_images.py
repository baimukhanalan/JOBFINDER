import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
from qa_bot.adapters.llm.codex_cli import CodexCLIClient

class ImageTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_tampered_image_never_launches_model(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);exe=root/'codex';exe.touch();schema=root/'schema.json';schema.write_text('{}')
            image=root/'question.png';image.write_bytes(b'changed')
            client=CodexCLIClient(exe,schema,root)
            with patch('asyncio.create_subprocess_exec',new_callable=AsyncMock) as launch:
                with self.assertRaisesRegex(ValueError,'integrity'):
                    await client.complete({'_image_attachments':[{'location':str(image),'sha256':hashlib.sha256(b'original').hexdigest()}]},timeout=1)
                launch.assert_not_called()

    async def test_image_passed_as_argument_without_exposing_attachment_in_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);exe=root/'codex';exe.touch();schema=root/'schema.json';schema.write_text('{}')
            image=root/'question.png';image.write_bytes(b'image')
            client=CodexCLIClient(exe,schema,root)
            process=AsyncMock();process.returncode=0
            process.communicate.return_value=(b'{"type":"item.completed","item":{"type":"agent_message","text":"{}"}}',b'')
            with patch('asyncio.create_subprocess_exec',new_callable=AsyncMock,return_value=process) as launch:
                await client.complete({'question_id':'q','_image_attachments':[{'location':str(image),'sha256':hashlib.sha256(b'image').hexdigest()}]},timeout=1)
                args=launch.call_args.args
                self.assertEqual(args[args.index('--image')+1],str(image.resolve()))
                self.assertNotIn(b'_image_attachments',process.communicate.call_args.args[0])
