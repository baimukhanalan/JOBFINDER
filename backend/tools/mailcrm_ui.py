"""Gmail-style server-rendered UI for the self-hosted candidate mail CRM.

Ported from the amaskills mail panel (clean light theme) and adapted for
JOBFINDER: candidate mailboxes, recruiter-mail classification labels, the
/mail/* routes. Self-contained HTML (inline CSS+JS, Google Fonts). No build step.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from html import escape, unescape
from urllib.parse import urlencode

_MONTHS = ["", "янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
_KIND = {
    "interview": ("📞", "Собеседование", "#1a73e8", "#e8f0fe"),
    "offer": ("🎉", "Оффер", "#188038", "#e6f4ea"),
    "rejection": ("✕", "Отказ", "#d93025", "#fce8e6"),
    "action_needed": ("⚠️", "Действие нужно", "#b06000", "#feefc3"),
    "assessment_done": ("🤖", "Тест пройден", "#188038", "#e6f4ea"),
    "ack": ("•", "Принято", "#5f6368", "#f1f3f4"),
    "code": ("🔑", "Код", "#5f6368", "#f1f3f4"),
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


def _clean_addr(s: str) -> str:
    """One clean address for the 'кому:' line. A header like '"a@b" <a@b>' shows the email
    TWICE; collapse it: real display name if it differs from the address, else the address."""
    import email.utils
    name, addr = email.utils.parseaddr(s or "")
    name = (name or "").strip().strip('"').strip()
    if name and name.casefold() != (addr or "").casefold():
        return name
    return addr or (s or "").strip()


def _fulldate(ts: int) -> str:
    """Readable message timestamp for the message header, e.g. '24 авг 2026, 06:07'."""
    if not ts:
        return ""
    try:
        d = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        return f"{d.day} {_MONTHS[d.month]} {d.year}, {d:%H:%M}"
    except Exception:
        return ""


import re as _re
# _linkify runs on html.escape()'d text, so the body a URL sits in has had every '<'→'&lt;',
# '>'→'&gt;', '"'→'&quot;', "'"→'&#x27;' and every literal '&'→'&amp;'. A URL is therefore
# `https://` + a run of either the entity '&amp;' (a real '&' in a query string — KEEP it, the
# browser decodes it back to '&') OR any char that is not whitespace, '<' or a bare '&'. Stopping
# at a bare '&' ends the match at the '&gt;'/'&quot;'/'&#x27;' that a '<URL>' / '"URL"' wrapper
# produces. The old greedy `[^\s<]+` had no literal '<' left after escaping, so it swallowed that
# trailing entity INTO the href → the browser navigated to '…/token>' and the self-scheduling /
# Calendly / Zoom link died ("We can't find that self-scheduling link"). See test_mailcrm_linkify.
_URL_RE = _re.compile(r'https?://(?:&amp;|[^\s<&])+')
# Sentence punctuation an email writer puts right after a URL — never part of it. Excludes ';'/':'
# on purpose (';' ends the '&amp;' entity → peeling it would corrupt a query ending in a real '&').
_URL_TRAIL = ".,!?"


def _linkify(text: str) -> str:
    """Make http(s) URLs clickable in ALREADY-ESCAPED plain text, reproducing the sender's URL
    byte-for-byte in the href. A real '&' (escaped to '&amp;') stays inside the link; a trailing
    '&gt;' (from a '<URL>' wrapper), '&quot;' (from '"URL"') or sentence punctuation is left
    OUTSIDE it, so a scheduling / meeting token never gets a stray '>' / '.' appended."""
    def _repl(m: "_re.Match") -> str:
        url = m.group(0)
        trail = ""
        while url:
            if url[-1] in _URL_TRAIL:
                trail, url = url[-1] + trail, url[:-1]
            elif url[-1] == ")" and "(" not in url:      # unbalanced ')' from '(URL)'
                trail, url = url[-1] + trail, url[:-1]
            else:
                break
        return f'<a href="{url}" target="_blank" rel="noopener">{url}</a>{trail}'
    return _URL_RE.sub(_repl, text)


# Recruiter/ATS scheduling mail is multipart: the HTML part carries the real link inside an
# <a href="…">Share your availability here</a>, but the PLAIN alternative keeps only the anchor
# TEXT and drops the URL. We prefer the plain part (renders cleanly on iOS), so that link is lost.
# _html_links pulls the http(s) anchors out of the HTML so they can be surfaced as a clickable
# block whenever the plain body carries no URL of its own (the link-loss case).
_A_TAG = _re.compile(r'<a\b[^>]*?href=(["\']?)(https?://[^"\'>\s]+)\1[^>]*>(.*?)</a>', _re.I | _re.S)
_STRIP_TAGS = _re.compile(r'<[^>]+>')
_LINK_SKIP = _re.compile(r'(unsubscribe|list-manage|/preferences|/privacy|email_preferences|opt[-_]?out)', _re.I)
_HAS_URL = _re.compile(r'https?://', _re.I)


def _html_links(html: str):
    """[(url, label)] http(s) anchors from an HTML mail body — deduped, unsubscribe/footer noise
    dropped, label from the anchor text (falls back to the URL)."""
    out, seen = [], set()
    for _q, url, inner in _A_TAG.findall(html or ""):
        u = unescape(url)
        if u in seen or _LINK_SKIP.search(u):
            continue
        seen.add(u)
        label = unescape(_STRIP_TAGS.sub("", inner)).strip()
        out.append((u, label or u))
    return out


_ACTION_LINK_RE = _re.compile(
    r"assessment|apply|complete|verif|schedul|interview|\bstart\b|begin|\baccess\b|portal|"
    r"sign ?in|log ?in|confirm|upload|availab|calendar|meeting|zoom|teams|book|"
    r"self-?schedul|click here|view (job|position|details)|next steps?|take (the |your )?(test|survey)",
    _re.I)


def _extra_links_block(m: dict, plain_text: str) -> str:
    """A clickable «Ссылки из письма» block for links kept only in the HTML part.
    - If the rendered plain body has NO URL of its own → surface all HTML anchors (a
      scheduling/meeting link that lives only in HTML would otherwise be unreachable).
    - If the plain body DOES have URLs (e.g. footnote-style '[1] Assessment Link' + a raw
      '[1] https://…' at the bottom) → surface ONLY the ACTIONABLE named anchors (Assessment
      Link / Apply / Schedule / Verify …) so the reader gets a clean button instead of hunting
      the footnote, WITHOUT pulling in footer/logo noise. Requires a real text label."""
    links = _html_links(m.get("html") or "")
    if not links:
        return ""
    if plain_text and _HAS_URL.search(plain_text):
        links = [(u, lbl) for (u, lbl) in links
                 if lbl and lbl != u and _ACTION_LINK_RE.search(lbl)]
        if not links:
            return ""
    items = "".join(
        f'<a class="ml-link" href="{escape(u, quote=True)}" target="_blank" rel="noopener">'
        f'{escape(lbl[:90])}</a>' for (u, lbl) in links[:12])
    return f'<div class="msg-links"><div class="ml-hd">Ссылки из письма</div>{items}</div>'


_BULLET = {"*", "•", "-", "·", "◦", "▪", "‣", "*"}


def _clean_plain(text: str) -> str:
    """Tidy the plain-text body of an HTML email that was flattened badly: HTML lists become
    a lone bullet marker on one line and the item text on the next ('  *\\nsoft skills…'). Merge
    each such pair into a single '• item' line, and collapse runs of blank lines."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, i = [], 0
    while i < len(lines):
        if lines[i].strip() in _BULLET and i + 1 < len(lines) and lines[i + 1].strip():
            out.append("• " + lines[i + 1].strip())
            i += 2
            continue
        out.append(lines[i])
        i += 1
    return _re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def fulldate(ts: int) -> str:
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    return f"{d.day} {_MONTHS[d.month]} {d.year}, {d.strftime('%H:%M')}"


