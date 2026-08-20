"""Gmail-style server-rendered UI for the self-hosted candidate mail CRM.

Ported from the amaskills mail panel (clean light theme) and adapted for
JOBFINDER: candidate mailboxes, recruiter-mail classification labels, the
/mail/* routes. Self-contained HTML (inline CSS+JS, Google Fonts). No build step.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from html import escape

_MONTHS = ["", "янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
_KIND = {
    "interview": ("📞", "Собеседование", "#1a73e8", "#e8f0fe"),
    "offer": ("🎉", "Оффер", "#188038", "#e6f4ea"),
    "rejection": ("✕", "Отказ", "#d93025", "#fce8e6"),
    "ack": ("•", "Принято", "#5f6368", "#f1f3f4"),
    "other": ("✉", "", "#80868b", "#f1f3f4"),
}


def _initial(name: str) -> str:
    name = (name or "?").strip()
    return name[0].upper() if name else "?"


def _avatar_color(seed: str) -> str:
    h = int(hashlib.md5((seed or "").encode()).hexdigest(), 16) % 360
    return f"hsl({h} 48% 46%)"


def maildate(ts: int) -> str:
    if not ts:
        return ""
    try:
        d = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    except Exception:
        return ""
    now = datetime.now().astimezone()
    if d.date() == now.date():
        return d.strftime("%H:%M")
    if d.year == now.year:
        return f"{d.day} {_MONTHS[d.month]}"
    return d.strftime("%d.%m.%y")


def fulldate(ts: int) -> str:
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    return f"{d.day} {_MONTHS[d.month]} {d.year}, {d.strftime('%H:%M')}"


# ---------------------------------------------------------------- CSS (ported)
_CSS = """
:root{--bg-app:#f6f8fc;--panel:#fff;--panel-2:#f1f3f4;--ink:#202124;--ink-soft:#5f6368;--ink-mute:#80868b;--line:#e8eaed;--line-strong:#dadce0;--accent:#1a73e8;--accent-deep:#1762c4;--accent-soft:#e8f0fe;--danger:#d93025;--r:12px;--r-sm:8px;--r-full:999px;--ff:'Hanken Grotesk',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;--ff-mono:'JetBrains Mono',ui-monospace,monospace;--sidebar-w:64px;}
*{box-sizing:border-box;}html,body{margin:0;overflow-x:hidden;max-width:100%;}
body{font-family:var(--ff);font-size:13.5px;line-height:1.5;color:var(--ink);background:var(--bg-app);-webkit-font-smoothing:antialiased;}
a{color:var(--accent);text-decoration:none;}a:hover{text-decoration:underline;}
.layout{display:flex;min-height:100vh;}
.sidebar{width:var(--sidebar-w);background:#fff;border-right:1px solid var(--line);padding:16px 0;display:flex;flex-direction:column;align-items:center;position:sticky;top:0;height:100vh;gap:6px;}
.sidebar .brand{width:34px;height:34px;border-radius:9px;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:15px;margin-bottom:6px;}
.sidebar .nav a{display:flex;flex-direction:column;align-items:center;gap:3px;color:var(--ink-mute);padding:9px 6px;border-radius:var(--r-sm);font-size:9.5px;width:52px;text-align:center;}
.sidebar .nav a.active{color:var(--accent);background:var(--accent-soft);}
.sidebar .nav a:hover{color:var(--ink);text-decoration:none;}
.sidebar .nav a svg{width:20px;height:20px;}
main{flex:1;padding:22px 30px;min-width:0;}
.page-head{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:18px;position:sticky;top:0;z-index:10;background:var(--bg-app);padding-top:14px;transition:transform .25s;}
.page-head.hide{transform:translateY(-130%);}
.ph-left{display:flex;align-items:center;gap:18px;flex-wrap:wrap;}
.seg-nav{display:flex;align-items:baseline;gap:20px;}
.seg-nav a{font-size:22px;font-weight:600;color:var(--ink-mute);letter-spacing:-.02em;padding-bottom:4px;display:inline-flex;align-items:baseline;gap:7px;}
.seg-nav a:hover{color:var(--ink-soft);text-decoration:none;}
.seg-nav a b{font-family:var(--ff-mono);font-size:12px;font-weight:400;color:var(--ink-mute);}
.seg-nav a.active{color:var(--ink);box-shadow:0 2px 0 var(--accent);}
.seg-nav a.active b{color:var(--accent);}
.head-actions{display:flex;gap:8px;}
.hbtn{display:inline-flex;align-items:center;gap:7px;background:var(--panel);color:var(--ink-soft);border:1px solid var(--line-strong);padding:7px 14px;border-radius:var(--r-full);font-weight:600;font-size:13px;cursor:pointer;}
.hbtn:hover{background:var(--panel-2);color:var(--ink);}
.hbtn svg{width:15px;height:15px;}
.hbtn.danger{color:var(--danger);border-color:#f3c7c2;}.hbtn.danger:hover{background:var(--danger);color:#fff;}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
input,select,textarea{font:inherit;color:var(--ink);padding:9px 13px;border:1px solid var(--line-strong);border-radius:var(--r-sm);background:var(--panel);}
input::placeholder,textarea::placeholder{color:var(--ink-mute);}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgb(26 115 232/.15);}
input[type=search]{min-width:240px;}textarea{width:100%;min-height:150px;resize:vertical;}
label{display:block;font-weight:600;color:var(--ink-soft);margin:12px 0 5px;font-size:12px;}
button.primary{font:inherit;font-weight:600;cursor:pointer;border:0;padding:10px 16px;border-radius:var(--r-full);background:var(--accent);color:#fff;font-size:13.5px;}
button.primary:hover{background:var(--accent-deep);}
.ghost{background:var(--panel);color:var(--ink-soft);border:1px solid var(--line-strong);padding:9px 15px;border-radius:var(--r-full);font-weight:600;cursor:pointer;font-size:13px;}
.ghost:hover{background:var(--panel-2);color:var(--ink);text-decoration:none;}
.filterbar{background:var(--panel);border:1px solid var(--line);border-radius:var(--r-sm);padding:9px 14px;margin-bottom:16px;color:var(--ink-soft);}
.maillist{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;}
.mitem{display:flex;align-items:flex-start;border-bottom:1px solid var(--line);}
.mitem:last-child{border-bottom:0;}
.mitem:hover{background:#f8fafd;}
.mitem.unread{background:#f8fbff;}
.mrow{flex:1;min-width:0;display:flex;gap:13px;align-items:flex-start;padding:11px 18px;color:var(--ink);}
.mrow:hover{text-decoration:none;}
.avatar{position:relative;flex:0 0 auto;width:34px;height:34px;border-radius:50%;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:600;font-size:14px;margin-top:1px;}
.mbody{min-width:0;flex:1;display:flex;flex-direction:column;gap:1px;}
.mtop{display:flex;align-items:baseline;gap:8px;}
.msender{min-width:0;max-width:240px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500;color:var(--ink-soft);font-size:13.5px;}
.tag{flex:0 0 auto;font-family:var(--ff-mono);font-size:10px;padding:1px 8px;border-radius:var(--r-full);}
.mbox{flex:0 0 auto;font-family:var(--ff-mono);font-size:10.5px;color:var(--ink-mute);}
.mdate{flex:0 0 auto;margin-left:auto;font-family:var(--ff-mono);font-size:11px;color:var(--ink-mute);}
.msubj{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink-soft);font-size:13.5px;}
.msnip{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink-mute);font-size:12.5px;}
.mitem.unread .msender,.mitem.unread .msubj{color:var(--ink);font-weight:700;}
.mitem.unread .mdate{color:var(--accent);}
.empty{text-align:center;padding:48px;color:var(--ink-mute);}
.msg-toolbar{display:flex;align-items:center;gap:8px;margin-bottom:20px;}
.msg-toolbar .spacer{flex:1;}.msg-toolbar form{margin:0;}
.msg-page{max-width:840px;}
.msg-subject{font-size:22px;font-weight:600;letter-spacing:-.01em;margin:0 0 18px;line-height:1.25;}
.msg-from{display:flex;gap:13px;align-items:flex-start;padding-bottom:18px;margin-bottom:20px;border-bottom:1px solid var(--line);}
.msg-from .avatar{width:42px;height:42px;font-size:17px;}
.mf-meta{min-width:0;flex:1;}.mf-line{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;}
.mf-line b{font-size:15px;color:var(--ink);}
.mf-addr{font-family:var(--ff-mono);font-size:12px;color:var(--ink-mute);}
.mf-to{font-size:12.5px;color:var(--ink-mute);margin-top:5px;}.mf-dot{margin:0 7px;opacity:.6;}
.mail-frame-wrap{overflow:hidden;}.mail-frame{width:100%;border:0;background:transparent;display:block;}
.msg-content{white-space:pre-wrap;word-break:break-word;color:var(--ink);line-height:1.7;font-size:14.5px;}
.mbxlist{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;}
.mbxrow{display:flex;align-items:center;gap:12px;padding:11px 18px;border-bottom:1px solid var(--line);color:var(--ink);}
.mbxrow:last-child{border-bottom:0;}.mbxrow:hover{background:#f8fafd;text-decoration:none;}
.mbxrow .nm{font-weight:600;}.mbxrow .em{font-family:var(--ff-mono);font-size:12px;color:var(--ink-mute);margin-left:auto;}
.mbxrow .cnt{font-family:var(--ff-mono);font-size:11px;color:#fff;background:var(--accent);border-radius:var(--r-full);padding:1px 8px;}
.modal{position:fixed;inset:0;z-index:50;display:none;align-items:flex-start;justify-content:center;padding:8vh 16px;background:rgba(32,33,36,.5);}
.modal.open{display:flex;}
.modal-card{width:100%;max-width:480px;background:var(--panel);border-radius:var(--r);padding:22px 24px;box-shadow:0 12px 40px -8px rgba(32,33,36,.3);}
.modal-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;}
.modal-head h3{font-size:18px;margin:0;font-weight:600;}
.modal-head .x{background:none;border:0;font-size:24px;color:var(--ink-mute);cursor:pointer;}
.modal form input,.modal form textarea,.modal form select{width:100%;}
.modal-actions{margin-top:16px;}.modal-actions .primary{width:100%;}
.sendmsg{margin-top:10px;font-size:13px;}
@media(max-width:760px){.sidebar{width:auto;height:auto;position:static;flex-direction:row;border-right:0;border-bottom:1px solid var(--line);padding:8px;}.sidebar .brand{margin:0 6px 0 0;}main{padding:12px;}.seg-nav a{font-size:19px;}.toolbar{width:100%;}.toolbar input[type=search]{flex:1;min-width:0;}.msender{max-width:140px;}}
"""

_FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
          '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
          '<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700'
          '&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">')


def _sidebar(active: str) -> str:
    def item(href, key, label, svg):
        cls = " active" if active == key else ""
        return f'<a class="{cls.strip()}" href="{href}">{svg}<span>{label}</span></a>'
    inbox_ic = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.5 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.5A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.5z"/></svg>'
    box_ic = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/></svg>'
    roles_ic = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 3h18v18H3z"/><path d="M3 9h18M9 21V9"/></svg>'
    return (f'<aside class="sidebar"><div class="brand">JF</div><div class="nav">'
            + item("/mail", "inbox", "Инбокс", inbox_ic)
            + item("/mail/candidates", "candidates", "Кандидаты", box_ic)
            + item("/roles", "roles", "Компании", roles_ic)
            + '</div></aside>')


def _page(active: str, body: str, modal: str = "") -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>JobFinder — почта кандидатов</title>" + _FONTS +
        f"<style>{_CSS}</style></head><body>"
        f"<div class='layout'>{_sidebar(active)}<main>{body}</main></div>{modal}"
        + _JS + "</body></html>")


def _kind_tag(kind: str) -> str:
    emoji, label, fg, bg = _KIND.get(kind, _KIND["other"])
    if not label:
        return ""
    return f'<span class="tag" style="color:{fg};background:{bg}">{emoji} {escape(label)}</span>'


def render_rows(rows: list[dict], show_mailbox: bool = True) -> str:
    out = []
    for m in rows:
        sender = m.get("from_name") or m.get("from_email") or "?"
        unread = "" if m.get("seen") else " unread"
        mbox = (f'<span class="mbox">{escape((m.get("candidate") or m.get("mailbox") or "")[:22])}</span>'
                if show_mailbox else "")
        out.append(
            f'<div class="mitem{unread}" data-ts="{m.get("date_ts",0)}" data-id="{escape(m["id"])}">'
            f'<a class="mrow" href="/mail/message?id={escape(m["id"])}">'
            f'<span class="avatar" style="background:{_avatar_color(sender)}">{escape(_initial(sender))}</span>'
            f'<span class="mbody"><span class="mtop">'
            f'<span class="msender">{escape(sender)}</span>{_kind_tag(m.get("kind","other"))}{mbox}'
            f'<span class="mdate">{maildate(m.get("date_ts",0))}</span></span>'
            f'<span class="msubj">{escape(m.get("subject","") or "(без темы)")}</span>'
            f'<span class="msnip">{escape(m.get("snippet",""))}</span>'
            "</span></a></div>")
    return "".join(out)


def render_inbox(rows: list[dict], counts: dict, q: str = "", mailbox: str = "",
                 mailbox_name: str = "", page_size: int = 50) -> str:
    has_more = 1 if len(rows) == page_size else 0
    head = (
        '<div class="page-head"><div class="ph-left"><div class="seg-nav">'
        f'<a class="active" href="/mail">Инбокс <b>{counts.get("unread",0)}</b></a>'
        f'<a href="/mail/candidates">Кандидаты <b>{counts.get("mailboxes",0)}</b></a>'
        '</div><div class="head-actions">'
        '<button class="hbtn" onclick="openCompose()">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>'
        'Написать</button></div></div>'
        f'<form method="get" action="/mail" class="toolbar"><input type="search" name="q" value="{escape(q)}" placeholder="Поиск по теме / отправителю">'
        + (f'<input type="hidden" name="mailbox" value="{escape(mailbox)}">' if mailbox else "")
        + '<button class="ghost" type="submit">Найти</button>'
        + (f'<a class="ghost" href="/mail">Сброс</a>' if (q or mailbox) else "")
        + '</form></div>')
    fbar = (f'<div class="filterbar">Ящик кандидата: <b>{escape(mailbox_name or mailbox)}</b> '
            f'<a href="/mail">убрать фильтр</a></div>' if mailbox else "")
    empty = '<div class="empty">Писем нет</div>' if not rows else ""
    body = (head + fbar +
            f'<div class="maillist" id="maillist">{render_rows(rows)}</div>{empty}'
            f'<div id="loadmore" data-more="{has_more}" style="height:1px"></div>')
    return _page("inbox", body, _COMPOSE_MODAL)


def render_message(m: dict) -> str:
    sender = m.get("from_name") or m.get("from_email") or "?"
    reply_args = (f"'{escape(m.get('mailbox',''))}','{escape(m.get('from_email',''))}',"
                  f"\"{escape((m.get('subject') or '').replace(chr(34), ''))}\","
                  f"'{escape(m.get('message_id',''))}'")
    if m.get("html"):
        content = (f'<div class="mail-frame-wrap"><iframe class="mail-frame" sandbox="allow-same-origin" '
                   f'onload="fitFrame(this)" srcdoc="{escape(m["html"])}"></iframe></div>')
    else:
        content = f'<div class="msg-content">{escape(m.get("plain","") or "(пустое письмо)")}</div>'
    att = ('<div class="mf-to">📎 есть вложения (скачивание недоступно)</div>'
           if m.get("has_attachments") else "")
    body = (
        '<div class="msg-toolbar">'
        '<a class="hbtn" href="/mail"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>К списку</a>'
        '<div class="spacer"></div>'
        f'<button class="hbtn" onclick="reply({reply_args})"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/></svg>Ответить</button>'
        '</div>'
        f'<div class="msg-page"><h1 class="msg-subject">{escape(m.get("subject","") or "(без темы)")}</h1>'
        f'<div class="msg-from"><span class="avatar" style="background:{_avatar_color(sender)}">{escape(_initial(sender))}</span>'
        f'<div class="mf-meta"><div class="mf-line"><b>{escape(sender)}</b><span class="mf-addr">{escape(m.get("from_email",""))}</span>{_kind_tag(m.get("kind","other"))}</div>'
        f'<div class="mf-to">ящик кандидата: {escape(m.get("candidate",""))} &lt;{escape(m.get("mailbox",""))}&gt;<span class="mf-dot">·</span>{escape(m.get("date",""))}</div>{att}</div></div>'
        f'{content}</div>')
    return _page("inbox", body, _COMPOSE_MODAL)


def render_candidates(cands: list[dict]) -> str:
    rows = []
    for c in cands:
        n = c.get("unread", 0)
        badge = f'<span class="cnt">{n}</span>' if n else ""
        rows.append(
            f'<a class="mbxrow" href="/mail?mailbox={escape(c["email"])}">'
            f'<span class="avatar" style="background:{_avatar_color(c["name"])};width:30px;height:30px;font-size:13px">{escape(_initial(c["name"]))}</span>'
            f'<span class="nm">{escape(c["name"])}</span>{badge}'
            f'<span class="em">{escape(c["email"])}</span></a>')
    head = ('<div class="page-head"><div class="ph-left"><div class="seg-nav">'
            '<a href="/mail">Инбокс</a>'
            f'<a class="active" href="/mail/candidates">Кандидаты <b>{len(cands)}</b></a>'
            '</div></div></div>')
    body = head + f'<div class="mbxlist">{"".join(rows)}</div>'
    return _page("candidates", body)


_COMPOSE_MODAL = """
<div class="modal" id="composeModal"><div class="modal-card">
  <div class="modal-head"><h3>Письмо</h3><button class="x" onclick="closeCompose()">×</button></div>
  <form id="composeForm" onsubmit="return sendMail(event)">
    <label>С ящика кандидата</label><input name="from_email" placeholder="ruslan.baibekov@takhet.com" required autocomplete="off">
    <label>Кому</label><input name="to" type="email" placeholder="recruiter@company.com" required>
    <label>Тема</label><input name="subject" placeholder="Тема">
    <label>Текст</label><textarea name="body" placeholder="Текст письма"></textarea>
    <input type="hidden" name="in_reply_to">
    <div class="modal-actions"><button class="primary" type="submit">Отправить</button></div>
    <div class="sendmsg" id="sendmsg"></div>
  </form>
</div></div>
"""

_JS = """
<script>
function openCompose(){document.getElementById('composeModal').classList.add('open');document.body.style.overflow='hidden';document.getElementById('sendmsg').textContent='';}
function closeCompose(){document.getElementById('composeModal').classList.remove('open');document.body.style.overflow='';}
function reply(from,to,subj,mid){var f=document.getElementById('composeForm');f.from_email.value=from;f.to.value=to;f.subject.value=(subj&&subj.indexOf('Re:')===0?subj:'Re: '+(subj||''));f.in_reply_to.value=mid||'';openCompose();setTimeout(function(){f.body.focus();},50);}
document.querySelectorAll('.modal').forEach(function(m){m.addEventListener('click',function(e){if(e.target===m)closeCompose();});});
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeCompose();});
async function sendMail(e){
  e.preventDefault();
  var f=e.target, msg=document.getElementById('sendmsg');
  msg.style.color='#5f6368';msg.textContent='Отправка…';
  var fd=new FormData(f);
  try{
    var r=await fetch('/mail/send',{method:'POST',body:fd});
    var j=await r.json();
    if(j.ok){msg.style.color='#188038';msg.textContent='Отправлено ✓';setTimeout(closeCompose,900);}
    else{msg.style.color='#d93025';msg.textContent='Ошибка: '+(j.error||'не отправлено');}
  }catch(err){msg.style.color='#d93025';msg.textContent='Ошибка сети';}
  return false;
}
function fitFrame(f){try{var doc=f.contentDocument,wrap=f.parentElement;var st=doc.createElement('style');st.textContent='html,body{margin:0;padding:0;background:transparent;font-family:"Hanken Grotesk",-apple-system,sans-serif;color:#202124;font-size:14.5px;line-height:1.6;}img{max-width:100%;height:auto;}a{color:#1a73e8;}table{max-width:100%;}';doc.head.appendChild(st);var natW=Math.max(doc.body.scrollWidth,doc.documentElement.scrollWidth),avail=wrap.clientWidth;if(natW>avail+2){var s=avail/natW;f.style.width=natW+'px';f.style.transformOrigin='top left';f.style.transform='scale('+s+')';var h=doc.body.scrollHeight;f.style.height=h+'px';wrap.style.height=(h*s+8)+'px';}else{f.style.width='100%';f.style.transform='none';var hh=doc.body.scrollHeight+24;f.style.height=hh+'px';wrap.style.height=hh+'px';}}catch(e){f.style.height='600px';}}
// infinite scroll (keyset) + auto-hide header + SSE live refresh
(function(){
  var list=document.getElementById('maillist'), more=document.getElementById('loadmore');
  if(!list||!more)return;
  var loading=false,PAGE=50;
  async function loadMore(){
    if(loading||more.dataset.more!=='1')return;
    var rows=list.querySelectorAll('.mitem'),lastRow=rows[rows.length-1];if(!lastRow)return;
    loading=true;
    try{var sp=new URLSearchParams(location.search);sp.set('ts',lastRow.dataset.ts);sp.set('id',lastRow.dataset.id);
      var r=await fetch('/mail/more?'+sp.toString());
      if(r.ok){var html=await r.text();var added=(html.match(/class=.mrow/g)||[]).length;if(added)list.insertAdjacentHTML('beforeend',html);if(added<PAGE)more.dataset.more='0';}
    }catch(e){}finally{loading=false;}
  }
  var head=document.querySelector('.page-head'),lastY=window.scrollY;
  window.addEventListener('scroll',function(){var y=window.scrollY;if(window.innerHeight+y>=document.documentElement.scrollHeight-400)loadMore();if(y>lastY&&y>90)head.classList.add('hide');else if(y<lastY)head.classList.remove('hide');lastY=y;},{passive:true});
  async function refreshList(){if(document.querySelector('.modal.open'))return;try{var r=await fetch(location.href,{headers:{'X-Poll':'1'}});if(!r.ok)return;var doc=new DOMParser().parseFromString(await r.text(),'text/html');var fl=doc.getElementById('maillist');if(fl)list.innerHTML=fl.innerHTML;var sn=doc.querySelector('.seg-nav');if(sn)document.querySelector('.seg-nav').innerHTML=sn.innerHTML;}catch(e){}}
  var last=null,pollTimer=null;
  function startPoll(){if(pollTimer)return;pollTimer=setInterval(async function(){if(document.hidden||document.querySelector('.modal.open'))return;try{var r=await fetch('/mail/count'+location.search);if(!r.ok)return;var j=await r.json();if(j.n!==last){if(last!==null)await refreshList();last=j.n;}}catch(e){}},10000);}
  if(window.EventSource){try{var es=new EventSource('/mail/events');es.onmessage=function(){refreshList();};es.onerror=function(){startPoll();};}catch(e){startPoll();}}else{startPoll();}
})();
</script>
"""
