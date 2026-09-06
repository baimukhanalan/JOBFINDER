"""One native same-question retry after its exact audio has been prepared."""
import hashlib
import time


async def recover_read_warning(session,state):
    if getattr(session,'preflight',False) or 'We are unable to hear you.' not in state.get('text',''):
        return False
    from qa_bot.live_session import STATE_SCRIPT
    number=state.get('number')
    if not number:return False
    def enabled_try_again(nodes):
        return [n for n in nodes if n.get('name',{}).get('value','').strip()=='TRY AGAIN'
                and not any(p['name']=='disabled' and p['value'].get('value') for p in n.get('properties',[]))]
    if len(enabled_try_again(await session.controls('button')))!=1:return False
    signature=await session.page.evaluate('()=>globalThis.__qaDynamicReadAloud?.retrySignature?.()')
    if not isinstance(signature,str) or not signature:return False
    key=hashlib.sha256(signature.encode()).hexdigest()
    attempted=getattr(session,'_read_retry_attempts',None)
    if attempted is None:attempted=session._read_retry_attempts=set()
    if key in attempted:return False
    attempted.add(key)
    ready=await session.page.evaluate('async()=>Boolean(await globalThis.__qaDynamicReadAloud?.prepareRetryCurrent?.())')
    if not ready:return False
    fresh=await session.page.evaluate(STATE_SCRIPT)
    if fresh.get('number')!=number or 'We are unable to hear you.' not in fresh.get('text',''):
        return False
    if len(enabled_try_again(await session.controls('button')))!=1:return False
    if not await session.page.evaluate('()=>Boolean(globalThis.__qaDynamicReadAloud?.retryCurrent?.())'):
        return False
    await session.click('button','TRY AGAIN')
    session.log('speech-retries.jsonl',{'time':time.time(),'number':number,'reason':'unable_to_hear','same_question_audio_prepared':True})
    return True
