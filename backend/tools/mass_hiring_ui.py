"""Server-rendered «Mass Hiring» tab — REMOTE-only, mass-hiring US jobs the human applies to
by hand. Reads backend.tools.mass_hiring (the SEPARATE mass_hiring_jobs table), renders companies
ranked by mass_hiring_score with an expandable job list, each job linking OUT to its own apply
page. No auto-apply anywhere — this is a discovery surface, decoupled from the /catalog engine."""
from __future__ import annotations

import html
import re
import time

from backend.tools import mailcrm_ui, mass_hiring

_SRC_LABEL = {"conduent": "Conduent", "alorica": "Alorica", "concentrix": "Concentrix",
              "amazon": "Amazon", "himalayas": "Himalayas", "remotive": "Remotive",
              "remoteok": "RemoteOK"}

# Neutral RU labels for the employer-segment heuristic (no stack disclosure).
_SEG_LABEL = {"staffing": "Кадровые / аутсорсинг", "government": "Госсектор",
              "education": "Образование", "healthcare": "Здравоохранение",
              "nonprofit": "НКО", "general": "Крупный работодатель"}


_JS = """
<script>
async function mhFill(id, btn){
  if(!id) return;
  btn.disabled = true; btn.textContent = 'Готовлю…';
  try{
    const r = await fetch('/mass-hiring/' + id + '/fill', {method:'POST'});
    const j = await r.json();
    if(j.novnc) window.open(j.novnc, '_blank', 'noopener');
    btn.textContent = 'Заполняется — смотри в окне';
    mhPoll(id, btn);
  }catch(e){ btn.textContent = 'Ошибка'; btn.disabled = false; }
}
async function mhPoll(id, btn){
  try{
    const r = await fetch('/mass-hiring/' + id + '/fill_status');
    const j = await r.json();
    if(j.state === 'done'){ btn.textContent = j.dry_run ? 'Заполнено (тест) ✓' : 'Подано ✓'; return; }
    if(j.state === 'error'){ btn.textContent = 'Ошибка: ' + (j.error || ''); btn.disabled = false; return; }
    setTimeout(function(){ mhPoll(id, btn); }, 3000);
  }catch(e){ setTimeout(function(){ mhPoll(id, btn); }, 5000); }
}
</script>
"""


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _ago(ts: int) -> str:
    if not ts:
        return "—"
    d = int(time.time()) - int(ts)
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < 86400:
        return f"{d // 3600} ч назад"
    return f"{d // 86400} дн назад"


def _tier(s: int) -> tuple[str, str]:
    """Demand tier: a WORD (leads the badge) + css class. The raw 0-100 index stays secondary,
    so the badge never reads as a naked count that could be confused with a vacancy total."""
    if s >= 85:
        return ("Высокий", "hi")
    if s >= 60:
        return ("Средний", "mid")
    return ("Точечный", "lo")


def _src_label(s: str) -> str:
    """Human source name — never a raw slug in the UI (title-cased fallback)."""
    return _SRC_LABEL.get(s) or str(s or "").replace("_", " ").replace("-", " ").strip().title()


