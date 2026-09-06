import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import wave


class NativelyLongAudioTests(unittest.TestCase):
    def run_driver(self, seconds, model='distil-whisper/distil-small.en', family='whisper'):
        driver = Path(__file__).resolve().parents[2] / 'tools/natively_local_stt.mjs'
        node = shutil.which('node') or '/Applications/Natively.app/Contents/MacOS/Natively'
        if not Path(node).is_file():
            self.skipTest('Node or Natively runtime required for driver integration')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'runtime'; (runtime / 'src').mkdir(parents=True)
            (runtime / 'package.json').write_text('{"type":"module"}')
            # Fake only the inference provider: execute the unchanged real WAV parser,
            # options construction and driver entrypoint with full-length PCM input.
            (runtime / 'src/transformers.js').write_text('''
export const env = {};
export async function pipeline() {
  const transcribe = async (audio, options) => ({text: JSON.stringify({samples: audio.length, options})});
  transcribe.model = {config: {model_type: FAMILY}};
  return transcribe;
}
'''.replace('FAMILY', json.dumps(family)))
            wav = root / 'input.wav'
            with wave.open(str(wav), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                stream.writeframes(bytes(round(seconds * 16000) * 2))
            env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
            process = subprocess.run([node, str(driver), str(wav), str(root / 'cache'), str(runtime), model],
                                     env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout.splitlines()[-1])
            self.assertEqual(result['status'], 'ok')
            return json.loads(result['text'])

    def test_long_whisper_audio_keeps_all_samples_and_enables_overlap(self):
        result = self.run_driver(32.02)
        self.assertEqual(result['samples'], round(32.02 * 16000))
        self.assertEqual(result['options']['chunk_length_s'], 30)
        self.assertEqual(result['options']['stride_length_s'], 5)

    def test_short_and_exactly_thirty_second_audio_keep_single_window(self):
        for seconds in (1, 30):
            with self.subTest(seconds=seconds):
                result = self.run_driver(seconds)
                self.assertEqual(result['samples'], seconds * 16000)
                self.assertNotIn('chunk_length_s', result['options'])
                self.assertNotIn('stride_length_s', result['options'])

    def test_other_stt_model_is_not_given_whisper_chunk_options(self):
        result = self.run_driver(32, model='onnx-community/moonshine-base-ONNX', family='moonshine')
        self.assertNotIn('chunk_length_s', result['options'])
        self.assertNotIn('stride_length_s', result['options'])
