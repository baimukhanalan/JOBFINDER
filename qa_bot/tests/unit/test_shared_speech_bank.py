import asyncio
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock

from qa_bot.audio.merge_bank import merge_banks, SpeechBankConflict
from qa_bot.audio.replay_bank import SpeechReplayBank
from tests.unit.test_speech_replay import FakeConverter


def _concurrent_prepare(directory, number):
    root = Path(directory)
    class Speech:
        async def synthesize_mp3(self, *_args, **_kwargs):
            with (root / 'generations').open('a') as stream:
                stream.write('generated\n')
            await asyncio.sleep(.15)
            return b'ID3' + bytes(200)
    with SpeechReplayBank(root / 'speech.sqlite3', root / 'answers', converter=FakeConverter()) as bank:
        result = asyncio.run(bank.get_or_create('Same question.', 'Same answer.',
            speech=Speech(), voice='local', source_profile=f'profile-{number}',
            source_test=f'test-{number}', source_question='q1'))
        return result.audio_sha256, result.wav_sha256, result.source


def _concurrent_topic(directory, number):
    root = Path(directory)
    async def answer_factory():
        with (root / 'reasoning').open('a') as stream:
            stream.write('answered\n')
        await asyncio.sleep(.15)
        return 'The same answer from the first completed callback.'
    class Speech:
        async def synthesize_mp3(self, *_args, **_kwargs):
            with (root / 'generations').open('a') as stream:
                stream.write('generated\n')
            await asyncio.sleep(.15)
            return b'ID3' + bytes(200)
    with SpeechReplayBank(root / 'speech.sqlite3', root / 'answers', converter=FakeConverter()) as bank:
        result = asyncio.run(bank.get_or_create_spoken('Same topic.', answer_factory=answer_factory,
            speech=Speech(), voice='local', source_profile=f'profile-{number}',
            source_test=f'test-{number}', source_question='topic-28'))
        return result.answer_text, result.audio_sha256, result.wav_sha256, result.source


