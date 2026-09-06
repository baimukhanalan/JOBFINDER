"""Decode a recorded browser container into mono PCM16 WAV without external codecs."""
import base64
import io
import wave


async def decode_to_wav(page, encoded):
    pcm = await page.evaluate('''async b64=>{
      const bytes=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));
      const decoder=new OfflineAudioContext(1,1,16000);
      const decoded=await decoder.decodeAudioData(bytes.buffer);
      if(!(decoded.duration>0&&decoded.duration<=300))throw Error('audio_duration_rejected');
      const context=new OfflineAudioContext(1,Math.ceil(decoded.duration*16000),16000);
      const source=context.createBufferSource();source.buffer=decoded;source.connect(context.destination);source.start();
      const audio=await context.startRendering(),data=audio.getChannelData(0),out=new Uint8Array(data.length*2),view=new DataView(out.buffer);
      for(let i=0;i<data.length;i++)view.setInt16(i*2,Math.max(-32768,Math.min(32767,Math.round(data[i]*32767))),true);
      let text='';for(let i=0;i<out.length;i+=4096)text+=String.fromCharCode(...out.slice(i,i+4096));return btoa(text);
    }''', base64.b64encode(encoded).decode())
    output=io.BytesIO()
    with wave.open(output,'wb') as writer:
        writer.setnchannels(1);writer.setsampwidth(2);writer.setframerate(16000)
        writer.writeframes(base64.b64decode(pcm))
    return output.getvalue()