_CSS = """
<style>
.mh-wrap{max-width:1040px;margin:0 auto;padding:18px 16px 60px;}
.mh-star{flex:0 0 auto;color:#f5a623;font-size:15px;line-height:1;margin-right:5px;
  text-shadow:0 1px 3px rgba(245,166,35,.45);}
.mh-pay{font-size:12.5px;font-weight:700;color:#0f7b3e;font-variant-numeric:tabular-nums;white-space:nowrap;}
.mh-pay.est{color:var(--ink-soft);font-weight:600;}
.mh-est-t{font-size:10px;font-weight:600;opacity:.65;}
.mh-fill{margin-left:8px;display:inline-flex;align-items:center;border:1px solid var(--accent);background:var(--accent);
  color:#fff;border-radius:var(--r-full);height:var(--chip-h);padding:0 var(--chip-px);font:inherit;font-size:var(--chip-fs);font-weight:700;
  cursor:pointer;white-space:nowrap;}
.mh-fill:hover{filter:brightness(1.07);}
.mh-fill:disabled{opacity:.6;cursor:default;}
.mh-st{font-size:11px;font-weight:700;border-radius:6px;padding:1px 7px;white-space:nowrap;}
.mh-st.st-auto{background:#0f7b3e;color:#fff;}
.mh-st.st-blk{background:var(--panel-2);color:var(--ink-soft);border:1px solid var(--line);}
.mh-card{border:1px solid var(--line);border-radius:14px;background:var(--panel);margin-bottom:12px;overflow:hidden;}
.mh-crow{display:flex;align-items:center;gap:12px;padding:14px 16px;cursor:pointer;list-style:none;}
.mh-crow::-webkit-details-marker{display:none;}
/* Demand tier leads with a WORD; the raw index is secondary (no naked number, no mid-blue). */
.mh-score{flex:0 0 auto;min-width:66px;padding:7px 9px;border-radius:11px;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:1px;line-height:1.05;font-variant-numeric:tabular-nums;text-align:center;}
.mh-score .mh-tier{font-weight:700;font-size:12px;white-space:nowrap;}
.mh-score .mh-num{font-size:10px;font-weight:600;opacity:.72;}
.mh-score.hi{background:#0f7b3e;color:#fff;}
.mh-score.mid{background:#fef2d6;color:#8a5a04;}
.mh-score.lo{background:var(--panel-2);color:var(--ink-soft);}
.mh-cinfo{min-width:0;flex:1 1 auto;}
.mh-cname{font-weight:700;font-size:16px;letter-spacing:-.01em;}
.mh-cstats{color:var(--ink-soft);font-size:13px;margin-top:2px;}
.mh-src{font-size:11.5px;font-weight:500;color:var(--ink-mute);margin-left:7px;}
.mh-caret{flex:0 0 auto;color:var(--ink-soft);transition:transform .15s;}
details[open] .mh-caret{transform:rotate(90deg);}
.mh-jobs{border-top:1px solid var(--line);}
.mh-job{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 10px;padding:11px 16px 11px 74px;border-top:1px solid var(--line);}
.mh-job:first-child{border-top:none;}
.mh-jtitle{font-weight:600;font-size:14px;color:var(--ink);text-decoration:none;}
.mh-jtitle:hover{text-decoration:underline;color:var(--accent);}
.mh-lang{flex:0 0 auto;border:1px solid var(--line);border-radius:6px;padding:1px 7px;font-size:11px;
  font-weight:600;color:#8a94a6;background:rgba(138,148,166,.10);white-space:nowrap;}
.mh-lang.ok{color:#1f8f4e;border-color:#1f8f4e;background:rgba(31,143,78,.12);}
.mh-jloc{color:var(--ink-soft);font-size:12.5px;}
.mh-apply{margin-left:auto;font-size:12.5px;font-weight:600;color:var(--accent);text-decoration:none;white-space:nowrap;}
.mh-empty{text-align:center;color:var(--ink-soft);padding:60px 20px;}
.mh-emp{border:1px solid var(--line);border-radius:14px;background:var(--panel);margin-bottom:18px;overflow:hidden;}
.mh-emp-sum{display:flex;align-items:center;gap:10px;padding:13px 16px;cursor:pointer;list-style:none;}
.mh-emp-sum::-webkit-details-marker{display:none;}
.mh-emp-t{font-weight:700;font-size:15px;letter-spacing:-.01em;}
.mh-emp-c{color:var(--ink-soft);font-size:12.5px;margin-left:auto;}
.mh-emp-list{border-top:1px solid var(--line);}
.mh-emp-row{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 10px;padding:10px 16px;border-top:1px solid var(--line);}
.mh-emp-row:first-child{border-top:none;}
.mh-emp-n{font-weight:600;font-size:13.5px;color:var(--ink);}
.mh-emp-seg{font-size:11px;font-weight:600;color:var(--ink-soft);border:1px solid var(--line);border-radius:6px;padding:1px 6px;}
.mh-emp-s{color:var(--ink-soft);font-size:12px;margin-left:auto;font-variant-numeric:tabular-nums;}
.mh-emp-note{color:var(--ink-soft);font-size:12px;padding:10px 16px 13px;border-top:1px solid var(--line);}
@media(max-width:760px){
  .mh-job{padding-left:16px;}.mh-apply{margin-left:0;}.mh-emp-s{margin-left:0;}}
</style>
"""


def _fmt_rate(x: float) -> str:
    if abs(x - round(x)) < 0.005:
        return f"${x:.0f}"
    return "$" + f"{x:.2f}".rstrip("0").rstrip(".")     # 21.65 -> $21.65, 28.80 -> $28.8


