"""Capture an actually playing HTML audio URL using the owned browser client."""
import os
import asyncio
from urllib.parse import urlsplit

async def capture_html(session,item):
    if session.preflight:raise ValueError('preflight cannot capture audio')
    url=urlsplit(item['url'])
    if url.scheme!='https' or url.hostname!='qbdata-amcat.s3.amazonaws.com' or not url.path.startswith('/SpeechAssessmentBank/') or not url.path.endswith('.mp3'):
        raise ValueError('played source rejected')
    observed=await session.page.evaluate('globalThis.__qaDirectAudioLoopback.observedSources')
    if not any(s.get('player')=='html' and s.get('path')==url.path and s.get('id')==item['id'] and s.get('order')==item['order'] for s in observed):
        raise ValueError('actual playback evidence required')
    for attempt in range(3):
        try:
            response=await session.page.request.get(item['url'],timeout=15000)
            break
        except Exception:
            if attempt==2:raise ValueError('played audio transport failed after retries') from None
            await asyncio.sleep(.25*(attempt+1))
    if response.status!=200:raise ValueError('full played audio unavailable')
    data=await response.body()
    if not 0<len(data)<=20000000:raise ValueError('audio size rejected')
    receipt=await session.page.request.post('http://127.0.0.1:18771/capture-prompt',headers={
        'Origin':'https://amcatglobal.aspiringminds.com','Content-Type':response.headers.get('content-type','audio/mpeg'),
        'X-Prompt-Token':os.environ['QA_LOCAL_BRIDGE_TOKEN'],'X-Prompt-Source':'https://qbdata-amcat.s3.amazonaws.com'+url.path,
        'X-Source-Profile':session.profile_id,'X-Source-Test':session.test_id,'X-Source-Question':'heard-'+item['id']},data=data)
    if receipt.status not in (200,201):raise ValueError('prompt capture rejected')
    record=await receipt.json()
    return {'file':record['file'],'sha256':record['sha256']}
