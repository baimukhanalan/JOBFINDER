"""Replay a test page's own prompt audio into its microphone stream.

This exact path is intended for listen-and-repeat QA. It does not transcribe or
resynthesize the sentence, so there is no model drift. The script is opt-in,
installed before navigation, and only reacts to allowlisted audio paths while
the page visibly says that recording has started.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DirectAudioLoopback:
    allowed_hosts: tuple[str, ...] = ("qbdata-amcat.s3.amazonaws.com",)
    path_markers: tuple[str, ...] = ("/SpeechAssessmentBank/",)
    record_markers: tuple[str, ...] = ("speak now", "recording")
    maximum_replays: int = 100
    maximum_prepare_attempts: int = 3
    retry_base_ms: int = 250
    failure_history_limit: int = 20
    section_heading: str = ""
    capture_bridge_url: str | None = None
    capture_token: str | None = None
    source_profile: str | None = None
    source_test: str | None = None

    def __post_init__(self) -> None:
        if not self.allowed_hosts or any(not h or "/" in h for h in self.allowed_hosts):
            raise ValueError("allowed_hosts must contain hostnames")
        if not self.path_markers or any(not p.startswith("/") for p in self.path_markers):
            raise ValueError("path_markers must be absolute path fragments")
        if not self.record_markers or any(not m.strip() for m in self.record_markers):
            raise ValueError("record_markers must be non-empty")
        if not isinstance(self.section_heading, str) or len(self.section_heading) > 200:
            raise ValueError("invalid section heading")
        if type(self.maximum_replays) is not int or not 1 <= self.maximum_replays <= 1000:
            raise ValueError("maximum_replays must be between 1 and 1000")
        if (type(self.maximum_prepare_attempts) is not int
                or not 1 <= self.maximum_prepare_attempts <= 10):
            raise ValueError("maximum_prepare_attempts must be between 1 and 10")
        if type(self.retry_base_ms) is not int or not 10 <= self.retry_base_ms <= 10_000:
            raise ValueError("retry_base_ms must be between 10 and 10000")
        if type(self.failure_history_limit) is not int or not 1 <= self.failure_history_limit <= 100:
            raise ValueError("failure_history_limit must be between 1 and 100")
        capture_values = (
            self.capture_bridge_url, self.capture_token, self.source_profile, self.source_test)
        if any(value is not None for value in capture_values):
            if any(not isinstance(value, str) or not value.strip() for value in capture_values):
                raise ValueError("all prompt capture settings are required together")
            parsed = urlsplit(self.capture_bridge_url)
            if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                    or parsed.username or parsed.password or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment):
                raise ValueError("capture_bridge_url must be an explicit 127.0.0.1 origin")
            if len(self.capture_token) < 24:
                raise ValueError("capture token must contain at least 24 characters")
            if any(len(value) > 200 for value in (self.source_profile, self.source_test)):
                raise ValueError("prompt capture source identifiers are too long")

    def init_script(self) -> str:
        config = json.dumps({
            "hosts": self.allowed_hosts,
            "paths": self.path_markers,
            "markers": tuple(m.casefold() for m in self.record_markers),
            "maximum": self.maximum_replays,
            "maximumPrepareAttempts": self.maximum_prepare_attempts,
            "retryBaseMs": self.retry_base_ms,
            "failureHistoryLimit": self.failure_history_limit,
            "sectionHeading": self.section_heading,
            "capture": ({
                "base": self.capture_bridge_url.rstrip("/"),
                "token": self.capture_token,
                "profile": self.source_profile,
                "test": self.source_test,
            } if self.capture_bridge_url else None),
        }, separators=(",", ":"))
        return f"""(() => {{
  'use strict';
  const cfg = {config};
  const nativeFetch = globalThis.fetch.bind(globalThis);
  const NativeXHR = globalThis.XMLHttpRequest;
  const qa = {{status: 'installed', replayCount: 0, failures: [], played: new Set(),
               context: null, destination: null, preparing: new Map(), prepared: new Map(),
               retries: new Map(), prepareAttempts: 0,
               xhrFallbackCount: 0,
               missed: new Set(), recordingUnpreparedCount: 0,
               active: null, busy: false, recordingSeenAt: null, replayStartedAt: null,
               captureCount: 0, captureFailures: [], captureStarted: new Set()}};
  Object.defineProperty(globalThis, '__qaDirectAudioLoopback', {{value: qa, configurable: false}});

  const safePath = raw => {{
    try {{
      const u = new URL(raw, location.href);
      if (!cfg.hosts.includes(u.hostname)) return null;
      if (!cfg.paths.some(part => u.pathname.includes(part))) return null;
      if (!/\\.(mp3|wav|ogg|m4a)$/i.test(u.pathname)) return null;
      return {{url: u.href, key: u.hostname + u.pathname,
               source: u.origin + u.pathname, extension: u.pathname.split('.').pop().toLowerCase()}};
    }} catch (_) {{ return null; }}
  }};

  const latestAudio = () => {{
    const entries = performance.getEntriesByType('resource');
    for (let i = entries.length - 1; i >= 0; i--) {{
      const found = safePath(entries[i].name);
      if (found) return found;
    }}
    return null;
  }};

  const elementAudio = () => {{
    const nodes = document.querySelectorAll('audio[src],audio source[src]');
    for (let i = nodes.length - 1; i >= 0; i--) {{
      const node = nodes[i];
      const found = safePath(node.currentSrc || node.src || node.getAttribute('src'));
      if (found) return found;
    }}
    return null;
  }};

  const audioFromNode = node => {{
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return null;
    if (node.matches && node.matches('audio[src],source[src]')) {{
      const found = safePath(node.currentSrc || node.src || node.getAttribute('src'));
      if (found) return found;
    }}
    if (node.querySelectorAll) {{
      const nested = node.querySelectorAll('audio[src],audio source[src]');
      for (let i = nested.length - 1; i >= 0; i--) {{
        const found = safePath(nested[i].currentSrc || nested[i].src || nested[i].getAttribute('src'));
        if (found) return found;
      }}
    }}
    return null;
  }};

  const visible = node => {{
    if (!node || !node.isConnected || node.closest('[hidden],[aria-hidden="true"],template'))
      return false;
    const style = getComputedStyle(node);
    return node.getClientRects().length > 0 && style.display !== 'none' &&
      style.visibility !== 'hidden' && style.opacity !== '0';
  }};
  const phaseMatches = (text, marker) => {{
    if (text === marker) return true;
    // Permit a timer attached to the phase label, but never an instruction or
    // diagnostic sentence that merely contains the word "recording".
    const suffix = text.slice(marker.length).trim();
    return text.startsWith(marker) && /^(?:[:\\-]\\s*)?\\d{{1,2}}:\\d{{2}}$/.test(suffix);
  }};
  const recordingVisible = () => {{
    if (!document.body) return false;
    if (cfg.sectionHeading && !Array.from(document.querySelectorAll('h1,h2,h3,[role="heading"]'))
        .some(node => visible(node) && node.textContent.trim() === cfg.sectionHeading)) return false;
    const nodes = [document.body, ...document.body.querySelectorAll('*')];
    return nodes.some(node => {{
      if (!visible(node)) return false;
      const visibleChildren = Array.from(node.children || []).filter(visible);
      if (visibleChildren.length) return false;
      const text = String(node.innerText || node.textContent || '')
        .normalize('NFC').replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
      return cfg.markers.some(marker => phaseMatches(text, marker));
    }});
  }};

  const rememberFailure = (history, error) => {{
    if (history.length >= cfg.failureHistoryLimit) return;
    history.push(String(error && error.message || error).slice(0, 120));
  }};

  const ensureGraph = () => {{
    if (!qa.context) {{
      const shared = globalThis.__qaMicrophoneBus;
      qa.context = shared ? shared.context : new AudioContext();
      qa.destination = shared ? shared.destination : qa.context.createMediaStreamDestination();
    }}
    return qa.destination.stream;
  }};

  const ensureAudio = async () => {{
    const stream = ensureGraph();
    if (qa.context.state === 'suspended') await qa.context.resume();
    return stream;
  }};

  const capturePrepared = async (item, encoded) => {{
    if (!cfg.capture || qa.captureStarted.has(item.key)) return;
    qa.captureStarted.add(item.key);
    try {{
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest(
        'SHA-256', new TextEncoder().encode(item.key))))
        .map(value => value.toString(16).padStart(2, '0')).join('');
      const types = {{mp3: 'audio/mpeg', wav: 'audio/wav', ogg: 'audio/ogg', m4a: 'audio/mp4'}};
      const response = await nativeFetch(cfg.capture.base + '/capture-prompt', {{
        method: 'POST', credentials: 'omit', cache: 'no-store',
        headers: {{'Content-Type': types[item.extension], 'X-Prompt-Token': cfg.capture.token,
          'X-Prompt-Source': item.source, 'X-Source-Profile': cfg.capture.profile,
          'X-Source-Test': cfg.capture.test,
          'X-Source-Question': 'listen-repeat-' + digest.slice(0, 24)}},
        body: encoded
      }});
      if (!response.ok) throw new Error('prompt_capture_http_' + response.status);
      qa.captureCount += 1;
    }} catch (error) {{
      rememberFailure(qa.captureFailures, error);
    }}
  }};

  const xhrArrayBuffer = url => new Promise((resolve, reject) => {{
    if (typeof NativeXHR !== 'function') return reject(new Error('xhr_unavailable'));
    const request = new NativeXHR();
    request.open('GET', url, true);
    request.responseType = 'arraybuffer';
    request.timeout = 10000;
    request.withCredentials = false;
    request.onload = () => {{
      if (request.status >= 200 && request.status < 300 && request.response)
        resolve(request.response);
      else reject(new Error('xhr_http_' + request.status));
    }};
    request.onerror = () => reject(new Error('xhr_network_error'));
    request.ontimeout = () => reject(new Error('xhr_timeout'));
    request.onabort = () => reject(new Error('xhr_aborted'));
    request.send();
  }});

  const loadArrayBuffer = async item => {{
    try {{
      const response = await nativeFetch(item.url, {{cache: 'force-cache', credentials: 'omit'}});
      if (!response.ok) throw new Error('audio_http_' + response.status);
      return await response.arrayBuffer();
    }} catch (fetchError) {{
      try {{
        const encoded = await xhrArrayBuffer(item.url);
        qa.xhrFallbackCount += 1;
        return encoded;
      }} catch (xhrError) {{
        throw new Error('audio_transport_failed:' +
          String(xhrError && xhrError.message || xhrError).slice(0, 80));
      }}
    }}
  }};

  const prepare = item => {{
    if (!item || qa.prepared.has(item.key)) return Promise.resolve(item);
    if (qa.preparing.has(item.key)) return qa.preparing.get(item.key);
    qa.active = item;
    const retry = qa.retries.get(item.key) || {{attempts: 0, nextAttemptAt: 0, terminal: false}};
    qa.retries.set(item.key, retry);
    if (retry.terminal || performance.now() < retry.nextAttemptAt) return Promise.resolve(null);
    retry.attempts += 1;
    qa.prepareAttempts += 1;
    qa.status = 'preparing';
    const task = (async () => {{
      ensureGraph();
      const encoded = await loadArrayBuffer(item);
      const decoded = await qa.context.decodeAudioData(encoded.slice(0));
      qa.prepared.set(item.key, {{item, decoded}});
      qa.retries.delete(item.key);
      qa.status = 'prepared';
      // Persistence is intentionally detached from preparation/replay timing.
      setTimeout(() => capturePrepared(item, encoded.slice(0)), 0);
      return item;
    }})().catch(error => {{
      retry.terminal = retry.attempts >= cfg.maximumPrepareAttempts;
      retry.nextAttemptAt = performance.now() +
        cfg.retryBaseMs * Math.pow(2, retry.attempts - 1);
      qa.status = retry.terminal ? 'failed' : 'retry_wait';
      rememberFailure(qa.failures, error);
      return null;
    }}).finally(() => qa.preparing.delete(item.key));
    qa.preparing.set(item.key, task);
    return task;
  }};

  const warmLatest = () => {{
    const item = elementAudio() || latestAudio();
    if (item) prepare(item);
  }};

  const observeAudioMutations = records => {{
    for (const record of records) {{
      const direct = audioFromNode(record.target);
      if (direct) {{ prepare(direct); continue; }}
      for (const node of record.addedNodes || []) {{
        const added = audioFromNode(node);
        if (added) {{ prepare(added); break; }}
      }}
    }}
    warmLatest();
    maybeReplay();
  }};

  const maybeReplay = async () => {{
    const recording = recordingVisible();
    if (!recording) {{
      // Each question owns a separate timing window.  Reset the observation
      // timestamp as soon as the page leaves that window so later questions
      // are measured from their own Speak Now transition.
      qa.recordingSeenAt = null;
      qa.replayStartedAt = null;
      return;
    }}
    if (qa.busy || qa.replayCount >= cfg.maximum) return;
    if (qa.recordingSeenAt === null) qa.recordingSeenAt = performance.now();
    const item = qa.active || elementAudio() || latestAudio();
    if (!item || qa.played.has(item.key) || qa.missed.has(item.key)) return;
    const cached = qa.prepared.get(item.key);
    if (!cached) {{
      // Never start late inside a short recording window.  Preparation may
      // continue for evidence or a later page, but this question is marked as
      // missed and requires a controlled retry instead of delayed playback.
      qa.missed.add(item.key);
      qa.recordingUnpreparedCount += 1;
      qa.status = 'recording_unprepared';
      rememberFailure(qa.failures, 'recording_unprepared');
      prepare(item);
      return;
    }}
    qa.busy = true;
    try {{
      await ensureAudio();
      const source = qa.context.createBufferSource();
      source.buffer = cached.decoded;
      source.connect(qa.destination);
      source.start(qa.context.currentTime + 0.01);
      qa.played.add(item.key);
      qa.replayCount += 1;
      qa.replayStartedAt = performance.now();
      qa.status = 'replaying';
      source.addEventListener('ended', () => {{ qa.status = 'ready'; }}, {{once: true}});
    }} catch (error) {{
      qa.status = 'failed';
      rememberFailure(qa.failures, error);
    }} finally {{ qa.busy = false; }}
  }};

  const media = navigator.mediaDevices;
  if (!media || typeof media.getUserMedia !== 'function') {{
    qa.status = 'media_devices_unavailable';
    return;
  }}
  const original = media.getUserMedia.bind(media);
  media.getUserMedia = async constraints => {{
    if (!constraints || !constraints.audio) return original(constraints);
    if (globalThis.__qaMicrophoneBus) return original(constraints);
    const stream = await ensureAudio();
    queueMicrotask(maybeReplay);
    return stream;
  }};

  // Resume during the browser's real user-activation event, before the page's
  // asynchronous Record handler can lose that activation.
  const resumeOnGesture = () => {{
    ensureGraph();
    if (qa.context.state === 'suspended') qa.context.resume().catch(() => {{}});
  }};
  addEventListener('pointerdown', resumeOnGesture, {{capture: true}});
  addEventListener('keydown', resumeOnGesture, {{capture: true}});

  new PerformanceObserver(() => {{ warmLatest(); maybeReplay(); }}).observe({{entryTypes: ['resource']}});
  const startObserver = () => new MutationObserver(observeAudioMutations).observe(
    document.documentElement, {{subtree: true, childList: true, characterData: true,
                               attributes: true, attributeFilter: ['src']}});
  if (document.documentElement) startObserver();
  else addEventListener('DOMContentLoaded', startObserver, {{once: true}});
  addEventListener('DOMContentLoaded', warmLatest, {{once: true}});
  setInterval(() => {{ warmLatest(); maybeReplay(); }}, 50);
}})();"""