def _pay_html(j: dict) -> str:
    """Hourly pay next to the job: real posted rate (green) or a labeled estimate (muted)."""
    hp = mass_hiring.hourly_pay(j)
    if not hp:
        return ""
    lo, hi, est = hp
    rng = _fmt_rate(lo) if abs(lo - hi) < 0.5 else f"{_fmt_rate(lo)}–{_fmt_rate(hi)[1:]}"
    if est:
        return (f'<span class="mh-pay est" title="Оценка по типу роли — точную ставку '
                f'смотри в вакансии">≈{rng}/ч <span class="mh-est-t">оц.</span></span>')
    return f'<span class="mh-pay" title="Ставка из вакансии">{rng}/ч</span>'


_STATUS = {"auto": ("Авто", "st-auto", "Подаётся автоматически, без человека"),
           "blocked": ("Ассессмент", "st-blk",
                       "Обязательный человеческий видео/голос-ассессмент — авто невозможно")}


def _status_badge(j: dict) -> str:
    s = _STATUS.get(j.get("auto_status") or "")
    if not s:
        return ""
    return f'<span class="mh-st {s[1]}" title="{_esc(s[2])}">{s[0]}</span>'


_BILINGUAL_RE = re.compile(r"\b([A-Z][a-z]+)-English\s+Bilingual", re.I)
# Languages we can staff HONESTLY: persona = English + Russian, and the team passes a Russian test.
_STAFFABLE_LANGS = {"russian", "english"}


def _lang_badge(j: dict) -> str:
    """A bilingual job names its required language in the title (e.g. 'Russian-English Bilingual …').
    Surface it so the owner sees at a glance which language each needs, and mark the ones we can
    actually staff (English+Russian) green vs the rest muted (no native speaker → honest dead-end)."""
    m = _BILINGUAL_RE.search(j.get("title") or "")
    if not m:
        return ""
    lang = m.group(1)
    doable = lang.lower() in _STAFFABLE_LANGS
    cls = "mh-lang ok" if doable else "mh-lang"
    tip = ("Можем подать: персона англ+рус, языковой тест сдаёт команда"
           if doable else f"Требует язык: {lang} — не покрываем (нет носителя)")
    return f'<span class="{cls}" title="{_esc(tip)}">🌐 {_esc(lang)}</span>'


def _job_row(j: dict) -> str:
    url = _esc(j.get("apply_url"))
    title = _esc(j.get("title"))
    loc = _esc(j.get("location_raw") or "Удалённо")
    star = ('<span class="mh-star" title="Стабильная оплата (не комиссия)">★</span>'
            if j.get("comp_type") != "variable" else "")
    # Auto-fill (dry-run) is supported only where we have a working strategy — Avature (Maximus).
    fill = ""
    if "avature.net" in (j.get("apply_url") or "").lower():
        fill = (f'<button class="mh-fill" type="button" onclick="mhFill({int(j.get("id") or 0)},this)" '
                f'title="Заполнить форму автоматически (тест — ничего не отправляется)">Заполнить (тест)</button>')
    return (f'<div class="mh-job">{star}<a class="mh-jtitle" href="{url}" target="_blank" '
            f'rel="noopener">{title}</a><span class="mh-jloc">{loc}</span>{_lang_badge(j)}{_pay_html(j)}'
            f'{_status_badge(j)}'
            f'<a class="mh-apply" href="{url}" target="_blank" rel="noopener">подать вручную →</a>{fill}</div>')