# ---------------------------------------------------------------- CSS (ported)
_CSS = """
:root{--bg-app:#f6f8fc;--panel:#fff;--panel-2:#f1f3f4;--ink:#202124;--ink-soft:#5f6368;--ink-mute:#80868b;--line:#e8eaed;--line-strong:#dadce0;--accent:#1a73e8;--accent-deep:#1762c4;--accent-soft:#e8f0fe;--danger:#d93025;--r:12px;--r-sm:8px;--r-full:999px;--ff:'Hanken Grotesk',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;--ff-mono:'JetBrains Mono',ui-monospace,monospace;--sidebar-w:64px;}
*{box-sizing:border-box;}html,body{margin:0;overflow-x:hidden;max-width:100%;touch-action:manipulation;-webkit-text-size-adjust:100%;}
body{font-family:var(--ff);font-size:13.5px;line-height:1.5;color:var(--ink);background:var(--bg-app);-webkit-font-smoothing:antialiased;}
a{color:var(--accent);text-decoration:none;}a:hover{text-decoration:underline;}
.layout{display:flex;min-height:100vh;}
.sidebar{width:var(--sidebar-w);background:#fff;border-right:1px solid var(--line);padding:16px 0;display:flex;flex-direction:column;align-items:center;position:sticky;top:0;height:100vh;gap:6px;}
.sidebar .brand{width:34px;height:34px;border-radius:9px;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:15px;margin-bottom:6px;overflow:hidden;padding:0;}
.jf-logo{width:100%;height:100%;object-fit:cover;display:block;}
.gm-ava,.gm-drawer-head .brand{overflow:hidden;padding:0;}
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
.hbtn{display:inline-flex;align-items:center;gap:7px;background:var(--panel);color:var(--ink-soft);border:1px solid var(--line-strong);padding:9px 14px;min-height:40px;border-radius:var(--r-full);font-weight:600;font-size:13px;cursor:pointer;}
.hbtn:hover{background:var(--panel-2);color:var(--ink);}
.hbtn svg{width:15px;height:15px;}
.hbtn-lbl{display:inline;}
.fab-compose{display:none;}
.hbtn.danger{color:var(--danger);border-color:#f3c7c2;}.hbtn.danger:hover{background:var(--danger);color:#fff;}
/* Round borderless icon buttons (Gmail-style message toolbar: back, delete). */
.iconbtn{display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto;width:40px;height:40px;min-width:40px;border:0;background:transparent;color:var(--ink-soft);border-radius:50%;cursor:pointer;padding:0;}
.iconbtn:hover{background:rgba(60,64,67,.09);color:var(--ink);text-decoration:none;}
.iconbtn svg{width:20px;height:20px;}
.iconbtn.danger{color:var(--danger);}
.iconbtn.danger:hover{background:rgba(217,48,37,.1);color:var(--danger);}
/* Gmail-style reply bar at the end of a conversation. */
.reply-bar{display:flex;gap:12px;margin-top:20px;}
.reply-bar .reply-btn{flex:1;justify-content:center;}
.reply-btn{display:inline-flex;align-items:center;gap:9px;border:1px solid var(--line-strong);background:var(--panel);color:var(--ink);border-radius:var(--r-full);padding:11px 24px;font-size:14.5px;font-weight:600;cursor:pointer;min-height:46px;}
.reply-btn svg{width:18px;height:18px;}
.reply-btn:hover{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-deep);}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
input,select,textarea{font:inherit;color:var(--ink);padding:9px 13px;border:1px solid var(--line-strong);border-radius:var(--r-sm);background:var(--panel);}
input::placeholder,textarea::placeholder{color:var(--ink-mute);}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgb(26 115 232/.15);}
input[type=search]{min-width:240px;}textarea{width:100%;min-height:150px;resize:vertical;}
label{display:block;font-weight:600;color:var(--ink-soft);margin:12px 0 5px;font-size:12px;}
button.primary{font:inherit;font-weight:600;cursor:pointer;border:0;padding:10px 16px;border-radius:var(--r-full);background:var(--accent);color:#fff;font-size:13.5px;}
button.primary:hover{background:var(--accent-deep);}
.ghost{background:var(--panel);color:var(--ink-soft);border:1px solid var(--line-strong);padding:11px 15px;border-radius:var(--r-full);font-weight:600;cursor:pointer;font-size:13px;}
.ghost:hover{background:var(--panel-2);color:var(--ink);text-decoration:none;}
.filterbar{background:var(--panel);border:1px solid var(--line);border-radius:var(--r-sm);padding:9px 14px;margin-bottom:16px;color:var(--ink-soft);}
.maillist{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;}
.mitem{display:flex;align-items:flex-start;border-bottom:1px solid var(--line);}
.mitem:last-child{border-bottom:0;}
.mitem:hover{background:#f8fafd;}
.mitem.unread{background:#f8fbff;}
.mitem.selected{background:var(--accent-soft);}
.mrow{flex:1;min-width:0;display:flex;align-items:flex-start;padding:11px 18px 11px 0;color:var(--ink);}
.mrow:hover{text-decoration:none;}
/* avatar = Gmail-style select toggle: tap it to select the row (checkmark), tap the body to open */
.msel{position:relative;flex:0 0 auto;border:0;background:transparent;padding:0;margin:12px 13px 0 18px;width:34px;height:34px;border-radius:50%;cursor:pointer;}
.msel .avatar{margin:0;transition:opacity .1s;}
.msel .selcheck{position:absolute;inset:0;display:none;align-items:center;justify-content:center;background:var(--accent);color:#fff;border-radius:50%;}
.msel .selcheck svg{width:18px;height:18px;}
.msel:hover::after{content:"";position:absolute;inset:-4px;border-radius:50%;background:rgba(60,64,67,.09);}
.mitem.selected .msel .avatar{opacity:0;}
.mitem.selected .msel .selcheck{display:flex;}
/* selection action bar (shown when >=1 row is selected) */
.sel-bar{display:flex;align-items:center;gap:8px;margin:0 0 14px;padding:7px 10px;background:var(--accent-soft);border-radius:var(--r);}
.sel-bar[hidden]{display:none;}
.sel-count{font-weight:700;color:var(--accent-deep);font-size:15px;min-width:18px;text-align:center;}
.sel-link{border:0;background:transparent;color:var(--accent);font-weight:600;font-size:14px;cursor:pointer;padding:7px 9px;border-radius:var(--r-sm);}
.sel-link:hover{background:rgba(26,115,232,.12);}
.sel-bar .iconbtn{color:var(--accent-deep);}
.sel-bar .iconbtn:hover{background:rgba(26,115,232,.14);color:var(--accent-deep);}
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
.healthbar{background:#fef7e0;border:1px solid #fdd663;color:#7c5b00;border-radius:10px;padding:11px 14px;margin:0 0 14px;font-size:13.5px;font-weight:500;}
.msg-toolbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:8px;margin-bottom:12px;padding:8px 0;flex-wrap:nowrap;background:var(--bg-app);}
.mf-reply{flex:0 0 auto;margin-left:auto;align-self:flex-start;display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border:0;background:transparent;color:var(--ink-soft);border-radius:50%;cursor:pointer;}
.mf-reply:hover{background:rgba(60,64,67,.09);color:var(--accent);}
.mf-reply svg{width:19px;height:19px;}
.reply-btn.primary-btn{background:var(--accent);border-color:var(--accent);color:#fff;}
.reply-btn.primary-btn:hover{background:var(--accent-deep);border-color:var(--accent-deep);color:#fff;}
.msg-toolbar .spacer{flex:1;}.msg-toolbar form{margin:0;}
.msg-page{max-width:840px;}
.msg-subject{font-size:22px;font-weight:600;letter-spacing:-.01em;margin:0 0 18px;line-height:1.25;}
.msg-from{display:flex;gap:13px;align-items:flex-start;padding-bottom:0;margin-bottom:14px;}
.msg-from .avatar{width:42px;height:42px;font-size:17px;}
.mf-meta{min-width:0;flex:1;}.mf-line{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;}
.mf-line b{font-size:15px;color:var(--ink);}
.mf-addr{font-family:var(--ff-mono);font-size:12px;color:var(--ink-mute);}
.mf-to{font-size:12.5px;color:var(--ink-mute);margin-top:5px;}.mf-dot{margin:0 7px;opacity:.6;}
.mail-frame-wrap{overflow-x:auto;overflow-y:hidden;-webkit-overflow-scrolling:touch;}.mail-frame{width:100%;border:0;background:transparent;display:block;opacity:0;}.mail-frame.ready{opacity:1;}
.msg-content{white-space:pre-wrap;word-break:break-word;color:var(--ink);line-height:1.7;font-size:14.5px;}
.clip{flex:0 0 auto;font-size:12px;color:var(--ink-mute);}
.tcount{font-family:var(--ff-mono);font-size:12px;font-weight:400;color:#fff;background:var(--ink-mute);border-radius:var(--r-full);padding:1px 9px;margin-left:10px;vertical-align:middle;}
.tsub{font-family:var(--ff-mono);font-size:12px;color:var(--ink-mute);margin:-8px 0 18px;}
/* Full-width message body — no boxed card; messages are separated by a hairline only. */
.tcard{background:transparent;border:0;border-radius:0;padding:18px 0;margin:0;}
.tcard + .tcard{border-top:1px solid var(--line);}
.tcard.out .mf-line b{color:var(--accent-deep);}   /* our own messages: name tinted, no green box */
.tcard .msg-from{padding-bottom:14px;margin-bottom:16px;}
.atts{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;}
.att{display:inline-flex;align-items:center;gap:8px;background:var(--panel-2);border:1px solid var(--line-strong);border-radius:var(--r-sm);padding:8px 12px;color:var(--ink);max-width:280px;}
.att:hover{background:#e8f0fe;border-color:var(--accent);text-decoration:none;}
.att-ic{font-size:15px;}.att-nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px;font-weight:500;}
.att-sz{font-family:var(--ff-mono);font-size:10.5px;color:var(--ink-mute);margin-left:auto;}
.funnel{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 16px;}
/* Compact mobile filter trigger + the stage section of the filter modal: desktop hides
   both (it uses the inline chip slider), mobile shows them. */
.filter-btn{display:none;}
.fm-stages{display:none;}
.candidate-tools{display:flex;align-items:center;gap:8px;margin:0 0 14px;}
.candidate-tools input{width:min(420px,100%);}
.funnel.busy{opacity:.65;pointer-events:none}.fbtn.pending{border-color:var(--accent);color:var(--accent);}
.filter-status{min-height:18px;margin:-8px 0 8px;color:var(--ink-mute);font-size:12px;}
.fbtn{display:inline-flex;align-items:center;gap:6px;padding:8px 13px;border-radius:var(--r-full);border:1px solid var(--line);background:var(--panel);color:var(--ink-soft);font-size:13px;font-weight:600;text-decoration:none;white-space:nowrap;min-height:38px;}
.fbtn b{font-family:var(--ff-mono);font-size:12.5px;color:var(--ink);}
.fbtn:hover{border-color:var(--accent);text-decoration:none;}
.fbtn.active{background:var(--accent);border-color:var(--accent);color:#fff;}
.fbtn.active b{color:#fff;}
.mbxlist{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;}
.mbxrow{display:flex;align-items:center;gap:12px;padding:11px 18px;border-bottom:1px solid var(--line);color:var(--ink);overflow:hidden;}
.mbxrow:last-child{border-bottom:0;}.mbxrow:hover{background:#f8fafd;text-decoration:none;}
.mbxrow .nm{font-weight:600;flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.mbxrow .em{font-family:var(--ff-mono);font-size:12px;color:var(--ink-mute);margin-left:auto;flex:0 1 auto;min-width:0;max-width:52%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:10px;}
.mbxrow .cnt{font-family:var(--ff-mono);font-size:11px;color:#fff;background:var(--accent);border-radius:var(--r-full);padding:1px 8px;}
.mbxrow .apps-chip{flex:0 0 auto;font-size:11.5px;font-weight:700;color:var(--accent);background:var(--accent-soft);border-radius:var(--r-full);padding:2px 9px;cursor:pointer;white-space:nowrap;}
.mbxrow .apps-chip:hover{background:#d7e6fd;}
/* Candidate applications page */
.capp-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:6px 0 16px;}
.capp-hbtns{display:flex;gap:10px;flex-wrap:wrap;margin-left:auto;}
.capp-back{font-size:13px;color:var(--ink-soft);}
.capp-name{font-size:20px;font-weight:700;color:var(--ink);}
.capp-em{font-family:var(--ff-mono);font-size:12.5px;color:var(--ink-mute);}
.capp-list{display:flex;flex-direction:column;gap:10px;}
.capp-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;}
.capp-co{font-size:12px;font-weight:700;color:var(--ink-mute);text-transform:uppercase;letter-spacing:.03em;}
.capp-ttl{font-size:15.5px;font-weight:600;color:var(--ink);margin:2px 0 6px;line-height:1.3;}
.capp-meta{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:12.5px;color:var(--ink-mute);margin-bottom:11px;}
.capp-tag{font-size:11px;font-weight:700;border-radius:var(--r-full);padding:2px 9px;}
.capp-sub{color:#188038;background:#e6f4ea;}
.capp-nosub{color:var(--ink-mute);background:var(--panel-2);}
.capp-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.capp-btn{display:inline-flex;align-items:center;gap:6px;border-radius:var(--r-full);padding:8px 15px;font-size:13px;font-weight:600;min-height:38px;text-decoration:none;cursor:pointer;}
.capp-btn.dl{background:var(--accent);color:#fff;border:none;}
.capp-btn.dl:hover{background:var(--accent-deep);text-decoration:none;}
.capp-btn.dl.off{background:var(--panel-2);color:var(--ink-mute);pointer-events:none;}
.capp-btn.ext{background:var(--panel);color:var(--ink-soft);border:1px solid var(--line-strong);}
.capp-btn.ext:hover{border-color:var(--accent);color:var(--ink);text-decoration:none;}
.modal{position:fixed;inset:0;z-index:50;display:none;align-items:flex-start;justify-content:center;padding:8vh 16px;background:rgba(32,33,36,.5);overflow-y:auto;-webkit-overflow-scrolling:touch;}
.modal.open{display:flex;}
.modal-card{width:100%;max-width:480px;background:var(--panel);border-radius:var(--r);padding:22px 24px;box-shadow:0 12px 40px -8px rgba(32,33,36,.3);}
.modal-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;}
.modal-head h3{font-size:18px;margin:0;font-weight:600;}
.modal-head .x{background:none;border:0;font-size:24px;color:var(--ink-mute);cursor:pointer;padding:6px 10px;min-width:40px;min-height:40px;line-height:1;}
.modal form input,.modal form textarea,.modal form select{width:100%;}
.modal-actions{margin-top:16px;}.modal-actions .primary{width:100%;}
.sendmsg{margin-top:10px;font-size:13px;}
/* Filter modal (stage picker + keyword editor combined) */
.fm-card{max-width:520px;}
.fm-lbl{font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:var(--ink-mute);margin:4px 0 8px;}
.fm-list{display:flex;flex-direction:column;gap:2px;margin-bottom:6px;}
.fm-stage{display:flex;align-items:center;gap:10px;padding:11px 12px;border-radius:var(--r-sm);color:var(--ink-soft);font-size:14.5px;font-weight:500;}
.fm-stage:hover{background:var(--panel-2);color:var(--ink);text-decoration:none;}
.fm-stage-lbl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.fm-stage-n{flex:0 0 auto;font-family:var(--ff-mono);font-size:12px;color:var(--ink-mute);background:var(--panel-2);border-radius:var(--r-full);padding:1px 9px;min-width:24px;text-align:center;}
.fm-stage.active{background:var(--accent-soft);color:var(--accent-deep);font-weight:600;}
.fm-stage.active .fm-stage-n{background:var(--accent);color:#fff;}
.fm-kw{border-top:1px solid var(--line);margin-top:14px;padding-top:6px;}
.fm-kw>summary{list-style:none;cursor:pointer;font-size:15px;font-weight:600;color:var(--ink);padding:10px 2px;display:flex;align-items:center;gap:8px;}
.fm-kw>summary::-webkit-details-marker{display:none;}
.fm-kw>summary::before{content:"";width:8px;height:8px;border-right:2px solid var(--ink-mute);border-bottom:2px solid var(--ink-mute);transform:rotate(-45deg);transition:transform .18s;}
.fm-kw[open]>summary::before{transform:rotate(45deg);}
.fm-hint{font-size:12.5px;color:var(--ink-mute);margin:0 0 12px;line-height:1.5;}
.kw-fields{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px;}
.kw-field{display:flex;flex-direction:column;gap:5px;font-size:12.5px;font-weight:600;color:var(--ink-soft);}
.kw-field textarea{min-height:78px;font-family:var(--ff-mono);font-weight:400;font-size:12.5px;line-height:1.5;border:1px solid var(--line-strong);border-radius:var(--r-sm);padding:8px 10px;background:var(--panel);color:var(--ink);resize:vertical;}
.fm-kw .primary{width:100%;}
.fm-reset{margin-top:10px;}.fm-reset .ghost{width:100%;justify-content:center;}
.kw-toast{position:fixed;left:50%;bottom:24px;transform:translate(-50%,16px);z-index:80;background:var(--ink);color:#fff;font-size:13.5px;font-weight:500;padding:11px 18px;border-radius:var(--r-full);box-shadow:0 8px 24px -6px rgba(32,33,36,.5);opacity:0;transition:opacity .25s,transform .25s;pointer-events:none;max-width:88vw;text-align:center;}
.kw-toast.show{opacity:1;transform:translate(-50%,0);}
.keyword-intro{max-width:760px;color:var(--ink-soft);margin:-4px 0 18px;}
.keyword-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;max-width:920px;}
.keyword-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px;}
.keyword-card h2{font-size:16px;margin:0 0 4px;}.keyword-card p{font-size:12.5px;color:var(--ink-mute);margin:0 0 10px;}
.keyword-card textarea{min-height:220px;font-family:var(--ff-mono);font-size:12.5px;line-height:1.55;}
.keyword-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;max-width:920px;margin-top:16px;}
.keyword-note{background:var(--accent-soft);color:var(--accent-deep);border-radius:var(--r-sm);padding:10px 13px;margin:0 0 14px;max-width:920px;}
@media(max-width:760px){.sidebar{width:auto;height:auto;position:static;flex-direction:row;border-right:0;border-bottom:1px solid var(--line);padding:8px;}.sidebar .brand{margin:0 6px 0 0;}main{padding:12px;}.seg-nav a{font-size:19px;}.toolbar{width:100%;}.toolbar input[type=search]{flex:1;min-width:0;}.msender{max-width:140px;}input,select,textarea{font-size:16px;}.modal textarea{min-height:110px;}.modal{padding:4vh 12px;}.sidebar .nav a{font-size:10.5px;flex-direction:column;gap:2px;width:auto;flex:1;min-width:0;padding:6px 3px;text-align:center;line-height:1.15;}.sidebar .nav a svg{width:19px;height:19px;}body{font-size:15px;}.msnip{font-size:13.5px;}.layout{flex-direction:column;}.sidebar{justify-content:flex-start;padding:6px 8px;}.sidebar .nav{display:flex;flex:1;flex-direction:row;gap:2px;justify-content:space-around;align-items:stretch;}}
/* iOS auto-zooms the page when a focused input's font-size is < 16px. The generic
   `input,select,textarea{font-size:16px}` above is low-specificity, so a class rule
   (e.g. .cat-company input[list]) can shrink it back below 16 and re-trigger the zoom.
   Force >=16px on EVERY form control on mobile so tapping any input never zooms. */
@media(max-width:760px){input,select,textarea,input[list]{font-size:16px!important;}}
/* Gmail-style mobile top bar + slide-out drawer (mobile only; desktop keeps .sidebar) */
.gm-topbar{display:none;padding:8px 12px 4px;}
.gm-pill{display:flex;align-items:center;gap:4px;background:var(--panel-2);border-radius:var(--r-full);padding:4px 6px 4px 4px;}
.gm-burger{background:none;border:0;padding:0;width:40px;height:40px;min-width:40px;display:flex;align-items:center;justify-content:center;color:var(--ink-soft);cursor:pointer;border-radius:50%;}
.gm-burger:hover{background:rgba(60,64,67,.08);}
.gm-burger svg{width:22px;height:22px;}
.gm-search{flex:1;min-width:0;margin:0;display:flex;}
.gm-search input[type=search]{flex:1;width:100%;min-width:0;border:0;background:transparent;padding:9px 4px;font-size:16px;border-radius:0;}
.gm-search input[type=search]:focus{outline:none;box-shadow:none;border:0;}
.gm-title{flex:1;font-weight:600;color:var(--ink-soft);font-size:16px;padding-left:6px;}
.gm-ava{flex:0 0 auto;width:32px;height:32px;border-radius:50%;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:12.5px;margin-right:2px;}
.gm-scrim{position:fixed;inset:0;background:rgba(32,33,36,.5);z-index:60;opacity:0;visibility:hidden;transition:opacity .2s;}
.gm-scrim.open{opacity:1;visibility:visible;}
.gm-drawer{position:fixed;top:0;left:0;bottom:0;width:284px;max-width:82vw;background:var(--panel);z-index:61;transform:translateX(-102%);transition:transform .22s ease;box-shadow:0 0 40px -8px rgba(32,33,36,.45);display:flex;flex-direction:column;padding:6px 0;}
.gm-drawer.open{transform:translateX(0);}
.gm-drawer-head{display:flex;align-items:center;gap:12px;padding:16px 18px 14px;}
.gm-drawer-head .brand{width:34px;height:34px;border-radius:9px;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:15px;}
.gm-drawer-head b{font-size:17px;color:var(--ink);}
.gm-drawer-nav{display:flex;flex-direction:column;padding:6px 8px;gap:2px;}
.gm-drawer-nav a{display:flex;align-items:center;gap:16px;padding:12px 16px;border-radius:var(--r-full);color:var(--ink-soft);font-weight:600;font-size:15px;}
.gm-drawer-nav a svg{width:22px;height:22px;flex:0 0 auto;}
.gm-drawer-nav a span{flex:1;}
.gm-drawer-nav a.active{background:var(--accent-soft);color:var(--accent-deep);}
.gm-drawer-nav a:hover{background:var(--panel-2);text-decoration:none;}
.gm-drawer-role{margin-left:auto;font-size:10.5px;font-weight:700;color:#b45309;background:#fef3c7;padding:2px 9px;border-radius:var(--r-full);}
.gm-drawer-foot{margin-top:auto;padding:10px 12px;border-top:1px solid var(--line);}
.gm-logout{display:flex;align-items:center;gap:16px;padding:12px 16px;border-radius:var(--r-full);color:var(--danger);font-weight:600;font-size:15px;}
.gm-logout svg{width:22px;height:22px;flex:0 0 auto;}
.gm-logout:hover{background:#fce8e6;text-decoration:none;}
/* desktop sidebar footer: admin marker + logout, pinned to the bottom of the rail */
.side-foot{margin-top:auto;display:flex;flex-direction:column;align-items:center;gap:4px;padding:8px 0 2px;width:100%;}
.side-role{font-size:8px;font-weight:700;letter-spacing:.06em;color:var(--ink-mute);text-transform:uppercase;}
.side-logout{display:flex;flex-direction:column;align-items:center;gap:3px;color:var(--ink-mute);padding:8px 6px;border-radius:var(--r-sm);font-size:9.5px;width:52px;text-align:center;}
.side-logout svg{width:20px;height:20px;}
.side-logout:hover{color:var(--danger);background:#fce8e6;text-decoration:none;}
@media(max-width:760px){.gm-topbar{display:block;}.sidebar{display:none;}.toolbar{display:none;}}
/* Mobile: the search pill hides on scroll down and reveals on scroll up (Gmail). It is FIXED
   (position:sticky is unreliable under the global overflow-x:hidden); the tabs scroll in flow
   beneath it, so main gets top padding to clear the fixed pill. */
@media(max-width:760px){
  .gm-topbar{position:fixed;top:0;left:0;right:0;z-index:16;background:var(--bg-app);transition:transform .28s ease;}
  .gm-topbar.hide{transform:translateY(-100%);}
  .page-head{position:static;}
  .page-head.hide{transform:none;}
  main{padding-top:62px;}
}
/* Mobile inbox: Compose becomes a Gmail-style floating action button (bottom-right); the
   header keyword button is hidden (keywords live in the «Фильтр» modal on mobile). */
@media(max-width:760px){
  .head-actions{gap:6px;}
  .hbtn-compose,.hbtn-kw{display:none;}
  .fab-compose{display:inline-flex;align-items:center;gap:9px;position:fixed;right:16px;
    bottom:calc(16px + env(safe-area-inset-bottom));z-index:40;background:var(--accent);color:#fff;
    border:none;border-radius:16px;height:52px;padding:0 20px;font-size:14.5px;font-weight:600;
    cursor:pointer;box-shadow:0 6px 18px -4px rgba(26,115,232,.55);overflow:hidden;
    transition:padding .26s cubic-bezier(.4,0,.2,1),gap .26s cubic-bezier(.4,0,.2,1),border-radius .26s cubic-bezier(.4,0,.2,1);}
  .fab-compose svg{width:22px;height:22px;flex:0 0 auto;}
  .fab-compose span{white-space:nowrap;overflow:hidden;max-width:130px;transition:max-width .26s cubic-bezier(.4,0,.2,1),opacity .2s ease;}
  /* scrolling down collapses it to a round pen (Gmail); scrolling up expands it back.
     Width is left auto so it follows the animating label — no jump from an `auto` width. */
  .fab-compose.collapsed{padding:0 15px;gap:0;border-radius:50%;}
  .fab-compose.collapsed span{max-width:0;opacity:0;}
  .fab-compose:active{transform:translateY(1px);}
  main{padding-bottom:92px;}
}
@media(max-width:760px){
  .candidate-tools{display:none;}
  /* The chip slider is desktop-only; mobile uses a compact «Фильтр» button (in the header,
     right of the tabs) + a modal that holds the stage picker and the keyword editor. */
  .funnel{display:none;}
  /* the funnel AJAX status line is desktop-only (mobile filters via full reload) — hide the
     empty 18px placeholder that left dead space under the Инбокс/Кандидаты tabs. */
  .filter-status{display:none;}
  .page-head{margin-bottom:14px;}
  .seg-nav{gap:14px;}
  .filter-btn{display:inline-flex;align-items:center;gap:6px;margin:0;min-height:36px;max-width:44vw;
    padding:0 13px;border:1px solid var(--line-strong);border-radius:var(--r-full);
    background:var(--panel);color:var(--ink);font-size:13.5px;font-weight:600;cursor:pointer;}
  .filter-btn svg{width:15px;height:15px;flex:0 0 auto;color:var(--ink-soft);}
  .filter-btn span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .filter-btn:active{background:var(--panel-2);}
  .fm-stages{display:block;}                  /* stage picker shows inside the modal on mobile */
  .fm-card{max-width:none;}
  .kw-fields{grid-template-columns:1fr;}      /* stack keyword fields on narrow screens */
  .msg-toolbar{align-items:center;margin-bottom:16px;}
  .msg-toolbar .iconbtn{width:44px;height:44px;}
  /* Open-message full screen: fix the back/delete bar to the top so you never scroll up to
     leave (position:fixed is reliable here; sticky breaks under the body overflow-x:hidden). */
  body.full-view .msg-toolbar{position:fixed;top:0;left:0;right:0;z-index:30;margin:0;padding:8px 10px;background:var(--bg-app);box-shadow:0 1px 0 var(--line);-webkit-backface-visibility:hidden;backface-visibility:hidden;transform:translateZ(0);}
  body.full-view main{padding-top:60px;padding-bottom:78px;}
  /* Reply + Forward = a fixed bottom bar that moves with scroll: Ответить at the left edge,
     Переслать at the right edge. */
  .reply-bar{position:fixed;left:0;right:0;bottom:0;z-index:25;margin:0;gap:10px;
    padding:9px 12px calc(9px + env(safe-area-inset-bottom));background:var(--bg-app);
    border-top:1px solid var(--line);-webkit-backface-visibility:hidden;backface-visibility:hidden;transform:translateZ(0);}
  .mbxrow{padding:11px 12px;gap:9px;}
  .mbxrow .em{display:none;}
  .keyword-grid{grid-template-columns:1fr}.keyword-card textarea{min-height:180px}.keyword-actions>*{flex:1 1 auto;text-align:center;justify-content:center;min-height:44px;}
}
/* NB: the catalog's own .cat-search is hidden inside catalog_ui _CAT_CSS, whose
   later .cat-search{display:flex} would otherwise override a rule placed here. */

/* ---- premium micro-interactions — subtle, consistent across every shell page.
   Centralised here (the shell _CSS is included on every page via _page) so buttons,
   cards, links and rows animate the same everywhere without per-file duplication. ---- */
html{scroll-behavior:smooth;}
a,button,.hbtn,.primary,.ghost,.iconbtn,.filter-btn,.fbtn,.chip,.tag,.pill,.seg-nav a,.nav a,.fab-compose{
  transition:background-color .15s ease,border-color .15s ease,color .15s ease,box-shadow .15s ease,transform .12s ease;}
button:not(:disabled):hover,.hbtn:hover,.primary:hover,.ghost:hover,.iconbtn:hover,.filter-btn:hover,.fbtn:hover{
  transform:translateY(-1px);box-shadow:0 4px 14px -6px rgba(15,23,42,.20);}
button:not(:disabled):active,.hbtn:active,.primary:active,.ghost:active,.iconbtn:active,.filter-btn:active,.fbtn:active{
  transform:translateY(0);box-shadow:none;}
.fab-compose:hover{transform:translateY(-2px);box-shadow:0 12px 26px -8px rgba(12,71,194,.42);}
.mbxrow,.cg-card,.cat-card,.u-user,.iv-cell,.mh-card,.tcard,.capp-card{
  transition:border-color .15s ease,box-shadow .18s ease,transform .18s ease;}
.cg-card:hover,.cat-card:hover,.u-user:hover,.mh-card:hover,.tcard:hover,.capp-card:hover{
  border-color:var(--accent);box-shadow:0 6px 20px -10px rgba(15,23,42,.22);}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,
.hbtn:focus-visible,.iconbtn:focus-visible,.filter-btn:focus-visible,[role=button]:focus-visible{
  outline:2px solid var(--accent);outline-offset:2px;}
@keyframes jfFadeIn{from{opacity:0;transform:translateY(8px) scale(.985);}to{opacity:1;transform:none;}}
/* modal/sheet open animation (the catalog modal keeps its own cm-pop; these are the
   shell compose/filter cards + the interview-assign panel, which had none). */
.modal-card,.fm-card,.iv-modal-panel{animation:jfFadeIn .18s ease;}
/* surfaced links from an HTML mail whose plain part dropped the URL (scheduling/meeting) */
.msg-links{margin-top:10px;padding:10px 13px;background:var(--panel-2);border:1px solid var(--line);
  border-radius:var(--r-sm);}
.msg-links .ml-hd{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;
  color:var(--ink-mute);margin-bottom:6px;}
.msg-links .ml-link{display:block;color:var(--accent);text-decoration:none;font-weight:600;
  padding:5px 0;word-break:break-word;min-height:22px;}
.msg-links .ml-link:hover{text-decoration:underline;}
@media (prefers-reduced-motion: reduce){
  *{animation-duration:.001ms!important;animation-iteration-count:1!important;
    transition-duration:.001ms!important;scroll-behavior:auto!important;}
}
"""

_FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
          '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
          '<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700'
          '&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">')


# Navigation is intentionally reduced to the 3 tabs that matter (per Alan,
# 2026-08-20): Кандидаты (mail/CRM entry — the general Инбокс stays reachable via
# the in-page tab strip), Каталог (the single remote-only source of jobs + form
# questions), Заявки (the one-click submit queue). The old Инбокс/Вакансии (/jobs)
# / Компании (/roles) sidebar items were duplicate job-browsing surfaces and were
# removed. Routes still exist; they're just no longer in the nav. One list drives
# both the desktop rail (_sidebar) and the mobile drawer (_drawer).
_IC_CANDIDATES = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/></svg>'
_IC_CATALOG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1"/><path d="M9 12h6M9 16h4"/></svg>'
_IC_INBOX = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>'
_IC_UNFINISHED = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>'
_IC_MASS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>'
_IC_STATS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>'
_IC_USERS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'
_IC_LOGOUT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>'
_NAV = [
    ("/mail/candidates", "candidates", "Кандидаты", _IC_CANDIDATES),
    ("/catalog", "catalog", "Каталог", _IC_CATALOG),
    ("/unfinished", "unfinished", "Незавершённые", _IC_UNFINISHED),
    ("/mass-hiring", "masshiring", "Mass Hiring", _IC_MASS),
    ("/stats", "stats", "Статистика", _IC_STATS),
    ("/users", "users", "Пользователи", _IC_USERS),
]
# Per-screen context for the Gmail-style mobile search pill: active -> (route,
# placeholder). Screens absent here (e.g. Заявки) show a title instead of a field.
_SEARCH_CTX = {
    "inbox": ("/mail", "Поиск в почте"),
    "candidates": ("/mail/candidates", "Поиск кандидата"),
    "catalog": ("/catalog", "Поиск вакансий"),
}


