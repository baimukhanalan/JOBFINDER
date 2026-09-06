import asyncio
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout
import hashlib
import io
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from qa_bot.audio.transcript_cache import TranscriptCache, natively_model_identity
from qa_bot.adapters.stt.natively_local import NativelyLocalSTT
from tests.unit.test_speech_replay import tone_wav


def _prepare(directory, number, unique=False):
    root = Path(directory)
    async def transcribe():
        with (root / 'calls').open('a') as stream:
            stream.write(json.dumps({'time': time.time(), 'event': 1, 'worker': number}) + '\n')
        await asyncio.sleep(.1)
        with (root / 'calls').open('a') as stream:
            stream.write(json.dumps({'time': time.time(), 'event': -1, 'worker': number}) + '\n')
        return 'Original clip transcript.'
    audio = b'audio' + (str(number).encode() if unique else b'')
    result = asyncio.run(TranscriptCache(root).get_or_transcribe(audio, model_identity='fixture-model-v1',
                         transcribe=transcribe, source={'profile': str(number)}))
    return result.text, result.audio_sha256, result.text_sha256, result.source


class TranscriptCacheTests(unittest.IsolatedAsyncioTestCase):
    def test_ten_processes_reuse_one_transcription(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProcessPoolExecutor(max_workers=10, mp_context=multiprocessing.get_context('spawn')) as pool:
                results = list(pool.map(_prepare, [directory] * 10, range(10)))
            calls = [json.loads(x) for x in (Path(directory) / 'calls').read_text().splitlines()]
            self.assertEqual(len(calls), 2)
            self.assertEqual(sum(r[3] == 'transcribed' for r in results), 1)
            self.assertEqual(len({r[:3] for r in results}), 1)

    def test_distinct_clips_never_load_multiple_stt_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context('spawn')) as pool:
                results = list(pool.map(_prepare, [directory] * 4, range(4), [True] * 4))
            calls = sorted((json.loads(x) for x in (Path(directory) / 'calls').read_text().splitlines()), key=lambda r: r['time'])
            active = maximum = 0
            for event in calls:
                active += event['event']
                maximum = max(maximum, active)
            self.assertEqual((maximum, active), (1, 0))
            self.assertEqual(len({r[1] for r in results}), 4)

    async def test_changed_bytes_or_model_miss_and_tampered_text_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = TranscriptCache(directory)
            callback = AsyncMock(return_value='Recognized words.')
            await cache.get_or_transcribe(b'a', model_identity='v1', transcribe=callback)
            await cache.get_or_transcribe(b'b', model_identity='v1', transcribe=callback)
            await cache.get_or_transcribe(b'a', model_identity='v2', transcribe=callback)
            self.assertEqual(callback.await_count, 3)
            for path in Path(directory).glob('*.json'):
                row = json.loads(path.read_text())
                row['text'] = 'Changed answer'
                path.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError, 'integrity'):
                await cache.get_or_transcribe(b'a', model_identity='v1', transcribe=callback)
            self.assertEqual(callback.await_count, 3)

    async def test_failed_transcription_is_not_cached_and_lock_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = TranscriptCache(directory)
            callback = AsyncMock(side_effect=RuntimeError('worker failed'))
            with self.assertRaises(RuntimeError):
                await cache.get_or_transcribe(b'a', model_identity='v1', transcribe=callback)
            self.assertEqual(list(Path(directory).glob('*.json')), [])
            callback.side_effect = None
            callback.return_value = 'Now complete.'
            result = await cache.get_or_transcribe(b'a', model_identity='v1', transcribe=callback)
            self.assertEqual(result.source, 'transcribed')

    def test_model_identity_changes_when_model_driver_or_runtime_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'org/model'
            model.mkdir(parents=True)
            weights = model / 'encoder.onnx'; weights.write_bytes(b'weights')
            driver = root / 'driver.mjs'; driver.write_text('driver')
            runtime = root / 'runtime'; runtime.mkdir()
            package = runtime / 'package.json'; package.write_text('{"version":"1"}')
            stt = SimpleNamespace(cache_dir=root, model='org/model', driver=driver, transformers_root=runtime)
            identities = [natively_model_identity(stt)]
            weights.write_bytes(b'newweights'); identities.append(natively_model_identity(stt))
            driver.write_text('newdriver'); identities.append(natively_model_identity(stt))
            package.write_text('{"version":"2"}'); identities.append(natively_model_identity(stt))
            self.assertEqual(len(set(identities)), 4)

    async def test_sessions_reuse_verified_cache_and_reject_changed_original(self):
        from qa_bot.live_session import Session
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = b'original observed clip'
            digest = hashlib.sha256(audio).hexdigest()
            sessions = []
            for number in range(2):
                session = Session.__new__(Session)
                session.prompt_dir = root / str(number) / 'prompts'; session.prompt_dir.mkdir(parents=True)
                session.output = root / str(number) / 'owned'; session.output.mkdir()
                session.speech_bank_dir = root / 'shared'
                session.project_root = root
                session.profile_id, session.test_id = str(number), 'fixture'
                session.page = object(); session.stt_lock = asyncio.Semaphore(1); session.log = Mock()
                (session.prompt_dir / 'clip.mp3').write_bytes(audio)
                # A stale text sidecar must not override the verified shared result.
                (session.output / (digest + '.txt')).write_text('Stale unverified text.')
                sessions.append(session)
            decode = AsyncMock(return_value=tone_wav())
            transcribe = AsyncMock(return_value='Verified spoken words.')
            item = {'sha256': digest, 'file': 'clip.mp3', 'id': 'conversation-1'}
            with patch('qa_bot.live_session.decode_to_wav', decode), \
                 patch('qa_bot.audio.transcript_cache.natively_model_identity', return_value='fixture-model'), \
                 patch.object(NativelyLocalSTT, 'transcribe_wav', transcribe), redirect_stdout(io.StringIO()):
                results = await asyncio.gather(*(s.transcribe(item) for s in sessions))
                self.assertEqual(results, ['Verified spoken words.'] * 2)
                transcribe.assert_awaited_once()
                decode.assert_awaited_once()
                (sessions[1].prompt_dir / 'clip.mp3').write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'integrity'):
                    await sessions[1].transcribe(item)
            self.assertEqual({s.log.call_args.args[1]['cache_source'] for s in sessions}, {'transcribed', 'replay'})

    async def test_cancelling_local_stt_kills_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); wav = root / 'input.wav'; wav.write_bytes(tone_wav())
            started = asyncio.Event()
            async def communicate():
                started.set()
                await asyncio.Event().wait()
            process = SimpleNamespace(returncode=None, communicate=communicate, kill=Mock(), wait=AsyncMock())
            with patch.object(NativelyLocalSTT, 'command', return_value=('fixture',)), \
                 patch('qa_bot.adapters.stt.natively_local.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
                task = asyncio.create_task(NativelyLocalSTT(root).transcribe_wav(wav))
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            process.kill.assert_called_once()
            process.wait.assert_awaited_once()