def _company_card(c: dict, category: str | None, comp: str | None = None) -> str:
    key = c.get("company_key")
    js = mass_hiring.jobs(company_key=key, category=category, limit=60, comp=comp)
    # Show the source only when it adds info — i.e. it's meaningfully DIFFERENT from the company
    # display name. For own-ATS employers the slug just repeats the name ("Conduent Conduent"), so
    # it's dropped; aggregator sources (Himalayas/RemoteOK) that differ from the employer stay.
    company = c.get("company") or ""
    cnorm = re.sub(r"[^a-z0-9]", "", company.lower())
    src = {j.get("source") for j in js}
    src_tag = "".join(
        f'<span class="mh-src">{_esc(lbl)}</span>'
        for lbl in (_src_label(s) for s in sorted(src))
        if (n := re.sub(r"[^a-z0-9]", "", lbl.lower())) and n not in cnorm and cnorm not in n)
    score = int(c.get("mass_hiring_score") or 0)
    tier, cls = _tier(score)
    caret = ('<svg class="mh-caret" width="18" height="18" viewBox="0 0 24 24" fill="none" '
             'stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>')
    stats = f'{c["active_jobs"]} вакансий'
    if c.get("cs_jobs"):
        stats += f' · {c["cs_jobs"]} в поддержке'
    return (
        f'<details class="mh-card"><summary class="mh-crow">'
        f'<div class="mh-score {cls}" title="Индекс масс-хайринга {score}/100">'
        f'<span class="mh-tier">{tier}</span><span class="mh-num">{score}</span></div>'
        f'<div class="mh-cinfo"><div class="mh-cname">{_esc(c.get("company"))}{src_tag}</div>'
        f'<div class="mh-cstats">{stats}</div></div>{caret}</summary>'
        f'<div class="mh-jobs">{"".join(_job_row(j) for j in js)}</div></details>')


def _everify_panel(limit: int = 40) -> str:
    """Compact reference panel of large US employers (10k+ workforce) ranked by hiring-site
    count — a mass-hiring SIGNAL, not job postings. Reads ONLY the cached file (no network on
    render) and is fully guarded: any error yields an empty string so the board never breaks."""
    try:
        from backend.tools import everify_employers
        data = everify_employers.load_cached()
        emps = (data.get("employers") or [])[:limit]
        if not emps:
            return ""
        rows = []
        for e in emps:
            seg = _SEG_LABEL.get(e.get("segment") or "general", "Крупный работодатель")
            states = list(e.get("states") or [])[:5]
            extra = e.get("additional_state_count") or 0
            geo = ", ".join(states) + (f" +{extra}" if extra else "")
            sites = int(e.get("hiring_sites") or 0)
            rows.append(
                f'<div class="mh-emp-row"><span class="mh-emp-n">{_esc(e.get("name"))}</span>'
                f'<span class="mh-emp-seg">{_esc(seg)}</span>'
                f'<span class="mh-emp-s">{sites:,} площадок найма'
                + (f' · {_esc(geo)}' if geo else '') + '</span></div>')
        total = int(data.get("count") or len(emps))
        return (
            f'<details class="mh-emp"><summary class="mh-emp-sum">'
            f'<span class="mh-emp-t">Крупные работодатели США (10 000+ сотрудников)</span>'
            f'<span class="mh-emp-c">{total} компаний · по числу площадок найма</span></summary>'
            f'<div class="mh-emp-list">{"".join(rows)}</div>'
            f'<div class="mh-emp-note">Справочный список крупнейших работодателей США — '
            f'сигнал масс-хайринга для ручного поиска вакансий (не автоподача).</div></details>')
    except Exception:
        return ""


_MODAL_CSS = """
<style>
.mhm-back{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;z-index:80;}
.mhm-back.on{display:block;}
.mhm{position:fixed;z-index:81;left:50%;top:50%;transform:translate(-50%,-50%);width:min(480px,94vw);
  max-height:90vh;overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:16px;
  padding:20px;box-shadow:0 20px 60px rgba(0,0,0,.35);}
.mhm h2{margin:0 0 4px;font-size:19px;}
.mhm .mhm-note{color:var(--ink-soft);font-size:12.5px;margin:0 0 8px;}
.mhm label{display:block;font-size:13px;font-weight:600;margin:12px 0 5px;color:var(--ink);}
.mhm input[type=number],.mhm select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
  background:var(--panel-2);color:var(--ink);font:inherit;font-size:14px;box-sizing:border-box;}
.mhm-row{display:flex;gap:12px;}.mhm-row>div{flex:1;}
.mhm-lanes{display:grid;grid-template-columns:1fr 1fr;gap:7px 12px;margin-top:6px;}
.mhm-lanes label{display:flex;align-items:center;gap:7px;font-weight:600;margin:0;cursor:pointer;}
.mhm-lanes input,.mhm-check input{width:auto;}
.mhm-check{display:flex;align-items:center;gap:8px;font-weight:600;margin-top:14px;cursor:pointer;}
.mhm-status{margin-top:14px;font-size:12.5px;color:var(--ink-soft);white-space:pre-line;line-height:1.5;
  font-variant-numeric:tabular-nums;}
.mhm-foot{display:flex;gap:8px;margin-top:18px;}
.mhm-foot button{flex:1;display:inline-flex;align-items:center;justify-content:center;border-radius:var(--r-full);height:var(--ctl-h);padding:0 var(--ctl-px);font:inherit;font-weight:700;font-size:var(--ctl-fs);
  cursor:pointer;border:1px solid var(--line);}
.mhm-go{background:#0f7b3e;color:#fff;border-color:#0f7b3e;}
.mhm-stop{background:var(--panel-2);color:var(--ink);}
.mhm-x{background:transparent;color:var(--ink-soft);flex:0 0 auto;}
</style>
"""