# The brand mark — the JF logo (a blue square, rounded by its container). Replaces the old
# "JF" text badge everywhere the brand appears (rail, mobile pill, drawer).
_LOGO_IMG = "<img src='/static/logo.svg' alt='JobFinder' class='jf-logo'>"

# PWA install (manifest + icons + theme) + a service worker registration. Injected into
# every page's <head> / end-of-body; the assets are on the dash_auth public allowlist.
_HEAD_PWA = (
    "<link rel='manifest' href='/static/manifest.webmanifest'>"
    "<meta name='theme-color' content='#0c47c2'>"
    "<meta name='apple-mobile-web-app-capable' content='yes'>"
    "<meta name='apple-mobile-web-app-status-bar-style' content='default'>"
    "<meta name='apple-mobile-web-app-title' content='JobFinder'>"
    "<link rel='apple-touch-icon' href='/static/apple-touch-icon.png'>"
    "<link rel='icon' type='image/png' sizes='32x32' href='/static/favicon-32.png'>"
    "<link rel='icon' href='/static/logo.svg' type='image/svg+xml'>")
_SW_REG = ("<script>if('serviceWorker' in navigator){window.addEventListener('load',function(){"
           "navigator.serviceWorker.register('/sw.js').catch(function(){});});}</script>")


def _nav_links(active: str) -> str:
    return "".join(
        f'<a class="{"active" if active == key else ""}" href="{href}">{svg}<span>{label}</span></a>'
        for href, key, label, svg in _NAV)


def _sidebar(active: str) -> str:
    """Desktop left rail. Hidden ≤760px, where _topbar + _drawer take over. The
    footer marks this as the admin portal and carries the logout control."""
    return (f'<aside class="sidebar"><div class="brand">{_LOGO_IMG}</div>'
            f'<div class="nav">{_nav_links(active)}</div>'
            '<div class="side-foot"><span class="side-role">Админ</span>'
            f'<a class="side-logout" href="/logout" title="Выйти из админки">{_IC_LOGOUT}'
            '<span>Выход</span></a></div></aside>')


def _topbar(active: str) -> str:
    """Gmail-style mobile top pill: ☰ (opens the drawer) + a context-aware search
    box + a decorative JF avatar. Shown only ≤760px via CSS; desktop keeps the rail."""
    burger = ('<button type="button" class="gm-burger" aria-label="Меню" onclick="gmDrawer(true)">'
              '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round"><path d="M3 6h18M3 12h18M3 18h18"/></svg></button>')
    ctx = _SEARCH_CTX.get(active)
    if ctx:
        route, ph = ctx
        mid = (f'<form class="gm-search" method="get" action="{route}" role="search">'
               f'<input type="search" name="q" placeholder="{ph}" autocomplete="off"></form>')
    else:
        lbl = next((l for _h, k, l, _s in _NAV if k == active), "")
        mid = f'<span class="gm-title">{lbl}</span>'
    return (f'<div class="gm-topbar"><div class="gm-pill">{burger}{mid}'
            f'<span class="gm-ava">{_LOGO_IMG}</span></div></div>')


