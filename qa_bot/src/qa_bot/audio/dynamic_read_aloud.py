"""Fail-closed multi-question Read-and-Speak microphone injection."""
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DynamicReadAloudBridge:
    bridge_url: str
    token: str
    allowed_origin: str
    allowed_path_prefix: str
    source_profile: str
    source_test: str
    auto_detect: bool = False
    question_counter_selector: str = ""
    suspension_selector: str = ""
    section_heading: str = ""
    question_root_selector: str = "[data-qa-read-aloud-question]"
    sentence_selector: str = "[data-qa-read-aloud-text]"
    instruction_selector: str = "[data-qa-read-aloud-instruction]"
    phase_selector: str = "[data-qa-read-aloud-phase]"
    site_id_attribute: str = "data-question-id"
    instruction_text: str = "Read the given sentence out loud."
    record_phases: tuple[str, ...] = ("Speak Now", "Recording")
    stable_observations: int = 3
    poll_interval_ms: int = 25
    maximum_audio_seconds: float = 30.0

    def __post_init__(self) -> None:
        bridge = urlsplit(self.bridge_url)
        if (bridge.scheme != "http" or bridge.hostname != "127.0.0.1"
                or bridge.username or bridge.password or not bridge.port
                or bridge.path not in ("", "/") or bridge.query or bridge.fragment):
            raise ValueError("bridge_url must be an explicit 127.0.0.1 HTTP origin")
        page = urlsplit(self.allowed_origin)
        if (page.scheme != "https" or not page.hostname or page.path not in ("", "/")
                or page.query or page.fragment or page.username or page.password):
            raise ValueError("allowed_origin must be an explicit HTTPS origin")
        if not self.allowed_path_prefix.startswith("/"):
            raise ValueError("allowed_path_prefix must start with /")
        if type(self.auto_detect) is not bool:
            raise ValueError("auto_detect must be boolean")
        if not isinstance(self.question_counter_selector, str) or len(self.question_counter_selector) > 500:
            raise ValueError("invalid question counter selector")
        if not isinstance(self.suspension_selector, str) or len(self.suspension_selector) > 500:
            raise ValueError("invalid suspension selector")
        if not isinstance(self.section_heading,str) or len(self.section_heading)>200:
            raise ValueError("invalid section heading")
        if not isinstance(self.token, str) or len(self.token) < 24:
            raise ValueError("bridge token must contain at least 24 characters")
        for name in ("source_profile", "source_test", "question_root_selector",
                     "sentence_selector", "instruction_selector", "phase_selector",
                     "site_id_attribute", "instruction_text"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError(f"invalid {name}")
        if not self.record_phases or any(not value.strip() for value in self.record_phases):
            raise ValueError("record phases must be non-empty")
        if not 2 <= self.stable_observations <= 10:
            raise ValueError("stable_observations must be between 2 and 10")
        if not 10 <= self.poll_interval_ms <= 250:
            raise ValueError("poll_interval_ms must be between 10 and 250")
        if not 0.1 <= self.maximum_audio_seconds <= 120:
            raise ValueError("maximum_audio_seconds must be between 0.1 and 120")

    def init_script(self) -> str:
        config = json.dumps({
            "base": self.bridge_url.rstrip("/"), "token": self.token,
            "origin": self.allowed_origin.rstrip("/"), "path": self.allowed_path_prefix,
            "profile": self.source_profile.strip(), "test": self.source_test.strip(),
            "autoDetect": self.auto_detect,
            "counterSelector": self.question_counter_selector,
            "suspensionSelector": self.suspension_selector,
            "sectionHeading": self.section_heading,
            "root": self.question_root_selector, "sentence": self.sentence_selector,
            "instruction": self.instruction_selector, "phase": self.phase_selector,
            "siteId": self.site_id_attribute,
            "instructionText": self.instruction_text.strip(),
            "recordPhases": tuple(value.strip() for value in self.record_phases),
            "stable": self.stable_observations, "poll": self.poll_interval_ms,
            "maxAudio": self.maximum_audio_seconds,
        }, separators=(",", ":"))
        return f"""(() => {{
  'use strict';
  const cfg = {config};
  if (globalThis.top !== globalThis || location.origin !== cfg.origin ||
      !location.pathname.startsWith(cfg.path)) return;
  const nativeFetch = globalThis.fetch.bind(globalThis);
  const qa = {{status: 'installed', failures: [], prepareCount: 0, audioCount: 0,
               replayCount: 0, graphCreations: 0, epoch: 0, currentKey: null,
               currentSiteId: null, recordingSeenAt: null, replayStartedAt: null,
               recordingUnpreparedCount: 0, retryCount: 0, transitions: []}};
  Object.defineProperty(globalThis, '__qaDynamicReadAloud',
    {{value: qa, configurable: false, writable: false}});

  const normalize = value => String(value || '').normalize('NFC').replace(/\\s+/g, ' ').trim();
  const visible = node => {{
    if (!node || !node.isConnected || node.closest('[hidden],[aria-hidden="true"],template')) return false;
    const style = getComputedStyle(node);
    return node.getClientRects().length > 0 && style.display !== 'none' &&
      style.visibility !== 'hidden' && style.opacity !== '0';
  }};
  const exactVisible = (root, selector) => {{
    let nodes;
    try {{ nodes = Array.from(root.querySelectorAll(selector)).filter(visible); }}
    catch (_) {{ return {{error: 'invalid_selector'}}; }}
    if (nodes.length !== 1) return {{error: nodes.length ? 'ambiguous_node' : 'missing_node'}};
    return {{node: nodes[0], text: normalize(nodes[0].innerText || nodes[0].textContent)}};
  }};
  const report = reason => {{
    const bounded = String(reason).slice(0, 100);
    if (!qa.failures.includes(bounded) && qa.failures.length < 20) qa.failures.push(bounded);
    qa.status = 'blocked';
  }};
  const transition = (epoch, state) => {{
    qa.status = state;
    qa.transitions.push({{epoch, state, at: performance.now()}});
    if (qa.transitions.length > 40) qa.transitions.shift();
  }};

  let context = null;
  let destination = null;
  let active = null;
  let candidateSignature = null;
  let candidateCount = 0;
  let candidateSeenAt = 0;
  let previousRecording = false;
  let recordingCandidate = null;
  let recordingCandidateAt = 0;

  const ensureGraph = () => {{
    if (!context) {{
      const shared = globalThis.__qaMicrophoneBus;
      context = shared ? shared.context : new AudioContext();
      destination = shared ? shared.destination : context.createMediaStreamDestination();
      qa.graphCreations += 1;
    }}
    return destination.stream;
  }};
  const resume = async () => {{
    const stream = ensureGraph();
    if (context.state === 'suspended') await context.resume();
    return stream;
  }};
  const stopActive = () => {{
    if (!active) return;
    active.abort.abort();
    if (active.source) {{
      try {{ active.source.stop(); }} catch (_) {{}}
      try {{ active.source.disconnect(); }} catch (_) {{}}
    }}
    active = null;
    qa.currentKey = null;
    qa.currentSiteId = null;
  }};

  const inspectExplicit = () => {{
    let roots;
    try {{ roots = Array.from(document.querySelectorAll(cfg.root)).filter(visible); }}
    catch (_) {{ return {{error: 'invalid_root_selector'}}; }}
    if (roots.length !== 1) return {{error: roots.length ? 'ambiguous_question_root' : 'missing_question_root'}};
    const root = roots[0];
    const instruction = exactVisible(root, cfg.instruction);
    const sentence = exactVisible(root, cfg.sentence);
    if (sentence.error) return {{error: sentence.error}};
    if (!sentence.text || sentence.text.length > 1000) return {{error: 'invalid_sentence'}};
    const siteId = normalize(root.getAttribute(cfg.siteId));
    if (!siteId || siteId.length > 200) return {{error: 'missing_site_question_id'}};
    const phase = exactVisible(root, cfg.phase);
    const recording = !phase.error && cfg.recordPhases.includes(phase.text);
    if (!recording && (instruction.error || instruction.text !== cfg.instructionText))
      return {{error: instruction.error || 'instruction_mismatch'}};
    return {{root, sentence: sentence.text, siteId, recording}};
  }};

  const leafBlocks = () => Array.from(document.querySelectorAll(
    'p,div,span,h1,h2,h3,h4,blockquote,li,label'))
    .filter(visible).filter(node => {{
      if (node.closest('button,a,nav,header,footer,[role="button"],[role="navigation"]')) return false;
      if (cfg.suspensionSelector && node.closest(cfg.suspensionSelector)) return false;
      const text = normalize(node.innerText || node.textContent);
      if (!text) return false;
      return !Array.from(node.children).some(child =>
        visible(child) && normalize(child.innerText || child.textContent));
    }});
  const isCounter = text => /^(?:(?:question|q)\\s*)?\\d+\\s*(?:of|\\/)\\s*\\d+$/i.test(text);
  const autoExcluded = text => {{
    const lowered = text.toLocaleLowerCase();
    if (text === cfg.instructionText || cfg.recordPhases.includes(text)) return true;
    if (isCounter(text)) return true;
    if (/^(?:section|part)\\s+[a-z0-9]+(?:\\s*[:.\\-].*)?$/i.test(text)) return true;
    if (/^\\d{{1,2}}:\\d{{2}}$/.test(text)) return true;
    if (/^(?:remaining time|time left)\\s*:?(?:\\s*\\d{{1,2}}:\\d{{2}})?$/i.test(text)) return true;
    if (/^\\d+\\s*(?:seconds?|secs?|s)$/i.test(text)) return true;
    return ['submit', 'submit answer', 'next', 'back', 'skip', 'listen carefully',
      'prepare', 'get ready', 'time left', 'help', 'exit', 'replay', 'record again',
      'play recording', 'connection', 'audio', 'question audio', 'warning', 'or',
      'ok', 'yes', 'no', 'try again', 'try later'].includes(lowered);
  }};
  const inspectAuto = () => {{
    const blocks = leafBlocks().map(node =>
      ({{node, text: normalize(node.innerText || node.textContent)}}));
    const instructions = blocks.filter(item => item.text === cfg.instructionText);
    const phases = blocks.filter(item => cfg.recordPhases.includes(item.text));
    if (instructions.length > 1) return {{error: 'ambiguous_instruction'}};
    if (phases.length > 1) return {{error: 'ambiguous_record_phase'}};
    const recording = phases.length === 1;
    if (!recording && instructions.length !== 1)
      return {{error: instructions.length ? 'ambiguous_instruction' : 'missing_instruction'}};
    const sentences = blocks.filter(item => {{
      if (autoExcluded(item.text)) return false;
      if (!item.text.length || item.text.length > 1000) return false;
      const words = item.text.match(/\\p{{L}}+(?:['’\\-]\\p{{L}}+)?/gu) || [];
      if (!words.length) return false;
      if (words.length >= 4) return true;
      // Short utterances are valid questions. Require their local instruction
      // context; a phase-only screen can retain only its already armed sentence.
      if (/^H[1-6]$/.test(item.node.tagName)) return false;
      if (instructions.length === 1) {{
        const anchor=instructions[0].node;
        if (anchor.parentElement?.contains(item.node)) return true;
        const area=anchor.closest('section,article,main,[role="main"]')||document.body;
        if (area.contains(item.node) && /[.!?…]$/.test(item.text) &&
            (anchor.compareDocumentPosition(item.node)&Node.DOCUMENT_POSITION_FOLLOWING)) return true;
      }}
      return Boolean(active && active.sentence === item.text && active.root.contains(item.node));
    }});
    if (sentences.length !== 1)
      return {{error: sentences.length ? 'ambiguous_sentence' : 'missing_sentence'}};
    const sentence = sentences[0];
    const counters = blocks.filter(item => isCounter(item.text));
    let siteId = '';
    for (let node = sentence.node; node && node !== document.body; node = node.parentElement) {{
      siteId = normalize(node.getAttribute && node.getAttribute(cfg.siteId));
      if (siteId) break;
    }}
    if (!siteId) {{
      if (counters.length > 1) return {{error: 'ambiguous_question_counter'}};
      if (counters.length === 1) siteId = 'counter:' + counters[0].text;
      else if (cfg.counterSelector) {{
        const counter = exactVisible(document, cfg.counterSelector);
        if (counter.error) return {{error: 'question_counter_' + counter.error}};
        if (!/^[1-9]\\d*$/.test(counter.text)) return {{error: 'invalid_question_counter'}};
        siteId = 'navigation:' + counter.text;
      }} else return {{error: 'missing_question_counter'}};
    }}
    const root = sentence.node.closest('section,article,main,[role="main"]') || document.body;
    return {{root, sentence: sentence.text, siteId, recording}};
  }};
  const inspect = () => cfg.autoDetect ? inspectAuto() : inspectExplicit();
  // Explicit recovery is only available while a configured suspension dialog
  // is present, after a completed/missed take of the same connected question.
  const retriedSignatures = new Set();
  qa.retryCurrent = () => {{
    if (!cfg.suspensionSelector || !document.querySelector(cfg.suspensionSelector) ||
        !active || !active.root.isConnected || !active.decoded || active.source ||
        retriedSignatures.has(active.signature) ||
        (!['played', 'missed'].includes(active.status) && !active.retryPrepared)) return false;
    retriedSignatures.add(active.signature);
    active.retryPrepared = false;
    active.played = false;
    active.status = 'armed';
    previousRecording = false;
    recordingCandidate = null;
    qa.retryCount += 1;
    transition(active.epoch, 'armed');
    return true;
  }};
  const autoTargetPresent = () => cfg.autoDetect && leafBlocks().some(item => {{
    const text = normalize(item.innerText || item.textContent);
    return text === cfg.instructionText || cfg.recordPhases.includes(text);
  }});

  const sha = async value => {{
    const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
    return Array.from(new Uint8Array(bytes)).map(v => v.toString(16).padStart(2, '0')).join('');
  }};
  const post = (path, request, signal) => nativeFetch(cfg.base + path, {{
    method: 'POST', credentials: 'omit', cache: 'no-store', signal,
    headers: {{'Content-Type': 'application/json', 'X-Speech-Token': cfg.token}},
    body: JSON.stringify(request)
  }});

  const arm = async observation => {{
    const signature = observation.siteId + '\\0' + observation.sentence;
    if (active && active.signature === signature && !['failed','missed'].includes(active.status)) return active.warmTask;
    if (active) stopActive();
    const epoch = ++qa.epoch;
    const abort = new AbortController();
    const state = {{epoch, signature, sentence: observation.sentence, siteId: observation.siteId, root: observation.root,
                   abort, status: 'preparing', decoded: null, source: null,
                   played: false, warmTask: null}};
    active = state;
    transition(epoch, 'preparing');
    state.warmTask = (async () => {{
      const key = await sha(signature);
      const request = {{question: observation.sentence, answer: observation.sentence,
        source_profile: cfg.profile, source_test: cfg.test,
        source_question: 'read-aloud-' + key.slice(0, 24)}};
      if (active !== state || abort.signal.aborted) return;
      ensureGraph();
      const prepared = await post('/prepare', request, abort.signal);
      if (!prepared.ok) throw new Error('prepare_http_' + prepared.status);
      const receipt = await prepared.json();
      if (!receipt || receipt.status !== 'ready') throw new Error('prepare_not_ready');
      qa.prepareCount += 1;
      const audio = await post('/audio', request, abort.signal);
      if (!audio.ok) throw new Error('audio_http_' + audio.status);
      const expected = audio.headers.get('X-Audio-SHA256');
      if (!expected || !/^[a-f0-9]{{64}}$/.test(expected)) throw new Error('audio_sha_missing');
      const encoded = await audio.arrayBuffer();
      const actual = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', encoded)))
        .map(v => v.toString(16).padStart(2, '0')).join('');
      if (actual !== expected) throw new Error('audio_sha_mismatch');
      const decoded = await context.decodeAudioData(encoded.slice(0));
      if (!(decoded.duration > 0) || decoded.duration > cfg.maxAudio) throw new Error('audio_duration_invalid');
      if (active !== state || abort.signal.aborted || !state.root.isConnected) return;
      state.decoded = decoded;
      state.status = 'armed';
      qa.audioCount += 1;
      qa.currentKey = key;
      qa.currentSiteId = state.siteId;
      transition(epoch, 'armed');
    }})().catch(error => {{
      if (error && error.name === 'AbortError') return;
      if (active === state) {{ state.status = 'failed'; report(error && error.message || error); }}
    }});
    return state.warmTask;
  }};

  qa.retrySignature = () => {{
    const observation=inspect();
    return observation.error?null:observation.siteId+'\\0'+observation.sentence;
  }};
  qa.prepareRetryCurrent = async () => {{
    if (!cfg.suspensionSelector) return false;
    const dialogs=Array.from(document.querySelectorAll(cfg.suspensionSelector)).filter(visible);
    if (dialogs.length!==1 || !normalize(dialogs[0].innerText||dialogs[0].textContent).includes('We are unable to hear you.')) return false;
    const observation=inspect();
    if (observation.error) return false;
    const signature=observation.siteId+'\\0'+observation.sentence;
    if (retriedSignatures.has(signature)) return false;
    if (!active || active.signature!==signature || !active.decoded) await arm(observation);
    const fresh=inspect();
    if (fresh.error || fresh.siteId!==observation.siteId || fresh.sentence!==observation.sentence ||
        !dialogs[0].isConnected || !active || active.signature!==signature || !active.decoded || active.source) return false;
    active.retryPrepared=true;
    return true;
  }};

  const replay = observation => {{
    if (!active || active.played || active.status !== 'armed' ||
        active.root !== observation.root || active.siteId !== observation.siteId ||
        active.signature !== observation.siteId + '\\0' + observation.sentence) return;
    if (context.state !== 'running') {{ active.status = 'failed'; report('audio_context_not_running'); return; }}
    const bus=globalThis.__qaMicrophoneBus;
    let startTime=context.currentTime+0.005;
    if(bus?.recorderTrackingEnabled) {{
      const recording=bus.recording;
      if(!recording?.active || recording.siteId!==observation.siteId) return;
      // The label can precede the recorder's real start. Keep the first phoneme
      // clear of recorder startup/encoder preroll without changing cached audio.
      startTime=Math.max(startTime,recording.contextTime+0.120);
      const remaining=normalize(document.body.innerText).match(/Remaining Time\\s*:\\s*(\\d{{1,2}}):(\\d{{2}})/i);
      if(remaining && startTime-context.currentTime+active.decoded.duration+0.05>Number(remaining[1])*60+Number(remaining[2])) {{
        active.status='missed';report('recording_window_too_short');return;
      }}
    }}
    active.played = true;
    qa.recordingSeenAt = performance.now();
    const source = context.createBufferSource();
    active.source = source;
    source.buffer = active.decoded;
    source.connect(destination);
    source.start(startTime);
    qa.replayStartedAt = performance.now();
    qa.replayCount += 1;
    transition(active.epoch, 'recording');
    source.addEventListener('ended', () => {{
      if (active && active.source === source) {{
        active.source = null;
        active.status = 'played';
        transition(active.epoch, 'played');
      }}
    }}, {{once: true}});
  }};

  const check = () => {{
    if (cfg.suspensionSelector) {{
      let suspended;
      try {{ suspended = document.querySelector(cfg.suspensionSelector) !== null; }}
      catch (_) {{ report('invalid_suspension_selector'); return; }}
      if (suspended) {{
        // A diagnostic dialog temporarily hides the prepared question. Keep
        // its buffer but never play behind the dialog or count its stale phase.
        previousRecording = false;
        recordingCandidate = null;
        return;
      }}
    }}
    if (cfg.sectionHeading && !Array.from(document.querySelectorAll('h1,h2,[role="heading"]'))
        .some(node=>visible(node)&&normalize(node.textContent)===cfg.sectionHeading)) {{
      if (active && !active.root.isConnected) stopActive();qa.status='idle';previousRecording=false;recordingCandidate=null;return;
    }}
    const observation = inspect();
    if (observation.error || !observation.recording) recordingCandidate = null;
    if (observation.error) {{
      if (cfg.autoDetect && observation.error === 'missing_instruction') {{
        // Dialog transitions can hide the question before mounting the dialog.
        // Keep the armed buffer until a new full identity replaces it; a missing
        // instruction alone never authorizes playback of that buffer.
        if (active && !active.root.isConnected) stopActive();
        qa.status = 'idle';
        candidateSignature = null; candidateCount = 0; candidateSeenAt = 0;
        previousRecording = false;
        return;
      }}
      if (observation.error !== 'missing_question_root') {{ stopActive(); report(observation.error); }}
      else if (active && !active.root.isConnected) stopActive();
      candidateSignature = null; candidateCount = 0; candidateSeenAt = 0; previousRecording = false;
      return;
    }}
    const signature = observation.siteId + '\\0' + observation.sentence;
    if(active?.source && (active.signature!==signature || active.root!==observation.root)) stopActive();
    if (signature === candidateSignature) candidateCount += 1;
    else {{ candidateSignature = signature; candidateCount = 1; candidateSeenAt = performance.now(); }}
    const stableFor = performance.now() - candidateSeenAt;
    if (!observation.recording && candidateCount >= cfg.stable &&
        stableFor >= (cfg.stable - 1) * cfg.poll &&
        (!active || active.signature !== signature)) arm(observation);
    if (observation.recording && !previousRecording) {{
      if (recordingCandidate !== signature) {{
        recordingCandidate = signature;
        recordingCandidateAt = performance.now();
      }}
      const matches = active && active.root === observation.root &&
        active.siteId === observation.siteId && active.signature === signature;
      if (!matches || active.status !== 'armed') {{
        // SPA templates briefly show a stale recording label while mounting.
        // Never play unprepared audio, but allow that label to settle before
        // cancelling an in-flight preparation. Ready audio still starts at once.
        if (performance.now() - recordingCandidateAt < 150) return;
        qa.recordingUnpreparedCount += 1;
        if (active) {{
          active.status = 'missed';
          active.abort.abort();
        }}
        report('recording_unprepared');
      }} else {{
        replay(observation);
        // A visible recording label alone is not enough. Poll until the
        // matching native recorder start arrives before consuming this phase.
        if(active && active.status==='armed' && !active.played)return;
      }}
    }}
    previousRecording = observation.recording;
  }};

  const media = navigator.mediaDevices;
  if (!media || typeof media.getUserMedia !== 'function') {{ report('media_devices_unavailable'); return; }}
  const original = media.getUserMedia.bind(media);
  media.getUserMedia = async constraints => {{
    if (!constraints || !constraints.audio) return original(constraints);
    if (globalThis.__qaMicrophoneBus) return original(constraints);
    const observation = inspect();
    let targetPresent = false;
    try {{ targetPresent = cfg.autoDetect ? autoTargetPresent() :
      document.querySelector(cfg.root) !== null; }} catch (_) {{}}
    if (observation.error) {{
      if (!targetPresent) return original(constraints);
      throw new DOMException('Read-aloud question is ambiguous', 'NotReadableError');
    }}
    if (!active || active.status === 'failed') throw new DOMException('Read-aloud bridge is not armed', 'NotReadableError');
    return resume();
  }};
  const resumeOnGesture = () => {{
    ensureGraph();
    if (context.state === 'suspended') context.resume().catch(() => {{}});
  }};
  addEventListener('pointerdown', resumeOnGesture, {{capture: true}});
  addEventListener('keydown', resumeOnGesture, {{capture: true}});

  const observe = () => new MutationObserver(check).observe(document.documentElement,
    {{subtree: true, childList: true, characterData: true, attributes: true,
      attributeFilter: ['class', 'style', 'hidden', 'aria-hidden', cfg.siteId]}});
  if (document.documentElement) observe();
  else addEventListener('DOMContentLoaded', observe, {{once: true}});
  addEventListener('DOMContentLoaded', check, {{once: true}});
  setInterval(check, cfg.poll);
}})();"""
