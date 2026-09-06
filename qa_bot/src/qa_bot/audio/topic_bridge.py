"""Arm a verified spoken answer, then play it once in its exact recording phase."""
import json
from qa_bot.audio.shared_microphone import SharedMicrophoneBridge


def topic_bridge_script(origin):
    SharedMicrophoneBridge(origin)
    return r'''(() => {
      if(location.origin!==ORIGIN || !globalThis.__qaMicrophoneBus)return;
      const bus=__qaMicrophoneBus;
      const qa=globalThis.__qaTopicSpeech={status:'idle',siteId:null,replays:0,failures:[]};
      const normalize=s=>s.replace(/\s+/g,' ').trim();
      const visible=n=>n.getClientRects().length&&!n.closest('[hidden],[aria-hidden="true"]');
      const number=()=>document.querySelector('button.currentQue')?.textContent.trim();
      const heading=text=>[...document.querySelectorAll('h1,h2,[role=heading]')].some(n=>visible(n)&&normalize(n.textContent)===text);
      let active=null;
      qa.arm=async data=>{
        if(!heading('Section D: Free Speech') || number()!==data.number || active)throw Error('topic_identity_rejected');
        const bytes=Uint8Array.from(atob(data.wav),c=>c.charCodeAt(0));
        const sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(b=>b.toString(16).padStart(2,'0')).join('');
        if(sha!==data.sha256)throw Error('topic_audio_integrity_failure');
        const buffer=await bus.context.decodeAudioData(bytes.buffer);
        if(!(buffer.duration>=15&&buffer.duration<=42))throw Error('topic_duration_rejected');
        active={number:data.number,prompt:normalize(data.prompt),buffer,played:false,source:null};
        qa.siteId='navigation:'+data.number;qa.status='armed';qa.duration=buffer.duration;
        return {status:qa.status,duration:buffer.duration};
      };
      setInterval(()=>{
        if(!active || active.played)return;
        if(!heading('Section D: Free Speech')||number()!==active.number){qa.status='blocked';return;}
        if(document.querySelector('[id^="ngdialog"]')||!heading('Speak Now'))return;
        if(!normalize(document.body.innerText).includes(active.prompt)||bus.context.state!=='running'){
          qa.status='blocked';qa.failures.push('topic_or_context_changed');active.played=true;return;
        }
        const node=bus.context.createBufferSource();node.buffer=active.buffer;node.connect(bus.destination);
        active.source=node;active.played=true;qa.status='replaying';qa.replays++;
        node.onended=()=>{active.source=null;qa.status='played'};node.start();
      },20);
    })();'''.replace('ORIGIN',json.dumps(origin))