def _drawer(active: str) -> str:
    """Slide-out menu behind the ☰ — the same 3 nav tabs. Mobile only."""
    return ('<div class="gm-scrim" onclick="gmDrawer(false)"></div>'
            '<aside class="gm-drawer"><div class="gm-drawer-head">'
            f'<span class="brand">{_LOGO_IMG}</span><b>JobFinder</b>'
            '<span class="gm-drawer-role">Админ</span></div>'
            f'<nav class="gm-drawer-nav">{_nav_links(active)}</nav>'
            '<div class="gm-drawer-foot">'
            f'<a class="gm-logout" href="/logout">{_IC_LOGOUT}<span>Выйти из админки</span></a>'
            '</div></aside>')


def _page(active: str, body: str, modal: str = "", topbar: bool = True) -> str:
    # topbar=False → a dedicated full screen (the open-message view): no Gmail search pill /
    # drawer, just the message's own sticky toolbar, like tapping a mail in Gmail.
    chrome = f"{_topbar(active)}{_drawer(active)}" if topbar else ""
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        + _HEAD_PWA +
        "<title>JobFinder — почта кандидатов</title>" + _FONTS +
        f"<style>{_CSS}</style></head><body{'' if topbar else ' class=full-view'}>"
        f"{chrome}"
        f"<div class='layout'>{_sidebar(active)}<main>{body}</main></div>{modal}"
        + _JS + _SW_REG + "</body></html>")


def _kind_tag(kind: str) -> str:
    emoji, label, fg, bg = _KIND.get(kind, _KIND["other"])
    if not label:
        return ""
    return f'<span class="tag" style="color:{fg};background:{bg}">{emoji} {escape(label)}</span>'


def _iv_sobes(mailbox: str, thread: str, hash: str, as_span: bool = False) -> str:
    """The «Собес» (interview-assign) control. Imported lazily so a problem in the
    interviews package can never break inbox rendering (returns ""). `as_span` renders
    a <span role=button> for placement inside a row <a> (valid HTML)."""
    try:
        from backend.interviews import operator_ui
        return operator_ui.sobes_button(mailbox or "", thread or "", hash or "",
                                        as_span=as_span)
    except Exception:
        return ""


def _iv_modal() -> str:
    """The one-time #ivModal shell, injected into the inbox/thread page. Lazy + safe."""
    try:
        from backend.interviews import operator_ui
        return operator_ui.modal_shell()
    except Exception:
        return ""


def render_rows(rows: list[dict], show_mailbox: bool = True,
                show_sobes: bool = True, read_only: bool = False) -> str:
    # read_only=True (the responsible cabinet, a user-facing read surface): no operator
    # affordances — implies show_sobes=False, renders the avatar as a plain non-interactive
    # element (the `toggleSel` selector JS is operator-only), and drops the decorative 📎
    # attachment emoji. read_only=False (operator default) stays byte-identical.
    if read_only:
        show_sobes = False
    out = []
    for m in rows:
        sender = m.get("from_name") or m.get("from_email") or "?"
        unread = "" if m.get("seen") else " unread"
        mbox = (f'<span class="mbox">{escape((m.get("candidate") or m.get("mailbox") or "")[:22])}</span>'
                if show_mailbox else "")
        if read_only:
            clip = ('<span class="clip" title="вложение"></span>' if m.get("has_att") else "")
        else:
            clip = ('<span class="clip" title="есть вложение">📎</span>'
                    if m.get("has_att") else "")
        # «Собес» control lives INSIDE the row <a> (like the «📄 N» apps-chip); its
        # onclick stops propagation so tapping it opens the assign modal, not the message.
        # show_sobes=False (the read-only responsible cabinet) suppresses the operator control.
        sobes = (_iv_sobes(m.get("mailbox", ""),
                           m.get("thread") or m.get("thread_key") or "",
                           m.get("id", ""), as_span=True)
                 if (show_sobes and m.get("kind") == "interview") else "")
        if read_only:
            # plain, non-interactive avatar (no select-toggle button / no toggleSel JS)
            avatar = (f'<span class="msel msel-ro">'
                      f'<span class="avatar" style="background:{_avatar_color(sender)}">{escape(_initial(sender))}</span></span>')
        else:
            # avatar doubles as the Gmail-style select toggle; tapping it selects the row,
            # tapping the body opens the message
            avatar = (
                f'<button type="button" class="msel" onclick="toggleSel(this)" aria-label="Выбрать сообщение">'
                f'<span class="avatar" style="background:{_avatar_color(sender)}">{escape(_initial(sender))}</span>'
                '<span class="selcheck"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></span></button>')
        out.append(
            f'<div class="mitem{unread}" data-ts="{m.get("date_ts",0)}" data-id="{escape(m["id"])}" '
            f'data-mailbox="{escape(m.get("mailbox","") or "", quote=True)}">'
            f'{avatar}'
            f'<a class="mrow" href="/mail/message?id={escape(m["id"])}">'
            f'<span class="mbody"><span class="mtop">'
            f'<span class="msender">{escape(sender)}</span>{_kind_tag(m.get("kind","other"))}{mbox}{sobes}'
            f'{clip}<span class="mdate">{maildate(m.get("date_ts",0))}</span></span>'
            f'<span class="msubj">{escape(m.get("subject","") or "(без темы)")}</span>'
            f'<span class="msnip">{escape(m.get("snippet",""))}</span>'
            "</span></a></div>")
    return "".join(out)


def _strip_lead_icon(s: str) -> str:
    """Drop a leading emoji/symbol + space so a chip label ('📤 Отправленные') becomes a
    clean dropdown-option label ('Отправленные'). No-op when the label starts with a letter."""
    parts = s.split(" ", 1)
    if len(parts) == 2 and parts[0] and not parts[0][0].isalnum():
        return parts[1]
    return s


_KW_FIELDS = [("interview", "Собеседование"), ("offer", "Оффер"),
              ("rejection", "Отказ"), ("ack", "Заявка принята")]


def _filter_trigger(rows: list[dict]) -> str:
    """Compact mobile «Фильтр» button (shows the active stage) that opens the filter modal.
    Desktop hides it — there the chip slider + the header keyword button are used instead."""
    active = next((_strip_lead_icon(r["label"]) for r in rows if r["active"]), "Все")
    return ('<button type="button" class="filter-btn" onclick="openFilter(false)" '
            'aria-haspopup="dialog"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
            '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>'
            f'<span>{escape(active)}</span></button>')


def _filter_modal(rows: list[dict], keyword_rules: dict | None, current_path: str) -> str:
    """One modal that unifies the two filters: pick a stage (top) and edit the stage
    keyword rules (a collapsible section). Replaces the separate /mail/keywords page and the
    big mobile dropdown. Stage chips are links (full navigation); the keyword form posts to
    the existing endpoint with a `next` back-link."""
    stages_html = "".join(
        f'<a class="fm-stage{" active" if r["active"] else ""}" href="{escape(r["href"], quote=True)}">'
        f'<span class="fm-stage-lbl">{r["label"]}</span>'
        f'<span class="fm-stage-n">{r["count"]}</span></a>' for r in rows)
    rules = keyword_rules or {}
    fields = "".join(
        f'<label class="kw-field"><span>{title}</span>'
        f'<textarea name="{k}" rows="3" placeholder="Одна фраза на строку">'
        f'{escape(chr(10).join(rules.get(k, [])))}</textarea></label>'
        for k, title in _KW_FIELDS)
    nxt = escape(current_path, quote=True)
    return (
        '<div class="modal" id="filterModal"><div class="modal-card fm-card">'
        '<div class="modal-head"><h3>Фильтр</h3>'
        '<button class="x" onclick="closeFilter()" aria-label="Закрыть">×</button></div>'
        f'<div class="fm-stages"><div class="fm-lbl">Стадия</div>'
        f'<div class="fm-list">{stages_html}</div></div>'
        '<details class="fm-kw"><summary>Ключевые слова</summary>'
        '<p class="fm-hint">Одна фраза на строку. Если фраза есть в теме или тексте письма — '
        'ему назначается стадия. Приоритет: оффер → отказ → собеседование → принято.</p>'
        f'<form method="post" action="/mail/keywords"><input type="hidden" name="next" value="{nxt}">'
        f'<div class="kw-fields">{fields}</div>'
        '<button class="primary" type="submit">Сохранить и пересчитать</button></form>'
        f'<form method="post" action="/mail/keywords/reset" class="fm-reset">'
        f'<input type="hidden" name="next" value="{nxt}">'
        '<button class="ghost" type="submit">Стандартные слова</button></form>'
        '</details></div></div>')


