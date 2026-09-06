"""Read-only measurements of the assessment's own audio recorder and analyser."""
import json


def recording_observer_script(origin, token):
    config = json.dumps({'origin':origin, 'token':token})
    return r'''(() => {
      const cfg = CONFIG;
      if (location.origin !== cfg.origin || !globalThis.__qaMicrophoneBus) return;
      const bus = __qaMicrophoneBus;
      const evidence = globalThis.__qaRecorderEvidence = {analysers:[],recordings:[]};
      const indices = new WeakMap();
      const originalFrequency = AnalyserNode.prototype.getByteFrequencyData;
      AnalyserNode.prototype.getByteFrequencyData = function(data) {
        const result = originalFrequency.call(this, data);
        if (!indices.has(this)) {
          indices.set(this, evidence.analysers.length);
          evidence.analysers.push({calls:0,nonzero:0,peak:0,contextState:this.context.state});
        }
        const record = evidence.analysers[indices.get(this)];
        const peak = Math.max(...data);
        record.calls++; record.nonzero += Number(peak > 0);
        record.peak = Math.max(record.peak,peak);
        record.contextState = this.context.state;
        return result;
      };
      const originalStart = MediaRecorder.prototype.start;
      const observed = new WeakSet();
      MediaRecorder.prototype.start = function(...args) {
        if (!observed.has(this) && this.stream.getVideoTracks().length === 0 &&
            this.stream.getAudioTracks().some(t=>bus.destination.stream.getAudioTracks().includes(t))) {
          observed.add(this);
          let chunks = [];
          this.addEventListener('start',()=>{chunks=[]});
          this.addEventListener('dataavailable',event=>{if(event.data.size)chunks.push(event.data)});
          this.addEventListener('stop', async()=>{
            const data = new Blob(chunks, {type:this.mimeType});
            const record = {bytes:data.size,type:data.type,number:document.querySelector('button.currentQue')?.textContent.trim()};
            evidence.recordings.push(record);
            try {
              const bytes = await data.arrayBuffer();
              const decoded = await bus.context.decodeAudioData(bytes.slice(0));
              const samples=decoded.getChannelData(0); let peak=0,sq=0;
              for(const v of samples){peak=Math.max(peak,Math.abs(v));sq+=v*v;}
              record.duration=decoded.duration;record.peak=peak;record.rms=Math.sqrt(sq/samples.length);
              const name='recorder-'+Date.now()+(data.type.startsWith('audio/mp4')?'.m4a':'.webm');
              const response=await fetch('http://127.0.0.1:18772/capture',{
                method:'POST',headers:{'Content-Type':data.type,'X-Capture-Token':cfg.token,'X-Capture-Name':name},body:bytes});
              if(response.ok)record.artifact=name;else record.error='capture_http_'+response.status;
            }catch(error){record.error=error.name;}
          });
        }
        return originalStart.apply(this,args);
      };
    })();'''.replace('CONFIG',config)
