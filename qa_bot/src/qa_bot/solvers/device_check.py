"""Navigate observed device review controls without choosing a background NEXT."""
import json
import time


_REVIEW = 'Click NEXT if you can hear your voice clearly'
_METADATA = r'''function() {
  const visible=n=>n&&n.isConnected&&n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden';
  const parent=n=>n.parentElement||(n.getRootNode&&n.getRootNode().host)||null;
  const chain=[];for(let n=this;n;n=parent(n))chain.push(n);
  const dialogs=chain.filter(n=>n.matches?.('[role="dialog"],[aria-modal="true"],[id^="ngdialog"],.ngdialog-content,.modal-dialog'));
  const dialog=dialogs.find(n=>/Question Audio|Device Testing|Device testing successful|Click NEXT if you can hear/.test(n.innerText||n.textContent||''))||dialogs[0];
  const r=this.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
  const hit=document.elementFromPoint(x,y);
  return {visible:visible(this),enabled:!this.disabled&&this.getAttribute('aria-disabled')!=='true',
    foreground:Boolean(hit&&(chain.includes(hit)||this.contains(hit))),
    dialog:Boolean(dialog),scopeText:dialog?(dialog.innerText||dialog.textContent||'').slice(0,3000):'',
    x,y};
}'''


async def _candidates(session, name):
    tree=await session.cdp.send('Accessibility.getFullAXTree')
    found=[]
    for node in tree['nodes']:
        if (node.get('ignored') or node.get('role',{}).get('value')!='button'
                or node.get('name',{}).get('value','').strip()!=name
                or any(p['name']=='disabled' and p['value'].get('value') for p in node.get('properties',[]))):
            continue
        backend=node.get('backendDOMNodeId')
        if backend is None:continue
        resolved=await session.cdp.send('DOM.resolveNode',{'backendNodeId':backend})
        object_id=resolved['object'].get('objectId')
        if not object_id:continue
        try:
            result=await session.cdp.send('Runtime.callFunctionOn',{'objectId':object_id,'functionDeclaration':_METADATA,'returnByValue':True})
            metadata=result.get('result',{}).get('value',{})
            if metadata.get('visible') and metadata.get('enabled') and metadata.get('foreground'):
                found.append({'backend':backend,**metadata})
        finally:
            await session.cdp.send('Runtime.releaseObject',{'objectId':object_id})
    return found


async def _click(session, name, *, diagnostic=False):
    candidates=await _candidates(session,name)
    if diagnostic:
        candidates=[c for c in candidates if c['dialog'] and all(marker in c['scopeText'] for marker in ('Connection','Audio','Question Audio'))]
    else:
        modal=[c for c in candidates if c['dialog']]
        if modal:candidates=modal
    if not candidates:return False
    if len(candidates)!=1:raise ValueError('ambiguous foreground device '+name+' control')
    target=candidates[0]
    # Revalidate the same backend node immediately before native input.
    fresh=await _candidates(session,name)
    current=[c for c in fresh if c['backend']==target['backend']]
    if len(current)!=1:return False
    if diagnostic and not current[0]['dialog']:return False
    for kind in ('mousePressed','mouseReleased'):
        await session.cdp.send('Input.dispatchMouseEvent',{'type':kind,'x':current[0]['x'],'y':current[0]['y'],'button':'left','clickCount':1})
    session.log('device-navigation.jsonl',{'time':time.time(),'action':name,'diagnostic_dialog':diagnostic})
    return True


async def advance_device_check(session,state):
    """Return True when this is a device screen, even while its button is disabled."""
    if getattr(session,'preflight',False):return False
    text=state.get('text','')
    if 'Device testing successful' in text and 'Your device is compatible.' in text:
        await _click(session,'OK');return True
    if 'Device Testing' in text and 'This is a sample question to check your device compatibility.' in text:
        await _click(session,'OK');return True
    if 'Device Testing' in text and 'Were you able to hear the question audio clearly?' in text:
        if any(x.get('duration',0)>0 for x in state.get('heard',[])):
            await _click(session,'YES')
        return True
    if state.get('number')!='1' or _REVIEW not in text:return False
    read=state.get('read') or {}; microphone=state.get('microphone') or {}
    if read.get('siteId')!='navigation:1' or read.get('replays',0)<1 or microphone.get('signal',0)<=2:
        return True
    diagnostic=all(marker in text for marker in ('Connection','Audio','Question Audio','TRY AGAIN'))
    if diagnostic:
        # The diagnostics NEXT is separate from the sample's background NEXT.
        # Waiting for the platform to enable it preserves failed/pending checks.
        await _click(session,'NEXT',diagnostic=True)
        return True
    if await _click(session,'NEXT'):return True
    if not getattr(session,'device_play_started',False) and await _click(session,'Play'):
        session.device_play_started=True
    return True