def render_inbox(rows: list[dict], counts: dict, q: str = "", mailbox: str = "",
                 mailbox_name: str = "", page_size: int = 50, warning: str = "",
                 stage: str = "", stage_counts: dict | None = None,
                 keyword_rules: dict | None = None) -> str:
    has_more = 1 if len(rows) == page_size else 0
    unread = counts.get("unread", 0)
    ncand = counts.get("candidates", counts.get("mailboxes", 0))  # ALL candidates, not just those with mail
    inbox_badge = f' <b>{unread}</b>' if unread else ''
    sc = stage_counts or {}
    _stages = [("", "Все"), ("sent", "📤 Отправленные"), ("ack", "✅ Принято"),
               ("action_needed", "⚠️ Действие"), ("assessment_done", "🤖 Тест пройден"),
               ("interview", "📞 Собеседование"), ("offer", "🎉 Оффер"), ("rejection", "✕ Отказ"),
               ("code", "🔑 Коды"), ("other", "📁 Прочее")]
    def _href(key: str) -> str:
        params = {}
        if key:
            params["stage"] = key
        if q:
            params["q"] = q
        if mailbox:
            params["mailbox"] = mailbox
        return "/mail" + ("?" + urlencode(params) if params else "")
    def _n(key: str) -> int:
        return sc.get("all" if not key else key, 0)
    stage_rows = [{"label": l, "href": _href(k), "count": _n(k), "active": stage == k}
                  for k, l in _stages]
    head = (
        '<div class="page-head"><div class="ph-left"><div class="seg-nav">'
        f'<a class="active" href="/mail">Инбокс{inbox_badge}</a>'
        f'<a href="/mail/candidates">Кандидаты <b>{ncand}</b></a>'
        '</div><div class="head-actions">'
        # compact «Фильтр» trigger — on mobile it sits at the right of the tabs row
        + _filter_trigger(stage_rows) +
        # keyword editor lives in the filter modal now; the button opens it (href = no-JS fallback)
        '<a class="hbtn hbtn-kw" href="/mail/keywords" onclick="openFilter(true);return false;" '
        'title="Ключевые слова" aria-label="Ключевые слова">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><line x1="4" y1="8" x2="20" y2="8"/><line x1="4" y1="16" x2="20" y2="16"/><circle cx="9" cy="8" r="2.2" fill="var(--panel)"/><circle cx="15" cy="16" r="2.2" fill="var(--panel)"/></svg>'
        '<span class="hbtn-lbl">Ключевые слова</span></a>'
        '<button class="hbtn hbtn-compose" onclick="openCompose()">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>'
        '<span class="hbtn-lbl">Написать</span></button></div></div>'
        f'<form method="get" action="/mail" class="toolbar"><input type="search" name="q" value="{escape(q)}" placeholder="Поиск по теме / отправителю">'
        + (f'<input type="hidden" name="mailbox" value="{escape(mailbox)}">' if mailbox else "")
        + (f'<input type="hidden" name="stage" value="{escape(stage)}">' if stage else "")
        + '<button class="ghost" type="submit">Найти</button>'
        + (f'<a class="ghost" href="/mail">Сброс</a>' if (q or mailbox) else "")
        + '</form></div>')
    chips = "".join(
        f'<a class="fbtn{" active" if r["active"] else ""}" href="{escape(r["href"], quote=True)}">'
        f'{r["label"]} <b>{r["count"]}</b></a>' for r in stage_rows)
    funnel = ('<div class="funnel" data-filter-list="maillist">' + chips + '</div>'
              + '<div class="filter-status" role="status" aria-live="polite"></div>')
    modal = _COMPOSE_MODAL + _filter_modal(stage_rows, keyword_rules, _href(stage)) + _iv_modal()
    fbar = (f'<div class="filterbar">Ящик кандидата: <b>{escape(mailbox_name or mailbox)}</b> '
            f'<a href="/mail">убрать фильтр</a></div>' if mailbox else "")
    empty = '<div class="empty" id="filterempty">Писем нет</div>' if not rows else '<div id="filterempty"></div>'
    banner = f'<div class="healthbar">⚠️ {escape(warning)}</div>' if warning else ""
    # Gmail-style floating Compose (mobile only — CSS hides it on desktop, where the header
    # "Написать" button stays).
    fab = ('<button class="fab-compose" onclick="openCompose()" aria-label="Написать">'
           '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
           'stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/>'
           '<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>'
           '<span>Написать</span></button>')
    sel_bar = (
        '<div class="sel-bar" id="selBar" hidden>'
        '<button type="button" class="iconbtn" onclick="clearSel()" aria-label="Снять выделение"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>'
        '<span class="sel-count" id="selCount">0</span>'
        '<button type="button" class="sel-link" onclick="selectAll()">Выбрать все</button>'
        '<div class="spacer"></div>'
        '<button type="button" class="iconbtn" onclick="markSelRead()" title="Отметить прочитанным" aria-label="Отметить прочитанным"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M22 13V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h9"/><polyline points="22 7 12 13 2 7"/><polyline points="16 17 18 19 22 15"/></svg></button>'
        '</div>')
    body = (banner + head + fbar + funnel + sel_bar +
            f'<div class="maillist" id="maillist">{render_rows(rows)}</div>{empty}'
            f'<div id="loadmore" data-more="{has_more}" style="height:1px"></div>' + fab)
    return _page("inbox", body, modal)


def render_keyword_settings(rules: dict[str, list[str]], saved: bool = False,
                            updated: int = 0, error: str = "") -> str:
    labels = {
        "interview": ("Собеседование", "Только явные приглашения или назначение времени."),
        "offer": ("Оффер", "Фразы из письма с предложением работы."),
        "rejection": ("Отказ", "Фразы о прекращении процесса или выборе другого кандидата."),
        "ack": ("Заявка принята", "Подтверждения получения или рассмотрения заявки."),
    }
    cards = []
    for kind in ("interview", "offer", "rejection", "ack"):
        title, help_text = labels[kind]
        value = "\n".join(rules.get(kind, []))
        cards.append(
            f'<section class="keyword-card"><h2>{title}</h2><p>{help_text}</p>'
            f'<textarea name="{kind}" aria-label="Ключевые слова: {title}" '
            f'placeholder="Одна фраза на строку">{escape(value)}</textarea></section>')
    note = ""
    if saved:
        note = f'<div class="keyword-note">Сохранено. Пересчитано писем: <b>{updated}</b>.</div>'
    elif error:
        note = f'<div class="healthbar">{escape(error)}</div>'
    body = (
        '<div class="page-head"><div class="ph-left"><div class="seg-nav">'
        '<a href="/mail">Инбокс</a><a class="active" href="/mail/keywords">Ключевые слова</a>'
        '</div></div></div>' + note +
        '<p class="keyword-intro">Одна фраза на строку. Регистр не важен: если фраза есть '
        'в теме или тексте письма, ему сразу назначается категория. Приоритет: оффер → отказ → '
        'собеседование → заявка принята.</p>'
        '<form method="post" action="/mail/keywords"><div class="keyword-grid">'
        + "".join(cards) + '</div><div class="keyword-actions">'
        '<button class="primary" type="submit">Сохранить и пересчитать письма</button></div></form>'
        '<form method="post" action="/mail/keywords/reset" class="keyword-actions">'
        '<button class="ghost" type="submit">Вернуть стандартные слова</button>'
        '<a class="ghost" href="/mail">Назад в инбокс</a></form>')
    return _page("inbox", body)


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n/1024/1024:.1f} МБ"
    if n >= 1024:
        return f"{n/1024:.0f} КБ"
    return f"{n} Б"


def _att_chips(m: dict) -> str:
    atts = m.get("attachments") or []
    if not atts:
        return ""
    chips = []
    for a in atts:
        href = f'/mail/attachment?id={escape(m["id"])}&i={a["i"]}'
        chips.append(
            f'<a class="att" href="{href}" download>'
            f'<span class="att-ic">📎</span><span class="att-nm">{escape(a["filename"])}</span>'
            f'<span class="att-sz">{_fmt_size(a["size"])}</span></a>')
    return f'<div class="atts">{"".join(chips)}</div>'


def _msg_card(m: dict, thread_subject: str = "") -> str:
    sender = m.get("from_name") or m.get("from_email") or "?"
    side = " out" if m.get("outbound") else ""
    who = "Вы (кандидат)" if m.get("outbound") else escape(sender)
    # Prefer the PLAIN-TEXT part rendered in a normal auto-sizing <div>. The HTML iframe is only
    # a fallback for HTML-only mail: on iOS Safari an auto-height iframe truncates/flickers no
    # matter how it's measured, and almost every recruiter/ATS email is multipart with plain text.
    plain = _clean_plain(m.get("plain") or "")
    if plain:
        content = (f'<div class="msg-content">{_linkify(escape(plain))}</div>'
                   + _extra_links_block(m, plain))
    elif m.get("html"):
        # allow-popups(+escape) so a link inside the sandboxed body actually opens (a bare
        # allow-same-origin iframe swallows every click); <base target="_blank"> makes them
        # open in a new tab instead of trying to navigate the dashboard itself. Still no
        # allow-scripts — the untrusted mail HTML never runs JS.
        html_src = '<base target="_blank">' + (m.get("html") or "")
        content = (f'<div class="mail-frame-wrap"><iframe class="mail-frame" '
                   f'sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox" '
                   f'srcdoc="{escape(html_src)}"></iframe></div>'
                   + _extra_links_block(m, ""))
    else:
        content = '<div class="msg-content">(пустое письмо)</div>'
    # reply icon right in the sender row (Gmail-style) — replies to THIS message's other party
    rt_to = m.get("to", "") if m.get("outbound") else m.get("from_email", "")
    reply_attrs = (
        f'data-from="{escape(m.get("mailbox",""), quote=True)}" '
        f'data-to="{escape(rt_to, quote=True)}" '
        f'data-subject="{escape(thread_subject, quote=True)}" '
        f'data-mid="{escape(m.get("message_id",""), quote=True)}"')
    reply_ic = (f'<button type="button" class="mf-reply reply-action" {reply_attrs} title="Ответить" '
                'aria-label="Ответить"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
                '<polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/></svg></button>')
    return (
        f'<div class="tcard{side}">'
        f'<div class="msg-from"><span class="avatar" style="background:{_avatar_color(sender)}">{escape(_initial(sender))}</span>'
        f'<div class="mf-meta"><div class="mf-line"><b>{who}</b><span class="mf-addr">{escape(m.get("from_email",""))}</span>{_kind_tag(m.get("kind","other"))}</div>'
        f'<div class="mf-to">кому: {escape(_clean_addr(m.get("to","") or m.get("mailbox","")))}<span class="mf-dot">·</span>{escape(_fulldate(m.get("date_ts", 0)) or m.get("date",""))}</div></div>'
        f'{reply_ic}</div>'
        f'{content}{_att_chips(m)}</div>')


def render_thread(t: dict) -> str:
    msgs = t.get("messages") or []
    # reply prefills from the candidate mailbox to the last INBOUND sender
    inbound = [m for m in msgs if not m.get("outbound")]
    tgt = inbound[-1] if inbound else (msgs[-1] if msgs else {})
    # Values stay in data attributes instead of executable onclick text. Subjects
    # and Message-IDs commonly contain quotes; entity decoding made the old inline
    # JavaScript syntactically invalid, so the Reply button appeared to do nothing.
    reply_attrs = (
        f'data-from="{escape(t.get("mailbox", ""), quote=True)}" '
        f'data-to="{escape(tgt.get("from_email", ""), quote=True)}" '
        f'data-subject="{escape(t.get("subject", ""), quote=True)}" '
        f'data-mid="{escape(tgt.get("message_id", ""), quote=True)}"')
    thread_id = next((m.get("id") for m in reversed(msgs) if m.get("id")), "")
    thread_key = next((m.get("thread") for m in reversed(msgs) if m.get("thread")), "")
    subj = t.get("subject", "")
    iv_sobes = _iv_sobes(t.get("mailbox", ""), thread_key, thread_id)
    cards = "".join(_msg_card(m, subj) for m in msgs) or '<div class="empty">Пусто</div>'
    last = msgs[-1] if msgs else {}
    fwd_body = (last.get("plain", "") or "")[:4000]
    fwd_attrs = (
        f'data-from="{escape(t.get("mailbox", ""), quote=True)}" '
        f'data-subject="{escape(subj, quote=True)}" '
        f'data-body="{escape(fwd_body, quote=True)}"')
    body = (
        # BLOCK 1 — sticky top toolbar (back + delete): no scrolling up to leave the message
        '<div class="msg-toolbar">'
        '<a class="iconbtn" href="/mail" onclick="if(document.referrer&&history.length>1){history.back();return false;}" title="Назад" aria-label="Назад к списку"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg></a>'
        '<div class="spacer"></div>'
        f'<button type="button" class="iconbtn danger delete-action" data-id="{escape(thread_id, quote=True)}" data-mailbox="{escape(t.get("mailbox", ""), quote=True)}" title="Удалить" aria-label="Удалить"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg></button>'
        '</div>'
        # BLOCK 2 — subject · BLOCK 3 — sender rows + body (per card) · BLOCK 4 — reply/forward footer
        f'<div class="msg-page"><h1 class="msg-subject">{escape(subj or "(без темы)")}'
        f'<span class="tcount">{len(msgs)}</span></h1>'
        f'<div class="tsub">Ящик: {escape(t.get("candidate",""))} &lt;{escape(t.get("mailbox",""))}&gt;</div>'
        f'{cards}'
        f'<div class="reply-bar"><button type="button" class="reply-btn reply-action primary-btn" {reply_attrs}>'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/></svg>Ответить</button>'
        f'<button type="button" class="reply-btn fwd-action" {fwd_attrs} title="Переслать">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 17 20 12 15 7"/><path d="M4 18v-2a4 4 0 0 1 4-4h12"/></svg>Переслать</button>'
        f'{iv_sobes}</div>'
        '</div>')
    return _page("inbox", body, _COMPOSE_MODAL + _iv_modal(), topbar=False)


