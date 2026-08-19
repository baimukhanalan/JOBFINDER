"""Server-rendered inbox for the candidate mailboxes: classified, durable, live.

Opens an EventSource on /mail/events and re-renders the instant a new message is
classified (5s poll fallback). Data comes from the durable store, so it reflects
every message ever captured — you can look at different times, nothing is lost.

Client-side filter by candidate profile + an address book of every mailbox, so you
can review who has which address and jump to one candidate's replies. The subtitle
names the live provider (real Mailgun inbox vs. local throwaway sink) — that
distinction decides whether an empty inbox means "nothing yet" or "nothing ever".
"""
from __future__ import annotations

import json
from typing import Any


def render_html(summary: dict[str, Any], addr_map: dict[str, str]) -> str:
    boot = json.dumps(summary, ensure_ascii=False)
    return _PAGE.replace("/*BOOT*/null", boot)


_PAGE = r"""<title>JobFinder — инбокс (sink)</title>
<style>
  :root{color-scheme:light dark;
    --bg:#0f1512;--card:#161d1a;--ink:#e7ede9;--ink2:#9fb0a8;--ink3:#6f7f77;
    --rule:#232d29;--accent:#4fbfa8;--chip:#1c2622;}
  @media(prefers-color-scheme:light){:root{--bg:#f3f6f4;--card:#fff;--ink:#12201b;
    --ink2:#4a5a52;--ink3:#7b8b83;--rule:#dde5e1;--accent:#0e6b5c;--chip:#e7efeb;}}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}
  .wrap{max-width:1040px;margin:0 auto;padding:26px 16px 70px}
  h1{font:600 24px/1.1 ui-monospace,Menlo,monospace;margin:0 0 4px}
  .sub{color:var(--ink3);font-size:13px;margin:0 0 16px}
  .sub code{background:var(--chip);padding:.1em .4em;border-radius:4px;font-size:.9em}
  .bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 14px}
  .chip{background:var(--card);border:1px solid var(--rule);border-radius:20px;
    padding:6px 13px;font-size:13px;color:var(--ink2)}
  .chip b{color:var(--ink);font:600 14px ui-monospace,Menlo,monospace}
  button.chip.kf{cursor:pointer;font:inherit;font-size:13px;color:var(--ink2)}
  button.chip.kf:hover{border-color:var(--accent)}
  button.chip.kf.on{border-color:var(--accent);color:var(--ink);
    background:color-mix(in oklab,var(--accent) 18%,transparent)}
  button.chip.kf.on b{color:var(--accent)}
  .live{margin-left:auto;font-size:12px;color:var(--ink3);display:flex;gap:6px;align-items:center}
  .dot{width:8px;height:8px;border-radius:50%;background:#d67a6a;transition:background .3s}
  .dot.on{background:var(--accent);box-shadow:0 0 8px var(--accent)}
  .layout{display:grid;grid-template-columns:280px 1fr;gap:16px}
  @media(max-width:760px){.layout{grid-template-columns:1fr}}
  .side{background:var(--card);border:1px solid var(--rule);border-radius:12px;
    padding:10px;max-height:70vh;overflow:auto}
  .side h2{font:600 11px ui-monospace,Menlo,monospace;letter-spacing:.12em;text-transform:uppercase;
    color:var(--ink3);margin:6px 8px 8px}
  .pf{display:flex;justify-content:space-between;gap:8px;align-items:center;
    padding:7px 9px;border-radius:8px;cursor:pointer;font-size:12.5px}
  .pf:hover{background:var(--chip)} .pf.sel{background:color-mix(in oklab,var(--accent) 18%,transparent)}
  .pf .nm{color:var(--ink);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .pf .ad{color:var(--ink3);font-size:11px;font-family:ui-monospace,Menlo,monospace}
  .pf .n{color:var(--accent);font:600 11px ui-monospace,Menlo,monospace;min-width:18px;text-align:right}
  .pf .ix{color:var(--ink3);font:600 11px ui-monospace,Menlo,monospace;margin-right:5px}
  table{width:100%;border-collapse:collapse;background:var(--card);
    border:1px solid var(--rule);border-radius:12px;overflow:hidden}
  td{padding:12px 14px;border-bottom:1px solid var(--rule);vertical-align:top}
  tr:last-child td{border-bottom:0} td:first-child{width:130px}
  .badge{display:inline-block;font:600 11px ui-monospace,Menlo,monospace;
    color:var(--c);border:1px solid color-mix(in oklab,var(--c) 45%,var(--rule));
    background:color-mix(in oklab,var(--c) 12%,transparent);
    padding:3px 9px;border-radius:20px;white-space:nowrap}
  .subj{font-weight:600} .meta{color:var(--ink3);font-size:12px;margin-top:2px}
  .prof{color:var(--accent);cursor:pointer} .snip{color:var(--ink2);font-size:12.5px;margin-top:5px}
  .empty{text-align:center;color:var(--ink3);padding:34px 14px}
  .empty code{background:var(--chip);padding:.1em .4em;border-radius:4px}
  .subj.clk{cursor:pointer;user-select:none}
  .subj .car{display:inline-block;color:var(--ink3);font-size:10px;margin-right:6px;
    transition:transform .15s}
  .subj.op .car{transform:rotate(90deg)}
  .body{margin-top:8px;max-height:70vh;overflow:auto}
  .msgcard{border:1px solid var(--rule);border-radius:10px;padding:11px 13px;margin:0 0 8px;
    background:var(--chip)}
  .msgcard.out{background:color-mix(in oklab,var(--accent) 12%,var(--card));
    border-color:color-mix(in oklab,var(--accent) 40%,var(--rule));margin-left:22px}
  .cfrom{font-size:12px;color:var(--ink3);font-weight:600;margin-bottom:5px}
  .msgcard.out .cfrom{color:var(--accent)}
  .mbody{white-space:pre-wrap;overflow-wrap:anywhere;color:var(--ink);font-size:13px;line-height:1.55}
  .reply{margin-top:8px}
  .replybtn,.sendbtn,.draftbtn{font:inherit;font-size:12.5px;cursor:pointer;color:var(--ink);
    background:var(--chip);border:1px solid var(--rule);border-radius:8px;padding:6px 11px}
  .replybtn:hover,.draftbtn:hover{border-color:var(--accent);color:var(--accent)}
  .draftbtn:disabled{opacity:.55;cursor:default}
  .sendbtn{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
  .sendbtn:hover{filter:brightness(1.06)} .sendbtn:disabled{opacity:.55;cursor:default;filter:none}
  .rl{margin-top:6px;display:flex;flex-direction:column;gap:7px}
  .rlhint{font-size:12px;color:var(--ink2);border:1px solid var(--rule);border-radius:8px;
    padding:7px 10px;background:color-mix(in oklab,var(--accent) 9%,transparent)}
  .rlhint b{color:var(--ink)}
  .rlf{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--ink3)}
  .rlf input{flex:1;font:inherit;font-size:12.5px;color:var(--ink);background:var(--bg);
    border:1px solid var(--rule);border-radius:7px;padding:6px 9px}
  .rlf input[readonly]{color:var(--ink2)}
  .ctext{font:inherit;font-size:13px;line-height:1.55;color:var(--ink);background:var(--bg);
    border:1px solid var(--rule);border-radius:8px;padding:10px 12px;resize:vertical;width:100%}
  .rlrow{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .sendmsg{font-size:12px;color:var(--ink3)} .okmsg{color:var(--accent)}
  .repd{font-size:11px;color:var(--accent);font-weight:600;margin-left:6px}
</style>
<div class="wrap">
  <h1>Инбокс кандидатов <span style="color:var(--ink3);font-weight:400;font-size:14px" id="pv">·</span></h1>
  <p class="sub" id="sub"></p>
  <div class="bar" id="chips"></div>
  <div class="layout">
    <div class="side">
      <h2>Кандидаты / адреса</h2>
      <div id="pflist"></div>
    </div>
    <table><tbody id="rows"></tbody></table>
  </div>
</div>
<script>
const BOOT=/*BOOT*/null;
const KIND={interview:["Собеседование","#3fb37f"],offer:["Оффер","#4fbfa8"],
  ack:["Принято","#5b9dd6"],rejection:["Отказ","#d67a6a"],other:["Прочее","#8a978f"]};
const ORDER=["interview","offer","ack","rejection","other"];
let DATA=BOOT, FILTER=new URLSearchParams(location.search).get("profile")||"";
// KINDFILTER: click a count chip (Собеседование/Оффер/Отказ/…) to drill down to just
// those messages AND collapse the left roster to only the accounts that have them —
// so you jump straight to "who has an interview" without scrolling every account.
let KINDFILTER=new URLSearchParams(location.search).get("kind")||"";
// Escapes &<> AND both quote chars — values (attacker-controlled Message-Id/Subject)
// are interpolated into double-quoted HTML attributes, so unescaped " would break out.
function esc(s){return (s||"").replace(/[&<>"']/g,c=>(
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

function renderHead(d){
  const mg=d.provider==="mailgun";
  document.getElementById("pv").textContent = mg ? "· Mailgun" : "· sink";
  document.getElementById("sub").innerHTML = mg
    ? `Настоящие входящие через Mailgun · адреса <code>@${esc(d.domain||"")}</code> · `+
      `catch-all route → store, опрос events API · хранилище персистентное `+
      `(Mailgun чистит store через ~3 дня, локальная копия остаётся).`
    : `Локальный Mailpit-приёмник · адреса <code>@${esc(d.domain||"mail.kz")}</code> · `+
      `реальная почта сюда НЕ приходит. Веб-инбокс Mailpit: <code>localhost:8025</code>`;
}
function renderChips(d){
  const c=d.counts||{};
  const el=document.getElementById("chips");
  el.innerHTML=
    ORDER.map(k=>`<button class="chip kf${KINDFILTER===k?' on':''}" data-k="${k}" `+
      `title="Показать аккаунты со статусом «${KIND[k][0]}»"><b>${c[k]||0}</b> ${KIND[k][0]}</button>`).join("")+
    `<span class="live"><span class="dot" id="dot"></span><span id="ago">подключение…</span></span>`;
  el.querySelectorAll(".kf").forEach(b=>b.onclick=()=>setKind(b.dataset.k));
}
// Toggle the kind filter (click the active chip again to clear it) and persist it in
// the URL, mirroring the profile filter.
function setKind(k){
  KINDFILTER=(KINDFILTER===k?"":k);
  const u=new URL(location);
  KINDFILTER?u.searchParams.set("kind",KINDFILTER):u.searchParams.delete("kind");
  history.replaceState(null,"",u);
  render(DATA);
}
function renderSide(d){
  const addr=d.addresses||{}, per=d.per_profile||{};
  // With a kind filter active, restrict the roster to accounts that actually have a
  // message of that kind and show the per-kind count — so the list shrinks to only the
  // relevant accounts. Otherwise show every account with its total message count.
  let count=per, ids=Object.keys(addr).sort();
  if(KINDFILTER){
    const k={};
    for(const m of (d.messages||[])) if(m.kind===KINDFILTER && m.profile) k[m.profile]=(k[m.profile]||0)+1;
    count=k; ids=ids.filter(id=>k[id]);
  }
  const shown=KINDFILTER?Object.values(count).reduce((a,b)=>a+b,0):(d.total||0);
  const allLbl=KINDFILTER?("Все · "+KIND[KINDFILTER][0]):"Все кандидаты";
  let html=`<div class="pf ${FILTER?'':'sel'}" data-p=""><span class="nm">${allLbl}</span>`+
           `<span class="n">${shown}</span></div>`;
  // Numbered candidate roster: sample is excluded from address_map, so #1 is the first
  // real candidate. ids are zero-padded, so the sort is the natural gen_xx_001.. order.
  html+=ids.map((id,i)=>{
    const nm=id.replace(/^gen_[a-z]{2}_\d+_/,"").replace(/_/g," ").replace(/\b\w/g,m=>m.toUpperCase());
    return `<div class="pf ${FILTER===id?'sel':''}" data-p="${id}" title="${esc(addr[id])}">`+
      `<div style="min-width:0"><div class="nm"><span class="ix">${i+1}.</span>${esc(nm)}</div><div class="ad">${esc(addr[id])}</div></div>`+
      `<span class="n">${count[id]||0}</span></div>`;
  }).join("")||`<div class="empty" style="padding:16px 8px">Нет аккаунтов с этим статусом.</div>`;
  const el=document.getElementById("pflist"); el.innerHTML=html;
  el.querySelectorAll(".pf").forEach(n=>n.onclick=()=>setFilter(n.dataset.p));
}
function emailOf(s){const x=/[\w.+-]+@[\w.-]+/.exec(s||"");return x?x[0].toLowerCase():"";}
function replyFrom(m){   // display-only mirror of mail_sink._reply_from; backend decides for real
  const dom="@"+(((DATA&&DATA.domain)||"")).toLowerCase();
  for(const a of (m.to||[])) if(a&&a.toLowerCase().endsWith(dom)) return a;
  const base=((DATA&&DATA.addresses)||{})[m.profile||""];
  return base||((m.to&&m.to[0])||"");
}
function rowHTML(m){
  const [label,clr]=KIND[m.kind]||KIND.other;
  const when=(m.received||"").slice(0,19).replace("T"," ");
  const rep=m.replied?` <span class="repd">✓ отвечено</span>`:"";
  const rcp=emailOf(m.from), our=replyFrom(m);
  const re=/^re:/i.test(m.subject||"")?(m.subject||""):("Re: "+(m.subject||""));
  return `<tr><td><span class="badge" style="--c:${clr}">${label}</span></td>`+
    `<td><div class="subj clk" data-id="${esc(m.id||"")}"><span class="car">▸</span>`+
      `${esc(m.subject||"(без темы)")}${rep}</div>`+
    `<div class="meta">${esc(m.from||"")} · <span class="prof" data-p="${esc(m.profile||"")}">${esc(m.profile||"—")}</span> · ${when}</div>`+
    `<div class="snip">${esc((m.snippet||"").slice(0,180))}</div>`+
    `<div class="body" hidden></div>`+
    `<div class="reply" hidden><button class="replybtn" data-id="${esc(m.id||"")}" `+
      `data-to="${esc(rcp)}" data-from="${esc(our)}" data-subj="${esc(re)}">`+
      `✉️ Ответить рекрутёру</button></div>`+
    `</td></tr>`;
}
const BODYCACHE={};   // id -> full message object {body, from, received, replies:[...]}
function fmtTs(ts){try{return new Date(ts*1000).toISOString().slice(0,16).replace("T"," ")+" UTC";}catch(_){return "";}}
// Gmail-like thread: the recruiter's message, then each reply we sent (out cards).
// All text goes through esc() (escapes quotes too) — safe even though we set innerHTML.
function threadHTML(m){
  if(!m||m.__err) return '<div class="msgcard in"><div class="mbody">(не удалось загрузить письмо)</div></div>';
  const body=(m.body||m.snippet||"").trim()
    ||"(текст письма недоступен — Mailgun не отдаёт тело на этом плане; см. входящий вебхук)";
  const when=m.received?" · "+esc((m.received||"").slice(0,16).replace("T"," ")):"";
  let h=`<div class="msgcard in"><div class="cfrom">${esc(m.from||"")}${when}</div>`+
        `<div class="mbody">${esc(body)}</div></div>`;
  for(const rp of (m.replies||[])){
    const w=rp.ts?" · "+esc(fmtTs(rp.ts)):"";
    h+=`<div class="msgcard out"><div class="cfrom">↩ Ваш ответ · ${esc(rp.from||"")} → ${esc(rp.to||"")}${w}</div>`+
       `<div class="mbody">${esc(rp.text||"")}</div></div>`;
  }
  return h;
}
async function toggleBody(subj){
  const cell=subj.parentElement, box=cell.querySelector(".body"),
        snip=cell.querySelector(".snip"), rep=cell.querySelector(".reply");
  if(!box.hasAttribute("hidden")){            // collapse
    box.setAttribute("hidden","");if(rep)rep.setAttribute("hidden","");
    if(snip)snip.style.display="";subj.classList.remove("op");return;
  }
  subj.classList.add("op");if(snip)snip.style.display="none";box.removeAttribute("hidden");
  if(rep)rep.removeAttribute("hidden");
  const id=subj.dataset.id;
  if(BODYCACHE[id]===undefined){
    box.innerHTML='<div class="msgcard in"><div class="mbody">Загрузка…</div></div>';
    try{const r=await fetch("/mail/message?id="+encodeURIComponent(id));
      BODYCACHE[id]=await r.json();
    }catch(_){BODYCACHE[id]={__err:1};}
  }
  box.innerHTML=threadHTML(BODYCACHE[id]);
}
// Reply SEND: the human writes the reply and clicks Отправить — /mail/send emails
// the recruiter (Mailgun). There is NO auto-reply; nothing is sent without this click.
function composeHTML(d){
  return `<div class="rl">`+
    `<div class="rlhint">Письмо уйдёт <b>рекрутёру</b> ${esc(d.to)} от адреса `+
      `${esc(d.from)}. Отправляете вы — кнопкой ниже. Автоматически ничего не уходит.</div>`+
    `<label class="rlf">От<input readonly value="${esc(d.from)}"></label>`+
    `<label class="rlf">Кому<input readonly value="${esc(d.to)}"></label>`+
    `<label class="rlf">Тема<input class="csubj" value="${esc(d.subject||"")}"></label>`+
    `<textarea class="ctext" rows="9" placeholder="Текст ответа…"></textarea>`+
    `<div class="rlrow"><button class="draftbtn" data-id="${esc(d.id||"")}">✍️ Черновик</button>`+
      `<button class="sendbtn">Отправить рекрутёру</button>`+
      `<span class="sendmsg"></span></div></div>`;
}
function openReply(btn){
  const wrap=btn.closest(".reply");
  const d={id:btn.dataset.id,to:btn.dataset.to,from:btn.dataset.from,subject:btn.dataset.subj};
  if(!d.to){wrap.innerHTML=`<div class="rlhint">У письма нет адреса рекрутёра — ответить нельзя.</div>`;return;}
  wrap.innerHTML=composeHTML(d);
  const send=wrap.querySelector(".sendbtn"), msg=wrap.querySelector(".sendmsg");
  const dbtn=wrap.querySelector(".draftbtn");
  if(dbtn) dbtn.onclick=async()=>{                 // suggest text — does NOT send
    const ta=wrap.querySelector(".ctext"); const old=dbtn.textContent;
    dbtn.disabled=true; dbtn.textContent="Готовлю…"; msg.textContent="";
    try{const r=await fetch("/mail/draft?id="+encodeURIComponent(d.id));
      const j=await r.json();
      if(j.ok){ta.value=j.draft||"";
        if(!j.has_body) msg.textContent="⚠ текст письма не захвачен — язык и даты не определены; черновик примерный, проверьте";}
      else msg.textContent="Не удалось составить черновик";
    }catch(_){msg.textContent="Сеть недоступна";}
    dbtn.disabled=false; dbtn.textContent=old;
  };
  send.onclick=async()=>{
    const text=wrap.querySelector(".ctext").value.trim();
    const subject=wrap.querySelector(".csubj").value.trim();
    if(!text){msg.textContent="Введите текст ответа.";return;}
    if(!confirm("Отправить ответ рекрутёру "+d.to+"?"))return;
    send.disabled=true;msg.textContent="Отправка…";
    try{
      const r=await fetch("/mail/send",{method:"POST",
        headers:{"Content-Type":"application/x-www-form-urlencoded"},
        body:new URLSearchParams({id:d.id,text:text,subject:subject})});
      const j=await r.json();
      if(j.ok){msg.innerHTML='<span class="okmsg">✓ отправлено рекрутёру</span>';
        send.textContent="Отправлено";
        // reflect the sent reply in the thread immediately (Gmail-like)
        const td=wrap.closest("td"), box=td?td.querySelector(".body"):null, cur=BODYCACHE[d.id];
        if(cur&&typeof cur==="object"&&!cur.__err){
          (cur.replies=cur.replies||[]).push(
            {text:text,from:d.from,to:d.to,subject:subject,ts:Math.floor(Date.now()/1000)});
          if(box)box.innerHTML=threadHTML(cur);
        }else{delete BODYCACHE[d.id];}
      }
      else{msg.textContent="Ошибка: "+(j.error||"не отправлено");send.disabled=false;}
    }catch(_){msg.textContent="Сеть недоступна";send.disabled=false;}
  };
}
function renderRows(d){
  const msgs=(d.messages||[]).filter(m=>(!FILTER||m.profile===FILTER)&&(!KINDFILTER||m.kind===KINDFILTER));
  const rows=document.getElementById("rows");
  const hint=(d.provider==="mailgun")
    ? 'Пусто. Проверь маршрут: <code>python -m backend.tools.mail_sink --status</code>, '+
      'затем пришли письмо на любой адрес из списка слева.'
    : 'Пусто. Отправь в Mailpit: <code>python -m backend.tools.mail_sink --seed</code>';
  const emptyMsg=KINDFILTER?`Нет писем со статусом «${KIND[KINDFILTER][0]}»${FILTER?" у этого кандидата":""}.`
                           :(FILTER?"У этого кандидата пока нет писем.":hint);
  rows.innerHTML=msgs.map(rowHTML).join("")||
    `<tr><td colspan="2" class="empty">${emptyMsg}</td></tr>`;
  rows.querySelectorAll(".prof").forEach(n=>{if(n.dataset.p)n.onclick=()=>setFilter(n.dataset.p);});
  rows.querySelectorAll(".subj.clk").forEach(s=>s.onclick=()=>toggleBody(s));
  rows.querySelectorAll(".replybtn").forEach(b=>b.onclick=()=>openReply(b));
}
function render(d){DATA=d;renderHead(d);renderChips(d);renderSide(d);renderRows(d);setLive();}
function setFilter(p){FILTER=p;const u=new URL(location);p?u.searchParams.set("profile",p):u.searchParams.delete("profile");
  history.replaceState(null,"",u);render(DATA);}
let LIVE=false;
function setLive(){const dot=document.getElementById("dot"),ago=document.getElementById("ago");
  if(dot){dot.classList.toggle("on",LIVE);ago.textContent=LIVE?"в реальном времени":"переподключение…";}}
function connect(){
  try{
    const es=new EventSource("/mail/events");
    es.onopen=()=>{LIVE=true;setLive();};
    es.onmessage=e=>{try{render(JSON.parse(e.data));}catch(_){}};
    es.onerror=()=>{LIVE=false;setLive();es.close();setTimeout(connect,3000);};
  }catch(_){poll();}
}
function poll(){fetch("/mail/data").then(r=>r.json()).then(render).catch(()=>{});setTimeout(poll,5000);}
render(DATA); connect();
</script>
"""