def _run_modal() -> str:
    from backend.tools import mh_ondemand, mh_settings
    lanes = "".join(
        f'<label><input type="checkbox" name="lane" value="{_esc(k)}" checked> {_esc(v["label"])}'
        + ('' if v["limit"] else ' <span style="color:var(--ink-soft);font-weight:400">(все)</span>')
        + '</label>'
        for k, v in mh_ondemand.LANES.items())
    show_sp = "" if mh_settings.hide_spanish() else "checked"
    freq = "".join(f'<option value="{_esc(k)}">{_esc(lbl)}</option>'
                   for k, lbl in mh_ondemand.SCHEDULE_LABELS.items())
    return (
        '<div class="mhm-back" id="mhmBack" onclick="if(event.target===this)mhCloseRun()"></div>'
        '<div class="mhm" id="mhm" hidden>'
        '<h2>Запуск подачи</h2>'
        '<p class="mhm-note">Стартует сейчас, без ожидания крона. «(все)» — лейн подаёт на все свои вакансии.</p>'
        '<div class="mhm-row">'
        '<div><label>Сколько подач</label><input type="number" id="mhCount" min="1" placeholder="все"></div>'
        '<div><label>Потоков</label><input type="number" id="mhWorkers" min="1" max="8" value="2"></div>'
        '</div>'
        '<label>Лейны</label>'
        f'<div class="mhm-lanes">{lanes}</div>'
        f'<label class="mhm-check"><input type="checkbox" id="mhSpanish" {show_sp} onchange="mhSaveSpanish(this)">'
        ' Показывать испанские вакансии</label>'
        '<label>Частота автозапуска (расписание)</label>'
        f'<select id="mhFreq"><option value="">— не менять —</option>{freq}</select>'
        '<div class="mhm-status" id="mhStatus"></div>'
        '<div class="mhm-foot">'
        '<button class="mhm-go" type="button" onclick="mhStartRun()">▶ Запустить сейчас</button>'
        '<button class="mhm-stop" type="button" onclick="mhStopRun()">■ Стоп</button>'
        '<button class="mhm-x" type="button" onclick="mhCloseRun()">Закрыть</button>'
        '</div></div>')