def render_candidate_rows(cands: list[dict]) -> str:
    """One <a.mbxrow> per candidate — shared by the full page and the
    /mail/candidates/more infinite-scroll fragment."""
    from backend.tools import candidate_apps
    resume_ids = candidate_apps.resume_profile_ids()   # cheap (cached): roster base résumés
    out = []
    for c in cands:
        n = c.get("unread", 0)
        badge = f'<span class="cnt">{n}</span>' if n else ""
        # résumé/applications chip → the candidate's applications page (résumé downloads +
        # where they applied). It lives inside the row <a>, so it stops the click from also
        # opening the inbox. Shows «📄 N» when the bot has applied N times, or a bare «📄»
        # for a roster candidate that has a base résumé but no application yet — so EVERY
        # candidate with a résumé shows one. Guard a missing id (minimal candidate dict).
        cid = c.get("id")
        na = candidate_apps.app_count(cid) if cid else 0
        show_chip = bool(cid) and (na or cid in resume_ids)
        chip_label = f"📄 {na}" if na else "📄"
        apps = (f'<span class="apps-chip" title="Резюме + куда подавались" '
                f'onclick="event.preventDefault();event.stopPropagation();'
                f"location.href='/candidates/{escape(cid)}'\">{chip_label}</span>") if show_chip else ""
        out.append(
            f'<a class="mbxrow" href="/mail?mailbox={escape(c["email"])}">'
            f'<span class="avatar" style="background:{_avatar_color(c["name"])};width:30px;height:30px;font-size:13px">{escape(_initial(c["name"]))}</span>'
            f'<span class="nm">{escape(c["name"])}</span>{badge}{apps}'
            f'<span class="em">{escape(c["email"])}</span></a>')
    return "".join(out)


def render_candidates(cands: list[dict], counts: dict | None = None,
                      active_filter: str = "", total: int | None = None,
                      has_more: int = 0, query: str = "",
                      keyword_rules: dict | None = None) -> str:
    counts = counts or {}
    total = total if total is not None else len(cands)

    _cstages = [("", "Все", total),
                ("submitted", "📤 Отправлено", counts.get("submitted", 0)),
                ("ack", "✅ Принято", counts.get("ack", 0)),
                ("interview", "📞 Собеседование", counts.get("interview", 0)),
                ("offer", "🎉 Оффер", counts.get("offer", 0)),
                ("rejection", "✕ Отказ", counts.get("rejection", 0))]
    def _chref(key: str) -> str:
        params = {}
        if key:
            params["filter"] = key
        if query:
            params["q"] = query
        return "/mail/candidates" + ("?" + urlencode(params) if params else "")
    # Hide empty stage filters (they reappear once populated). Always keep "Все"
    # (key == "") and whichever filter is currently active.
    def _show(key: str, n: int) -> bool:
        return not (n == 0 and key and active_filter != key)
    shown = [(k, l, n) for k, l, n in _cstages if _show(k, n)]
    stage_rows = [{"label": l, "href": _chref(k), "count": n, "active": active_filter == k}
                  for k, l, n in shown]
    chips = "".join(
        f'<a class="fbtn{" active" if r["active"] else ""}" href="{escape(r["href"], quote=True)}">'
        f'{r["label"]} <b>{r["count"]}</b></a>' for r in stage_rows)
    funnel = ('<div class="funnel" data-filter-list="mbxlist">' + chips + '</div>'
              + '<div class="filter-status" role="status" aria-live="polite"></div>')
    modal = _filter_modal(stage_rows, keyword_rules, _chref(active_filter))
    head = ('<div class="page-head"><div class="ph-left"><div class="seg-nav">'
            '<a href="/mail">Инбокс</a>'
            f'<a class="active" href="/mail/candidates">Кандидаты <b>{total}</b></a>'
            '</div></div><div class="head-actions">'
            + _filter_trigger(stage_rows) + '</div></div>')
    search = ('<form class="candidate-tools" method="get" action="/mail/candidates" role="search">'
              f'<input type="search" name="q" value="{escape(query, quote=True)}" '
              'placeholder="Поиск по имени или email" autocomplete="off">'
              + (f'<input type="hidden" name="filter" value="{escape(active_filter, quote=True)}">'
                 if active_filter else '')
              + '<button class="primary" type="submit">Найти</button>'
              + ('<a class="ghost" href="/mail/candidates">Сбросить</a>' if query else '')
              + '</form>')
    empty = '<div class="empty" id="filterempty">Никого в этой корзине</div>' if not cands else '<div id="filterempty"></div>'
    body = (head + search + funnel
            + f'<div class="mbxlist" id="mbxlist">{render_candidate_rows(cands)}</div>{empty}'
            + f'<div id="mbxmore" data-more="{has_more}" style="height:1px"></div>')
    return _page("candidates", body, modal)


