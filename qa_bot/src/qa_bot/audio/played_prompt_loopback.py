"""Repeat only a prompt actually played for the current question, never preloads."""
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from qa_bot.audio.shared_microphone import SharedMicrophoneBridge


@dataclass(frozen=True)
class PlayedPromptLoopback:
    origin: str
    token: str
    profile: str
    test: str
    capture_url: str = 'http://127.0.0.1:18771'

    def __post_init__(self):
        SharedMicrophoneBridge(self.origin)
        endpoint=urlsplit(self.capture_url)
        if (endpoint.scheme!='http' or endpoint.hostname!='127.0.0.1'
                or not endpoint.port or endpoint.path or endpoint.query or endpoint.fragment
                or endpoint.username or endpoint.password):
            raise ValueError('exact loopback capture endpoint required')
        if len(self.token) < 24 or any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}',v)
                                      for v in (self.profile,self.test)):
            raise ValueError('valid token and source identifiers required')

    def init_script(self):
        cfg=json.dumps({'origin':self.origin,'token':self.token,'profile':self.profile,'test':self.test,'captureUrl':self.capture_url})
        return r'''(() => {
 const cfg=CONFIG;
 if(location.origin!==cfg.origin || !globalThis.__qaMicrophoneBus)return;
 const bus=__qaMicrophoneBus;
 const qa=globalThis.__qaDirectAudioLoopback={status:'installed',replayCount:0,failures:[],playedSources:[],observedSources:[],heardSources:[],currentSiteId:null,replayStartedAt:null};
 const encodedMetadata=new WeakMap(),decodedMetadata=new WeakMap(),bufferHashes=new WeakMap(),byHash=new Map(),pending=new Map();
 const nativeFetch=fetch.bind(globalThis);
 let active=null;const ambiguous=new Set();
 const digest=async bytes=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
 const source=url=>{try{const u=new URL(url,location.href);return u.protocol==='https:' && u.hostname==='qbdata-amcat.s3.amazonaws.com' && u.pathname.startsWith('/SpeechAssessmentBank/') && /\.(mp3|wav|ogg|m4a)$/i.test(u.pathname)?{url:u.origin+u.pathname,path:u.pathname}:null}catch{return null}};
 const remember=(bytes,url)=>{
   const item=source(url);if(!item || !(bytes instanceof ArrayBuffer) || !bytes.byteLength || bytes.byteLength>20000000)return;
   item.bytes=bytes.slice(0);encodedMetadata.set(bytes,item);
   return digest(item.bytes).then(hash=>{item.hash=hash;byHash.set(hash,item);
     for(const entry of pending.get(hash)||[]){entry.report.path=item.path;handle(item,entry.buffer,entry.id,entry.section)}
     pending.delete(hash);return item;
   });
 };
 qa.registerSource=async(encoded,url)=>{const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0)).buffer;return Boolean(await remember(bytes,url))};
 const arrayBuffer=Response.prototype.arrayBuffer;
 Response.prototype.arrayBuffer=async function(){const bytes=await arrayBuffer.call(this);remember(bytes,this.url);return bytes};
 const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send,urls=new WeakMap();
 XMLHttpRequest.prototype.open=function(method,url,...args){urls.set(this,url);return open.call(this,method,url,...args)};
 XMLHttpRequest.prototype.send=function(...args){
   this.addEventListener('readystatechange',()=>{if(this.readyState===4 && this.responseType==='arraybuffer')remember(this.response,urls.get(this))});
   return send.apply(this,args);
 };
 const decode=BaseAudioContext.prototype.decodeAudioData;
 BaseAudioContext.prototype.decodeAudioData=function(bytes,success,failure){
   const zone=globalThis.Zone?.current;
   const invoke=(callback,value)=>{if(callback)return zone?zone.run(callback,undefined,[value]):callback(value)};
   // Decode an owned copy: native Chromium decoding detaches its input. Host
   // recorder/simulation code may still inspect the original encoded bytes.
   // An observer must not invalidate that shared buffer while computing its hash.
   let copy;
   try{copy=bytes.slice(0)}catch{return decode.call(this,bytes,success,failure)}
   const known=encodedMetadata.get(bytes),hash=digest(copy);
   const promise=decode.call(this,copy.slice(0)).then(async buffer=>{
     const key=await hash;bufferHashes.set(buffer,key);
     const item=known || byHash.get(key);if(item)decodedMetadata.set(buffer,item);
     invoke(success,buffer);return buffer;
   },error=>{invoke(failure,error);throw error});
   // Callback-only clients are allowed to ignore the returned promise.
   if(failure)promise.catch(()=>{});
   return promise;
 };
 const visible=n=>n.isConnected && n.getClientRects().length && !n.closest('[hidden],[aria-hidden="true"]') && getComputedStyle(n).visibility!=='hidden';
 const section=()=>[...document.querySelectorAll('h1,h2,[role=heading]')].some(n=>visible(n)&&n.textContent.trim()==='Section B: Listen and Repeat');
 const number=()=>{const nodes=[...document.querySelectorAll('button.currentQue')].filter(visible);return nodes.length===1?nodes[0].textContent.trim():null};
 const phase=label=>[...document.querySelectorAll('h2,[role=heading]')].some(n=>visible(n)&&n.textContent.trim()===label);
 let sequence=0;
 const capture=async(item,id,sectionName,duration,order=++sequence)=>{
   const type={mp3:'audio/mpeg',wav:'audio/wav',ogg:'audio/ogg',m4a:'audio/mp4'}[item.path.split('.').pop().toLowerCase()];
   try{const response=await nativeFetch(cfg.captureUrl+'/capture-prompt',{method:'POST',headers:{'Content-Type':type,'X-Prompt-Token':cfg.token,'X-Prompt-Source':item.url,'X-Source-Profile':cfg.profile,'X-Source-Test':cfg.test,'X-Source-Question':'heard-'+id},body:item.bytes});if(!response.ok)throw Error('capture_http_'+response.status);const receipt=await response.json();qa.heardSources.push({id,section:sectionName,path:item.path,file:receipt.file,sha256:receipt.sha256,duration,order});qa.heardSources.sort((a,b)=>a.order-b.order)}catch(error){qa.failures.push(error.message.slice(0,100))}
 };
 const sectionName=()=>[...document.querySelectorAll('h1,[role=heading]')].find(n=>visible(n)&&n.textContent.trim().startsWith('Section '))?.textContent.trim();
 // Conversations use HTML audio and range requests, unlike the Web Audio
 // sentence player. Observe actual playback, then archive the complete file.
 const play=HTMLMediaElement.prototype.play,mediaSeen=new Set();
 HTMLMediaElement.prototype.play=function(...args){
   this.addEventListener('playing',async()=>{
     const playbackUrl=this.currentSrc||this.src;
     const item=source(playbackUrl),id=number(),heading=sectionName();
     if(!item || !id || !heading || /\/(sections|subSections)\//.test(item.path))return;
     const key=id+'|'+item.url;if(mediaSeen.has(key))return;mediaSeen.add(key);
     const order=++sequence,report={id,section:heading,path:item.path,duration:this.duration,order,player:'html',startedAt:performance.now()};qa.observedSources.push(report);
     this.addEventListener('ended',()=>{report.endedAt=performance.now()},{once:true});
     try{
       // Preserve this playback's signed URL in memory; archive only its path.
       if(globalThis.__qaCapturePlayedAudio){
         const receipt=await globalThis.__qaCapturePlayedAudio({url:playbackUrl,id,order});
         qa.heardSources.push({id,section:heading,path:item.path,file:receipt.file,sha256:receipt.sha256,duration:this.duration,order});
         qa.heardSources.sort((a,b)=>a.order-b.order);return;
       }
       const response=await nativeFetch(playbackUrl);
       if(response.status!==200)throw Error('full_prompt_http_'+response.status);
       const bytes=await response.arrayBuffer();if(!bytes.byteLength || bytes.byteLength>20000000)throw Error('prompt_size_rejected');
       item.bytes=bytes;await capture(item,id,heading,this.duration,order);
     }catch(error){qa.failures.push(error.message.slice(0,100))}
   },{once:true});
   return play.apply(this,args);
 };
 const handle=(item,buffer,id,heading)=>{
   if(item && id && !/\/(sections|subSections)\//.test(item.path))capture(item,id,heading,buffer.duration);
   accept(item,buffer,id);
 };
 const accept=(item,buffer,id)=>{
   if(!section() || !phase('Listen Carefully') || id!==number())return;
   // This bank also holds spoken questions under sID/.../questionN.mp3.
   // Other bank paths are section directions, beeps or recording instructions.
   if(!item || !id || ambiguous.has(id) || (item.path.includes('/QuestionBank/') &&
       !/\/sID\/\d+\/[^?#]+\/question\d+\.mp3$/i.test(item.path)))return;
   if(active && active.id===id && active.item.url!==item.url){qa.status='ambiguous_prompt';qa.failures.push('multiple_spoken_prompts');ambiguous.add(id);active=null;return}
   if(!(buffer.duration>0 && buffer.duration<=120))return;
   active={id,item,buffer,played:false};
   qa.currentSiteId='navigation:'+id;qa.status='armed';
   qa.playedSources.push({id,path:item.path,duration:buffer.duration});
 };
 const start=AudioBufferSourceNode.prototype.start;
 AudioBufferSourceNode.prototype.start=function(...args){
   const result=start.apply(this,args);
   if(this.context===bus.context || this.context instanceof OfflineAudioContext || !sectionName())return result;
   const item=decodedMetadata.get(this.buffer)||byHash.get(bufferHashes.get(this.buffer)),id=number();
   const heading=sectionName();const report={id,section:heading,path:item?.path||'unidentified',duration:this.buffer?.duration,contextState:this.context.state,startedAt:performance.now()};qa.observedSources.push(report);
   this.context.addEventListener('statechange',()=>{report.contextState=this.context.state});
   this.addEventListener('ended',()=>{report.endedAt=performance.now()});
   if(item)handle(item,this.buffer,id,heading);
   else {const hash=bufferHashes.get(this.buffer);if(hash){const list=pending.get(hash)||[];list.push({buffer:this.buffer,id,report,section:heading});pending.set(hash,list)}}
   return result;
 };
 setInterval(()=>{
   if(!section() || !number() || document.querySelector('[id^="ngdialog"]'))return;
   if(active && active.id!==number()){active=null;qa.currentSiteId=null;qa.status='waiting_prompt'}
   if(!active || active.played || !phase('Speak Now') || qa.status!=='armed')return;
   if(bus.context.state!=='running'){qa.status='blocked';qa.failures.push('audio_context_not_running');return}
   const state=active,node=bus.context.createBufferSource();node.buffer=state.buffer;node.connect(bus.destination);
   state.played=true;state.source=node;qa.status='replaying';qa.replayCount++;qa.replayStartedAt=performance.now();node.start(bus.context.currentTime+.01);
   node.onended=()=>{if(active===state){state.source=null;qa.status='ready'}};
 },20);
 qa.retryCurrent=()=>{
   if(!active || !active.played || active.source || !document.querySelector('[id^="ngdialog"]'))return false;
   active.played=false;qa.status='armed';return true;
 };
})();'''.replace('CONFIG',cfg)