_RUN_JS = """
<script>
function mhOpenRun(){document.getElementById('mhmBack').classList.add('on');
  document.getElementById('mhm').hidden=false;mhStatusPoll();}
function mhCloseRun(){document.getElementById('mhmBack').classList.remove('on');
  document.getElementById('mhm').hidden=true;if(window._mhTimer)clearTimeout(window._mhTimer);}
async function mhSaveSpanish(cb){var fd=new FormData();fd.append('show_spanish',cb.checked?'1':'0');
  try{await fetch('/mass-hiring/settings',{method:'POST',body:fd});}catch(e){}}
async function mhStartRun(){
  var lanes=[].slice.call(document.querySelectorAll('#mhm input[name=lane]:checked')).map(function(c){return c.value;});
  if(!lanes.length){alert('Выбери хотя бы один лейн');return;}
  var fd=new FormData();
  fd.append('count',document.getElementById('mhCount').value||'');
  fd.append('workers',document.getElementById('mhWorkers').value||'2');
  fd.append('lanes',lanes.join(','));
  fd.append('show_spanish',document.getElementById('mhSpanish').checked?'1':'0');
  fd.append('schedule',document.getElementById('mhFreq').value||'');
  document.getElementById('mhStatus').textContent='Запускаю…';
  try{var r=await fetch('/mass-hiring/apply_all',{method:'POST',body:fd});mhRender(await r.json());mhStatusPoll();}
  catch(e){document.getElementById('mhStatus').textContent='Ошибка запуска';}
}
async function mhStopRun(){try{var r=await fetch('/mass-hiring/apply_all_stop',{method:'POST'});mhRender(await r.json());}catch(e){}}
async function mhStatusPoll(){
  try{var r=await fetch('/mass-hiring/apply_all_status');var j=await r.json();mhRender(j);
    if(j.active){window._mhTimer=setTimeout(mhStatusPoll,3000);}}catch(e){window._mhTimer=setTimeout(mhStatusPoll,5000);}
}
function mhRender(j){
  var s=document.getElementById('mhStatus');if(!s)return;
  if(j&&j.error){s.textContent='⚠ '+j.error;}
  if(!j||(!j.active&&!(j.lanes&&Object.keys(j.lanes).length))){if(!(j&&j.error))s.textContent='';return;}
  var lines=[];
  if(j.lanes)for(var k in j.lanes){var L=j.lanes[k];
    var ic=L.state==='running'?'⏳':(L.state==='done'?'✓':(L.state==='error'?'✕':'·'));
    lines.push(ic+' '+L.label+' — '+L.state);}
  var hd=j.active?('Идёт '+(j.elapsed||0)+'с · потоков '+j.workers+(j.count?(' · '+j.count+' подач'):' · все')):'Остановлено';
  s.textContent=hd+'\\n'+lines.join('\\n');
}
</script>
"""


# Sticky-header hide + FAB collapse on scroll (self-contained; this page has neither the
# inbox #maillist nor the candidates #mbxlist scroll IIFE, so there is no double-bind).
_MH_SCROLL_JS = """
<script>(function(){
  var head=document.querySelector('.page-head'),
      pill=document.querySelector('.gm-topbar'),
      fab=document.querySelector('.fab-compose'),lastY=window.scrollY;
  if(!head&&!fab)return;
  window.addEventListener('scroll',function(){
    var y=window.scrollY,dy=y-lastY;if(Math.abs(dy)<=6)return;lastY=y;
    if(dy>0&&y>90){if(head)head.classList.add('hide');if(pill)pill.classList.add('hide');if(fab)fab.classList.add('collapsed');}
    else if(dy<0){if(head)head.classList.remove('hide');if(pill)pill.classList.remove('hide');if(fab)fab.classList.remove('collapsed');}
  },{passive:true});
})();</script>
"""

_PLAY_SVG = ('<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
             '<polygon points="6 4 20 12 6 20"/></svg>')
_REFRESH_ICON = ('<form method="post" action="/mass-hiring/collect" style="display:inline">'
                 '<button class="iconbtn" type="submit" title="Обновить вакансии" aria-label="Обновить вакансии">'
                 '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                 'stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/>'
                 '<path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg></button></form>')


def render_page(category: str | None = None, comp: str | None = None) -> str:
    # Filters were removed by owner request — the board shows all active jobs; the only
    # display toggle is «Показывать испанские» (persisted in mh_settings, default hidden).
    st = mass_hiring.stats()
    cos = mass_hiring.companies(limit=200)

    body = "".join(_company_card(c, None, None) for c in cos) or \
        ('<div class="mh-empty">Пока пусто. Нажми «Обновить», чтобы собрать вакансии '
         'из источников (Conduent / Alorica / Himalayas / …).</div>')

    info = ('<b style="color:#f5a623">★</b> — стабильная оплата (не комиссия).<br>'
            '«оц.» рядом со ставкой — оценка по типу роли (точную смотри в вакансии).')
    ph = mailcrm_ui._page_head(
        "Mass Hiring", count=st["active"],
        primary={"label": "Запустить подачу", "onclick": "mhOpenRun()", "svg": _PLAY_SVG},
        icons=_REFRESH_ICON,
        meta=(f'{st["active"]} вакансий · {st["companies"]} компаний · '
              f'обновлено {_ago(st.get("last_collected", 0))}'),
        info=info)
    head = (
        f'<div class="mh-wrap">{_CSS}{_MODAL_CSS}{ph}'
        f'{_everify_panel()}'
        f'{body}{_run_modal()}</div>{_JS}{_RUN_JS}{_MH_SCROLL_JS}')
    return mailcrm_ui._page("masshiring", head)