class SharedSpeechBankTests(unittest.TestCase):
    def bank(self, root):
        root.mkdir(exist_ok=True)
        return SpeechReplayBank(root / 'speech.sqlite3', root / 'answers', converter=FakeConverter())

    def record(self, bank, question='Question', answer='Answer', audio=None):
        return bank.record(question, answer, audio or b'ID3' + bytes(200), voice='local', model='fixture',
                           source_profile='authorized-fixture', source_test='fixture-a', source_question='q1')

    def test_ten_processes_share_one_generation_and_intact_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.bank(Path(directory)):
                pass
            with ProcessPoolExecutor(max_workers=10, mp_context=multiprocessing.get_context('spawn')) as pool:
                results = list(pool.map(_concurrent_prepare, [directory] * 10, range(10)))
            self.assertEqual((Path(directory) / 'generations').read_text().splitlines(), ['generated'])
            self.assertEqual(len({r[:2] for r in results}), 1)
            self.assertEqual(sum(r[2] == 'generated' for r in results), 1)
            with self.bank(Path(directory)) as bank:
                self.assertEqual(bank.stats(), {'answers': 1, 'reuses': 9})
                self.assertEqual(len(bank.usage()), 10)
                self.assertEqual(len({r['source_profile'] for r in bank.usage()}), 10)

    def test_ten_concurrent_topics_call_answer_factory_and_tts_once(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.bank(Path(directory)):
                pass
            with ProcessPoolExecutor(max_workers=10, mp_context=multiprocessing.get_context('spawn')) as pool:
                results = list(pool.map(_concurrent_topic, [directory] * 10, range(10)))
            self.assertEqual((Path(directory) / 'reasoning').read_text().splitlines(), ['answered'])
            self.assertEqual((Path(directory) / 'generations').read_text().splitlines(), ['generated'])
            self.assertEqual(len({r[:3] for r in results}), 1)
            self.assertEqual(sum(r[3] == 'generated' for r in results), 1)
            with self.bank(Path(directory)) as bank:
                self.assertEqual(bank.stats(), {'answers': 1, 'reuses': 9})
                missing_factory, speech = AsyncMock(), AsyncMock()
                with self.assertRaisesRegex(ValueError, 'replay-only'):
                    asyncio.run(bank.get_or_create_spoken('Unknown topic.', answer_factory=missing_factory,
                        speech=speech, voice='local', source_profile='p', source_test='t',
                        source_question='q', replay_only=True))
                missing_factory.assert_not_awaited()
                speech.synthesize_mp3.assert_not_called()

    def test_replay_only_never_synthesizes_and_miss_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory, self.bank(Path(directory)) as bank:
            expected = self.record(bank)
            speech = AsyncMock()
            args = dict(speech=speech, voice='local', source_profile='fixture',
                        source_test='fixture-b', source_question='q2', replay_only=True)
            actual = asyncio.run(bank.get_or_create('Question', 'Answer', **args))
            self.assertEqual(actual.audio_sha256, expected.audio_sha256)
            with self.assertRaisesRegex(ValueError, 'replay-only'):
                asyncio.run(bank.get_or_create('New question', 'New answer', **args))
            speech.synthesize_mp3.assert_not_called()

    def test_import_preserves_bytes_metadata_and_copied_history_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.bank(root / 'first') as bank:
                expected = self.record(bank)
                bank.replay('Question', source_profile='p2', source_test='t2', source_question='q2')
                expected_mp3 = expected.mp3_path.read_bytes()
                expected_wav = expected.wav_path.read_bytes()
            shutil.copytree(root / 'first', root / 'copy')
            with self.bank(root / 'copy') as bank:
                self.record(bank, 'Second question', 'Second answer')
                bank.replay('Question', source_profile='p3', source_test='t3', source_question='q3')
            first = merge_banks(root / 'shared', [root / 'first', root / 'copy'])
            self.assertEqual(first['imported'], 2)
            self.assertEqual(first['usage_added'], 4)
            second = merge_banks(root / 'shared', [root / 'first', root / 'copy'])
            self.assertEqual(second['imported'], 0)
            self.assertEqual(second['usage_added'], 0)
            with self.bank(root / 'shared') as bank:
                actual = bank.lookup('Question')
                self.assertEqual(actual.mp3_path.read_bytes(), expected_mp3)
                self.assertEqual(actual.wav_path.read_bytes(), expected_wav)
                self.assertEqual(actual.source_profile, expected.source_profile)
                self.assertEqual(actual.source_test, expected.source_test)
                self.assertEqual(bank.stats(), {'answers': 2, 'reuses': 2})
                self.assertEqual(len(bank.usage()), 4)

    def test_conflicting_source_rejects_entire_batch_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.bank(root / 'shared') as bank:
                expected = self.record(bank)
            with self.bank(root / 'good') as bank:
                self.record(bank, 'New question')
            with self.bank(root / 'bad') as bank:
                self.record(bank, audio=b'ID3' + bytes(201))
            with self.assertRaises(SpeechBankConflict):
                merge_banks(root / 'shared', [root / 'good', root / 'bad'])
            with self.bank(root / 'shared') as bank:
                self.assertIsNone(bank.lookup('New question'))
                self.assertEqual(bank.lookup('Question').audio_sha256, expected.audio_sha256)
                self.assertEqual(bank.stats(), {'answers': 1, 'reuses': 0})

    def test_corrupt_audio_and_path_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.bank(root / 'source') as bank:
                replay = self.record(bank)
                bank.db.execute("UPDATE speech_answers SET mp3_path='../outside.mp3'")
                bank.db.commit()
                with self.assertRaisesRegex(ValueError, 'escapes'):
                    bank.lookup('Question')
            with self.assertRaises(SpeechBankConflict):
                merge_banks(root / 'shared', [root / 'source'])
