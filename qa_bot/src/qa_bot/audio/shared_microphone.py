"""One opt-in virtual microphone for every section of an authorized QA session."""
from dataclasses import dataclass
import json
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SharedMicrophoneBridge:
    allowed_origin: str
    idle_floor: float = 0

    def __post_init__(self):
        parsed = urlsplit(self.allowed_origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.path
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("an exact HTTPS origin is required")
        if not 0<=self.idle_floor<=0.002:
            raise ValueError('idle noise floor must be between 0 and 0.002')

    def init_script(self):
        origin = json.dumps(self.allowed_origin)
        return f"""(() => {{
  'use strict';
  if (location.origin !== {origin} || globalThis.top !== globalThis ||
      globalThis.__qaMicrophoneBus) return;
  const context = new AudioContext();
  const destination = context.createMediaStreamDestination();
  const analyser = context.createAnalyser();
  analyser.fftSize = 2048;
  const input = context.createMediaStreamSource(destination.stream);
  input.connect(analyser);
  const bus = {{context, destination, analyser, input, consumers: [input], streamsRequested: 0, peak: 0,
               nonzeroSamples: 0, sampleCount: 0, firstSignalAt: null}};
  Object.defineProperty(globalThis, '__qaMicrophoneBus', {{value: bus}});
  // A physical microphone has a tiny idle noise floor. Exact digital zero
  // trips this platform's frozen-input detector during its 30-second prep.
  // The optional floor is disabled for every recording phase, including when
  // an answer is missing; it must never stand in for a spoken answer.
  if ({self.idle_floor} > 0) {{
    const buffer=context.createBuffer(1,context.sampleRate,context.sampleRate);
    const data=buffer.getChannelData(0);let seed=123456789;
    for(let i=0;i<data.length;i++){{seed=(1664525*seed+1013904223)>>>0;data[i]=seed/2147483648-1;}}
    const node=context.createBufferSource(),gain=context.createGain();
    node.buffer=buffer;node.loop=true;gain.gain.value={self.idle_floor};
    node.connect(gain);gain.connect(destination);node.start();bus.idleInput={{node,gain}};
    setInterval(()=>{{
      const recording=[...document.querySelectorAll('h2,[role=heading]')].some(n=>
        n.getClientRects().length&&!n.closest('[hidden],[aria-hidden="true"]')&&
        ['Speak Now','Recording'].includes(n.textContent.trim()));
      gain.gain.value=recording?0:{self.idle_floor};
    }},20);
  }}
  // Keep input nodes alive for recorder libraries that only retain the gain
  // node downstream. Some Chromium builds otherwise lose the stream consumer.
  const createInput = AudioContext.prototype.createMediaStreamSource;
  AudioContext.prototype.createMediaStreamSource = function(stream) {{
    const node = createInput.call(this, stream);
    if (stream.getAudioTracks().some(track =>
        destination.stream.getAudioTracks().includes(track))) bus.consumers.push(node);
    return node;
  }};
  const samples = new Float32Array(analyser.fftSize);
  setInterval(() => {{
    analyser.getFloatTimeDomainData(samples);
    let peak = 0;
    for (const sample of samples) peak = Math.max(peak, Math.abs(sample));
    bus.peak = Math.max(bus.peak, peak);
    bus.sampleCount += 1;
    if (peak > 0.01) {{
      bus.nonzeroSamples += 1;
      if (bus.firstSignalAt === null) bus.firstSignalAt = performance.now();
    }}
  }}, 20);
  const media = navigator.mediaDevices;
  if (!media || !media.getUserMedia) throw new Error('media_devices_unavailable');
  const original = media.getUserMedia.bind(media);
  media.getUserMedia = async constraints => {{
    if (!constraints || !constraints.audio) return original(constraints);
    if (constraints.video) throw new DOMException('Audio-only QA input', 'NotSupportedError');
    bus.streamsRequested += 1;
    if (context.state === 'suspended') await context.resume();
    return destination.stream;
  }};
  // Older recorder libraries still use the callback APIs. Route those through
  // the same input; otherwise they silently open the physical microphone.
  const getSharedMedia = media.getUserMedia;
  for (const name of ['getUserMedia', 'webkitGetUserMedia', 'mozGetUserMedia']) {{
    if (typeof navigator[name] !== 'function') continue;
    navigator[name] = (constraints, success, failure) => {{
      getSharedMedia(constraints).then(success, failure);
    }};
  }}
  const resume = () => {{ if (context.state === 'suspended') context.resume().catch(() => {{}}); }};
  addEventListener('pointerdown', resume, {{capture:true}});
  addEventListener('keydown', resume, {{capture:true}});
}})();"""
