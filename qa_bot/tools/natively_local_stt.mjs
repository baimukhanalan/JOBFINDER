#!/usr/bin/env node
// Local English STT using the Transformers/ONNX runtime bundled with Natively.
// Input must be mono PCM16 WAV at 16 kHz; output is one compact JSON object.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

function fail(message) {
  process.stdout.write(JSON.stringify({status: 'error', error: String(message).slice(0, 180)}) + '\n');
  process.exit(2);
}

function wavPcm16Mono16k(file) {
  const data = fs.readFileSync(file);
  if (data.length < 44 || data.toString('ascii', 0, 4) !== 'RIFF' ||
      data.toString('ascii', 8, 12) !== 'WAVE') fail('invalid_wav');
  let offset = 12, fmt = null, pcm = null;
  while (offset + 8 <= data.length) {
    const name = data.toString('ascii', offset, offset + 4);
    const size = data.readUInt32LE(offset + 4);
    const start = offset + 8;
    if (start + size > data.length) fail('truncated_wav');
    if (name === 'fmt ') fmt = data.subarray(start, start + size);
    if (name === 'data') { pcm = data.subarray(start, start + size); break; }
    offset = start + size + (size % 2);
  }
  if (!fmt || fmt.length < 16 || !pcm) fail('missing_wav_chunks');
  if (fmt.readUInt16LE(0) !== 1 || fmt.readUInt16LE(2) !== 1 ||
      fmt.readUInt32LE(4) !== 16000 || fmt.readUInt16LE(14) !== 16) {
    fail('wav_must_be_pcm16_mono_16khz');
  }
  const out = new Float32Array(Math.floor(pcm.length / 2));
  for (let i = 0; i < out.length; i++) out[i] = pcm.readInt16LE(i * 2) / 32768;
  return out;
}

const input = process.argv[2];
const cacheDir = process.argv[3];
const moduleRoot = process.argv[4];
const modelId = process.argv[5] || 'distil-whisper/distil-small.en';
if (!input || !cacheDir || !moduleRoot) fail('usage: input.wav cache_dir transformers_root [model]');

try {
  fs.mkdirSync(cacheDir, {recursive: true});
  const moduleUrl = pathToFileURL(path.join(moduleRoot, 'src', 'transformers.js')).href;
  const { pipeline, env } = await import(moduleUrl);
  env.cacheDir = cacheDir;
  env.allowRemoteModels = true;
  const transcriber = await pipeline('automatic-speech-recognition', modelId, {
    dtype: 'q8',
    session_options: {
      intraOpNumThreads: 1,
      interOpNumThreads: 1,
      executionMode: 'sequential',
      enableCpuMemArena: false,
      enableMemPattern: false,
    },
  });
  const audio = wavPcm16Mono16k(input);
  const generation = {
    sampling_rate: 16000,
    temperature: 0,
    condition_on_previous_text: false,
    compression_ratio_threshold: 2.4,
    logprob_threshold: -1,
    no_speech_threshold: 0.6,
  };
  if (!modelId.startsWith('distil-whisper/') && !modelId.endsWith('.en')) {
    generation.task = 'transcribe';
    generation.language = 'english';
  }
  const result = await transcriber(audio, generation);
  process.stdout.write(JSON.stringify({status: 'ok', text: String(result?.text || '').trim(),
    model: modelId, samples: audio.length}) + '\n');
} catch (error) {
  fail(error?.message || error);
}