def render_candidate_apps(cand: dict, apps: list[dict],
                          has_base_resume: bool = False) -> str:
    """A candidate's applications: where the bot applied + the résumé PDF it used
    (downloadable). `apps` from candidate_apps.applications_for(). `has_base_resume` adds a
    download of the candidate's BASE résumé (rendered from their profile) so a candidate with
    no application yet still has a downloadable CV."""
    from datetime import datetime
    name = escape(cand.get("name") or cand.get("id") or "")
    email = escape(cand.get("email") or "")
    cid = escape(cand.get("id") or "")
    inbox = (f'<a class="hbtn" href="/mail?mailbox={email}">Ящик кандидата</a>'
             if email else "")
    base_btn = (f'<a class="hbtn" href="/candidates/{cid}/resume.pdf" target="_blank" '
                f'rel="noopener">📄 Резюме</a>' if has_base_resume else "")
    head = (
        '<div class="page-head"><div class="ph-left"><div class="seg-nav">'
        '<a href="/mail/candidates">← Кандидаты</a></div></div></div>'
        f'<div class="capp-head"><div><div class="capp-name">{name}</div>'
        f'<div class="capp-em">{email}</div></div><div class="capp-hbtns">{base_btn}{inbox}</div></div>')
    if not apps:
        empty = ('<div class="empty">Заявок пока нет — резюме доступно по кнопке выше.</div>'
                 if has_base_resume else '<div class="empty">Заявок пока нет</div>')
        return _page("candidates", head + empty)

    cards = []
    for a in apps:
        try:
            d = datetime.fromtimestamp(a["ts"]).strftime("%d.%m.%Y")
        except Exception:
            d = ""
        tag = ('<span class="capp-tag capp-sub">Отправлено</span>' if a["submitted"]
               else '<span class="capp-tag capp-nosub">Заполнено</span>')
        jid = escape(str(a["jobid"]))
        dl = (f'<a class="capp-btn dl" href="/resume/{jid}?profile={cid}" '
              f'download="{cid}_{jid}.pdf">Скачать резюме</a>' if a["has_resume"]
              else '<span class="capp-btn dl off">Резюме нет</span>')
        ext = (f'<a class="capp-btn ext" href="{escape(a["apply_url"])}" target="_blank" '
               f'rel="noopener">Вакансия ↗</a>' if a["apply_url"] else "")
        cards.append(
            '<div class="capp-card">'
            f'<div class="capp-co">{escape(a["company"]) or "—"}</div>'
            f'<div class="capp-ttl">{escape(a["title"]) or "(без названия)"}</div>'
            f'<div class="capp-meta"><span>{d}</span>{tag}</div>'
            f'<div class="capp-actions">{dl}{ext}</div></div>')
    body = head + f'<div class="capp-list">{"".join(cards)}</div>'
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
function openFilter(expandKw){var m=document.getElementById('filterModal');if(!m)return;var d=m.querySelector('.fm-kw');if(d)d.open=!!expandKw;m.classList.add('open');document.body.style.overflow='hidden';}
function closeFilter(){var m=document.getElementById('filterModal');if(m)m.classList.remove('open');document.body.style.overflow='';}
// Gmail-style row selection: the avatar toggles selection; a bar offers select-all + mark-read.
function _updateSel(){var bar=document.getElementById('selBar');if(!bar)return;var sel=document.querySelectorAll('.maillist .mitem.selected');if(sel.length){bar.hidden=false;var c=document.getElementById('selCount');if(c)c.textContent=sel.length;}else{bar.hidden=true;}}
function toggleSel(btn){var it=btn.closest('.mitem');if(it){it.classList.toggle('selected');_updateSel();}}
function selectAll(){document.querySelectorAll('.maillist .mitem').forEach(function(it){it.classList.add('selected');});_updateSel();}
function clearSel(){document.querySelectorAll('.maillist .mitem.selected').forEach(function(it){it.classList.remove('selected');});_updateSel();}
async function markSelRead(){var sel=document.querySelectorAll('.maillist .mitem.selected');if(!sel.length)return;var ids=[];sel.forEach(function(it){if(it.dataset.id)ids.push(it.dataset.id);});try{var r=await fetch('/mail/mark_read',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({ids:ids.join(',')})});if(r.ok){sel.forEach(function(it){it.classList.remove('unread','selected');});_updateSel();}}catch(e){}}
function reply(from,to,subj,mid){var f=document.getElementById('composeForm');if(!f)return;var field=function(n){return f.elements.namedItem(n);};field('from_email').value=from||'';field('to').value=to||'';field('subject').value=(/^re:/i.test(subj||'')?subj:'Re: '+(subj||''));field('in_reply_to').value=mid||'';openCompose();setTimeout(function(){field('body').focus();},50);}
document.querySelectorAll('.reply-action').forEach(function(b){b.addEventListener('click',function(){reply(b.dataset.from,b.dataset.to,b.dataset.subject,b.dataset.mid);});});
function forward(from,subj,bodyText){var f=document.getElementById('composeForm');if(!f)return;var field=function(n){return f.elements.namedItem(n);};field('from_email').value=from||'';field('to').value='';field('subject').value=(/^fwd:/i.test(subj||'')?subj:'Fwd: '+(subj||''));field('in_reply_to').value='';field('body').value='\\n\\n---------- Пересылаемое сообщение ----------\\n'+(bodyText||'');openCompose();setTimeout(function(){field('to').focus();},50);}
document.querySelectorAll('.fwd-action').forEach(function(b){b.addEventListener('click',function(){forward(b.dataset.from,b.dataset.subject,b.dataset.body);});});
async function deleteThread(b){
  var id=b.dataset.id;if(!id)return;
  if(!confirm('Переместить всю цепочку в корзину? При необходимости её можно восстановить на сервере.'))return;
  b.disabled=true;var old=b.textContent;b.textContent='Удаление…';
  try{
    var r=await fetch('/mail/delete',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({id:id})});
    var j=await r.json();
    if(j.ok){var u='/mail';if(b.dataset.mailbox)u+='?mailbox='+encodeURIComponent(b.dataset.mailbox);location.href=u;return;}
    alert('Не удалось удалить: '+(j.error||'ошибка сервера'));
  }catch(e){alert('Не удалось удалить: ошибка сети');}
  b.disabled=false;b.textContent=old;
}
document.querySelectorAll('.delete-action').forEach(function(b){b.addEventListener('click',function(){deleteThread(b);});});
document.querySelectorAll('.modal').forEach(function(m){m.addEventListener('click',function(e){if(e.target===m){m.classList.remove('open');document.body.style.overflow='';}});});
document.addEventListener('keydown',function(e){if(e.key==='Escape'){document.querySelectorAll('.modal.open').forEach(function(m){m.classList.remove('open');});document.body.style.overflow='';}});
// toast after saving keyword rules from the filter modal (?kwsaved=N / ?kwerror=...)
(function(){try{var sp=new URLSearchParams(location.search);var k=sp.get('kwsaved'),e=sp.get('kwerror');if(k===null&&!e)return;var t=document.createElement('div');t.className='kw-toast';t.textContent=e?e:('Сохранено. Пересчитано писем: '+k);document.body.appendChild(t);requestAnimationFrame(function(){t.classList.add('show');});setTimeout(function(){t.classList.remove('show');setTimeout(function(){t.remove();},300);},3200);sp.delete('kwsaved');sp.delete('kwerror');history.replaceState({},'',location.pathname+(sp.toString()?'?'+sp.toString():''));}catch(_){}})();
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
function fitFrame(f){try{var doc=f.contentDocument,wrap=f.parentElement;var st=doc.createElement('style');st.textContent='html{-webkit-text-size-adjust:100%;}html,body{margin:0;padding:0;background:transparent;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;color:#202124;font-size:14.5px;line-height:1.6;}body{width:auto!important;}img{max-width:100%!important;height:auto;}a{color:#1a73e8;word-break:break-word;}table{max-width:100%!important;}td,th,div,p,pre,blockquote{max-width:100%!important;}pre{white-space:pre-wrap;word-break:break-word;}';doc.head.appendChild(st);try{doc.querySelectorAll('img').forEach(function(im){if(im.complete&&im.naturalWidth===0){im.style.display='none';}im.addEventListener('error',function(){this.style.display='none';});});}catch(_){}
  // Re-measure the height whenever the content reflows (fonts, injected styles, images),
  // else a tall HTML email gets truncated to whatever height was ready at onload.
  function docH(){return Math.max(doc.body.scrollHeight,doc.documentElement.scrollHeight,doc.body.offsetHeight);}
  var applied=-1;
  // Guarded: only touch the DOM when the wrapper height actually changes by >3px. Redundant
  // re-measures that re-set the same height are what made the frame flicker.
  function measure(){var natW=Math.max(doc.body.scrollWidth,doc.documentElement.scrollWidth),avail=wrap.clientWidth,h,wh,sc=null;if(natW>avail+2){sc=Math.max(avail/natW,0.72);h=docH();wh=Math.round(h*sc+8);}else{h=docH()+24;wh=h;}f.classList.add('ready');if(Math.abs(wh-applied)<=3)return;applied=wh;if(sc!==null){f.style.width=natW+'px';f.style.transformOrigin='top left';f.style.transform='scale('+sc+')';}else{f.style.width='100%';f.style.transform='none';}f.style.height=h+'px';wrap.style.height=wh+'px';}
  // Measure on the next frame (after the injected style lays out) + a couple of settle passes.
  // NO synchronous first call (it measures before layout → a tiny value → a visible jump). The
  // frame stays opacity:0 until the first measure sets its real height (adds .ready) so the
  // 150px→full jump is never shown.
  requestAnimationFrame(measure);setTimeout(measure,300);
  try{doc.querySelectorAll('img').forEach(function(im){im.addEventListener('load',measure);});}catch(_){}
}catch(e){f.style.height='600px';f.classList.add('ready');}}
// Wire the HTML-email iframes here (NOT via inline onload="fitFrame" — that fires while the
// body is still parsing, before fitFrame is defined → "fitFrame is not defined", so the frame
// kept its default 150px and truncated tall emails). This runs after the DOM is parsed.
document.querySelectorAll('iframe.mail-frame').forEach(function(f){var go=function(){fitFrame(f);};f.addEventListener('load',go);try{if(f.contentDocument&&f.contentDocument.body){go();}else{setTimeout(go,60);}}catch(_){setTimeout(go,60);}setTimeout(function(){f.classList.add('ready');},1600);});
// Funnel filters update in place. A loading state appears on the first tap and
// blocks duplicate taps while the server response is in flight.
(function(){
  var filtering=false;
  document.addEventListener('click',async function(e){
    var a=e.target.closest('.funnel[data-filter-list] .fbtn');
    if(!a||e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;
    e.preventDefault();if(filtering)return;filtering=true;
    var funnel=a.closest('.funnel'),target=funnel.dataset.filterList,
        list=document.getElementById(target),status=funnel.nextElementSibling;
    funnel.classList.add('busy');a.classList.add('pending');
    if(status&&status.classList.contains('filter-status'))status.textContent='Фильтруем…';
    try{
      var r=await fetch(a.href,{headers:{'X-Filter':'1'}});if(!r.ok)throw new Error('http');
      var doc=new DOMParser().parseFromString(await r.text(),'text/html'),
          nf=doc.querySelector('.funnel[data-filter-list="'+target+'"]'),nl=doc.getElementById(target),
          nm=doc.getElementById(target==='maillist'?'loadmore':'mbxmore'),
          more=document.getElementById(target==='maillist'?'loadmore':'mbxmore'),
          ne=doc.getElementById('filterempty'),empty=document.getElementById('filterempty');
      if(!nf||!nl||!list)throw new Error('html');
      funnel.innerHTML=nf.innerHTML;list.innerHTML=nl.innerHTML;
      if(more&&nm){more.dataset.more=nm.dataset.more;more.dataset.offset=nm.dataset.offset||'';}
      if(empty&&ne)empty.innerHTML=ne.innerHTML;
      history.pushState({},'',a.href);
      var active=funnel.querySelector('.fbtn.active');
      if(active)active.scrollIntoView({behavior:'smooth',block:'nearest',inline:'center'});
      if(status&&status.classList.contains('filter-status')){status.textContent='Готово';setTimeout(function(){status.textContent='';},1200);}
    }catch(err){location.href=a.href;return;}
    funnel.classList.remove('busy');filtering=false;
  });
  window.addEventListener('popstate',function(){location.reload();});
})();
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
  var head=document.querySelector('.page-head'),pill=document.querySelector('.gm-topbar'),fab=document.querySelector('.fab-compose'),lastY=window.scrollY;
  window.addEventListener('scroll',function(){var y=window.scrollY;if(window.innerHeight+y>=document.documentElement.scrollHeight-400)loadMore();
    // Only react to a net move of >6px — momentum scroll jitters direction by 1px, which was
    // rapidly toggling the pill hide/show → flicker. Below the threshold, keep lastY as anchor.
    var dy=y-lastY;if(Math.abs(dy)<=6)return;lastY=y;
    if(dy>0&&y>90){head.classList.add('hide');if(pill)pill.classList.add('hide');if(fab)fab.classList.add('collapsed');}
    else if(dy<0){head.classList.remove('hide');if(pill)pill.classList.remove('hide');if(fab)fab.classList.remove('collapsed');}
    },{passive:true});
  async function refreshList(){if(document.querySelector('.modal.open'))return;try{var r=await fetch(location.href,{headers:{'X-Poll':'1'}});if(!r.ok)return;var doc=new DOMParser().parseFromString(await r.text(),'text/html');var fl=doc.getElementById('maillist');if(fl)list.innerHTML=fl.innerHTML;var sn=doc.querySelector('.seg-nav');if(sn)document.querySelector('.seg-nav').innerHTML=sn.innerHTML;var nf=doc.querySelector('.funnel[data-filter-list="maillist"]'),cf=document.querySelector('.funnel[data-filter-list="maillist"]');if(nf&&cf&&!cf.classList.contains('busy'))cf.innerHTML=nf.innerHTML;}catch(e){}}
  var last=null,pollTimer=null;
  function startPoll(){if(pollTimer)return;pollTimer=setInterval(async function(){if(document.hidden||document.querySelector('.modal.open'))return;try{var r=await fetch('/mail/count'+location.search);if(!r.ok)return;var j=await r.json();if(j.n!==last){if(last!==null)await refreshList();last=j.n;}}catch(e){}},10000);}
  if(window.EventSource){try{var es=new EventSource('/mail/events');es.onmessage=function(){refreshList();};es.onerror=function(){startPoll();};}catch(e){startPoll();}}else{startPoll();}
})();
// Candidates list — offset-based infinite scroll (mirrors the inbox #maillist one)
(function(){
  var list=document.getElementById('mbxlist'), more=document.getElementById('mbxmore');
  if(!list||!more)return;
  var loading=false,PAGE=50;
  async function loadMore(){
    if(loading||more.dataset.more!=='1')return;
    loading=true;
    try{
      var sp=new URLSearchParams(location.search);
      sp.set('offset', list.querySelectorAll('.mbxrow').length);
      var r=await fetch('/mail/candidates/more?'+sp.toString());
      if(r.ok){var html=await r.text();var added=(html.match(/class=.mbxrow/g)||[]).length;if(added)list.insertAdjacentHTML('beforeend',html);if(added<PAGE)more.dataset.more='0';}
    }catch(e){}finally{loading=false;}
  }
  window.addEventListener('scroll',function(){if(window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-400)loadMore();},{passive:true});
})();
// Gmail-style mobile drawer + search-pill wiring
function gmDrawer(open){var d=document.querySelector('.gm-drawer'),s=document.querySelector('.gm-scrim');if(!d||!s)return;if(open){d.classList.add('open');s.classList.add('open');document.body.style.overflow='hidden';}else{d.classList.remove('open');s.classList.remove('open');document.body.style.overflow='';}}
document.addEventListener('keydown',function(e){if(e.key==='Escape')gmDrawer(false);});
(function(){try{var q=new URLSearchParams(location.search).get('q');if(q){var i=document.querySelector('.gm-search input[name=q]');if(i)i.value=q;}}catch(e){}})();
</script>
"""
