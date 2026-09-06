"""Inject one prepared local speech answer directly into browser getUserMedia.

The generated init script is deliberately self-contained: it is installed before
the assessment page runs, warms and verifies the WAV while the prompt is visible,
then starts an already-decoded AudioBuffer when the recording marker appears.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from qa_bot.audio.replay_bank import normalize_prompt


_REQUEST_FIELDS = (
    "question", "answer", "source_profile", "source_test", "source_question",
)


@dataclass(frozen=True)
class DirectSpeechBridge:
    bridge_url: str
    token: str
    request: dict[str, str]
    prepare_markers: tuple[str, ...] = ("listen carefully",)
    record_markers: tuple[str, ...] = ("speak now", "recording")

    def __post_init__(self) -> None:
        parsed = urlsplit(self.bridge_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.username or parsed.password or not parsed.port
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("bridge_url must be an explicit 127.0.0.1 HTTP origin")
        if not isinstance(self.token, str) or len(self.token) < 24:
            raise ValueError("bridge token must contain at least 24 characters")
        if not isinstance(self.request, dict) or set(self.request) != set(_REQUEST_FIELDS):
            raise ValueError("exact speech request fields required")
        for name in _REQUEST_FIELDS:
            value = self.request[name]
            if not isinstance(value, str) or not value.strip() or len(value) > 5000:
                raise ValueError(f"invalid {name}")
        if (not self.prepare_markers or not self.record_markers
                or any(not marker.strip() for marker in self.prepare_markers + self.record_markers)):
            raise ValueError("speech markers must be non-empty")

    def init_script(self) -> str:
        base = self.bridge_url.rstrip("/")
        payload = {name: normalize_prompt(self.request[name]) for name in _REQUEST_FIELDS}
        config = json.dumps({
            "base": base,
            "token": self.token,
            "request": payload,
            "prepareMarkers": tuple(marker.casefold() for marker in self.prepare_markers),
            "recordMarkers": tuple(marker.casefold() for marker in self.record_markers),
        }, separators=(",", ":"))
        return f"""(() => {{
  'use strict';
  const cfg = {config};
  const nativeFetch = globalThis.fetch.bind(globalThis);
  const qa = {{status: 'installed', failures: [], prepareCount: 0, audioCount: 0,
               replayCount: 0, context: null, destination: null, warmTask: null,
               decoded: null, busy: false, recordingSeenAt: null, replayStartedAt: null,
               wavSha256: null}};
  Object.defineProperty(globalThis, '__qaDirectSpeechBridge',
    {{value: qa, configurable: false, writable: false}});

  const textHas = markers => {{
    const text = (document.body && document.body.innerText || '').toLocaleLowerCase();
    return markers.some(marker => text.includes(marker));
  }};

  const ensureGraph = () => {{
    if (!qa.context) {{
      qa.context = new AudioContext();
      qa.destination = qa.context.createMediaStreamDestination();
    }}
    return qa.destination.stream;
  }};

  const resume = async () => {{
    const stream = ensureGraph();
    if (qa.context.state === 'suspended') await qa.context.resume();
    return stream;
  }};

  const post = path => nativeFetch(cfg.base + path, {{
    method: 'POST', credentials: 'omit', cache: 'no-store',
    headers: {{'Content-Type': 'application/json', 'X-Speech-Token': cfg.token}},
    body: JSON.stringify(cfg.request)
  }});

  const hex = bytes => Array.from(new Uint8Array(bytes))
    .map(value => value.toString(16).padStart(2, '0')).join('');

  const warm = () => {{
    if (qa.decoded) return Promise.resolve();
    if (qa.warmTask) return qa.warmTask;
    qa.status = 'preparing';
    ensureGraph();
    qa.warmTask = (async () => {{
      const prepared = await post('/prepare');
      if (!prepared.ok) throw new Error('prepare_http_' + prepared.status);
      const receipt = await prepared.json();
      if (!receipt || receipt.status !== 'ready') throw new Error('prepare_not_ready');
      qa.prepareCount += 1;
      const response = await post('/audio');
      if (!response.ok) throw new Error('audio_http_' + response.status);
      const expected = response.headers.get('X-Audio-SHA256');
      if (!expected || !/^[a-f0-9]{{64}}$/.test(expected)) throw new Error('audio_sha_missing');
      const encoded = await response.arrayBuffer();
      const actual = hex(await crypto.subtle.digest('SHA-256', encoded));
      if (actual !== expected) throw new Error('audio_sha_mismatch');
      qa.decoded = await qa.context.decodeAudioData(encoded.slice(0));
      qa.wavSha256 = actual;
      qa.audioCount += 1;
      qa.status = 'prepared';
    }})().catch(error => {{
      qa.status = 'failed';
      qa.failures.push(String(error && error.message || error).slice(0, 120));
      throw error;
    }});
    return qa.warmTask;
  }};

  const replay = async () => {{
    if (qa.busy || qa.replayCount > 0 || !textHas(cfg.recordMarkers)) return;
    qa.busy = true;
    if (qa.recordingSeenAt === null) qa.recordingSeenAt = performance.now();
    try {{
      await warm();
      await resume();
      const source = qa.context.createBufferSource();
      source.buffer = qa.decoded;
      source.connect(qa.destination);
      source.start(qa.context.currentTime + 0.005);
      qa.replayCount = 1;
      qa.replayStartedAt = performance.now();
      qa.status = 'replaying';
      source.addEventListener('ended', () => {{ qa.status = 'complete'; }}, {{once: true}});
    }} catch (_) {{
      // warm() records the bounded reason.
    }} finally {{ qa.busy = false; }}
  }};

  const check = () => {{
    if (textHas(cfg.prepareMarkers)) warm().catch(() => {{}});
    replay();
  }};

  const media = navigator.mediaDevices;
  if (!media || typeof media.getUserMedia !== 'function') {{
    qa.status = 'media_devices_unavailable';
    return;
  }}
  const original = media.getUserMedia.bind(media);
  media.getUserMedia = async constraints => {{
    if (!constraints || !constraints.audio) return original(constraints);
    const stream = await resume();
    queueMicrotask(check);
    return stream;
  }};

  const resumeOnGesture = () => {{
    ensureGraph();
    if (qa.context.state === 'suspended') qa.context.resume().catch(() => {{}});
  }};
  addEventListener('pointerdown', resumeOnGesture, {{capture: true}});
  addEventListener('keydown', resumeOnGesture, {{capture: true}});

  const observe = () => new MutationObserver(check).observe(document.documentElement,
    {{subtree: true, childList: true, characterData: true}});
  if (document.documentElement) observe();
  else addEventListener('DOMContentLoaded', observe, {{once: true}});
  addEventListener('DOMContentLoaded', check, {{once: true}});
  setInterval(check, 25);
}})();"""
