"""Server-rendered UI for the isolated company discovery/application pipeline."""
from __future__ import annotations

import json
from html import escape

from backend.profiles.store import is_sample_profile, load_profiles
from backend.tools import mailcrm_ui


STATE_LABELS = {
    "queued": "В очереди",
    "claimed": "Проверяется",
    "awaiting_approval": "Готово к запуску",
    "approved": "Разрешено",
    "submit_approved": "Пакет подтверждён",
    "preparing": "Заполняется",
    "ready_for_review": "Нужна проверка",
    "needs_input": "Нужны данные",
    "rejected": "Пропущено",
    "blocked": "Заблокировано",
    "failed": "Ошибка",
    "submitting": "Отправляется",
    "auto_submitted": "Отправлено",
    "submission_failed": "Не отправлено",
    "human_submitted": "Отправлено вручную",
}


def real_profiles() -> list[dict]:
    """Return only locally configured real people; never expose identity fields."""
    try:
        profiles = load_profiles()
    except Exception:
        return []
    return [
        {"id": profile.id, "name": profile.full_name}
        for profile in profiles.values()
        if not is_sample_profile(profile) and not profile.is_synthetic
    ]


def _n(value) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _row(item: dict) -> str:
    state = str(item.get("state") or "queued")
    label = STATE_LABELS.get(state, state)
    company = escape(str(item.get("company_name") or "Компания"))
    title = escape(str(item.get("title") or "Без названия"))
    source = escape(str(item.get("source") or ""))
    location = escape(str(item.get("location_raw") or "Remote"))
    url = escape(str(item.get("apply_url") or ""), quote=True)
    fit = item.get("fit_score")
    fit_text = f"{float(fit):.0f}%" if fit is not None else "—"
    return (
        f'<article class="mh-row" data-state="{escape(state, quote=True)}">'
        '<div class="mh-job"><div class="mh-company">'
        f'{company}<span>{source}</span></div><a href="{url}" target="_blank" '
        f'rel="noopener">{title}</a><small>{location}</small></div>'
        f'<div class="mh-fit"><b>{fit_text}</b><span>совпадение</span></div>'
        f'<div class="mh-state {escape(state, quote=True)}">{escape(label)}</div>'
        '</article>'
    )


def render_page(snapshot: dict, run: dict, *, selected_profile: str = "") -> str:
    profiles = snapshot.get("profiles") or []
    if not selected_profile and profiles:
        selected_profile = profiles[0]["id"]
    options = "".join(
        f'<option value="{escape(p["id"], quote=True)}"'
        f'{" selected" if p["id"] == selected_profile else ""}>'
        f'{escape(p["name"])} · {escape(p["id"])}</option>' for p in profiles
    )
    if not options:
        options = '<option value="">Нет готового реального профиля</option>'

    companies = snapshot.get("companies") or {}
    jobs = snapshot.get("jobs") or {}
    apps = snapshot.get("applications") or {}
    by_state = apps.get("by_state") or {}
    ready = int(by_state.get("awaiting_approval", 0))
    submitted = int(by_state.get("auto_submitted", 0)) + int(
        by_state.get("human_submitted", 0))
    rows = "".join(_row(item) for item in snapshot.get("rows") or [])
    if not rows:
        rows = ('<div class="mh-empty"><b>Очередь пока пуста</b>'
                '<span>Сначала обновите базу, затем сформируйте пакет заявок.</span></div>')

    available = bool(snapshot.get("available"))
    profile_ready = bool(profiles)
    can_start = available and profile_ready and ready > 0 and run.get("state") != "running"
    runtime_text = "Локальный контур готов" if available else "Нужна локальная база данных"
    runtime_class = "ok" if available else "warn"
    run_json = json.dumps(run, ensure_ascii=False, default=str).replace("</", "<\\/")

    css = """
<style>
.mh{max-width:1180px;margin:0 auto}.mh-head{display:flex;align-items:flex-start;justify-content:space-between;gap:22px;margin-bottom:22px}.mh-head h1{font-size:29px;line-height:1.15;letter-spacing:-.035em;margin:0 0 7px}.mh-head p{color:var(--ink-soft);font-size:14px;margin:0;max-width:680px}.mh-runtime{display:flex;align-items:center;gap:8px;white-space:nowrap;padding:9px 13px;background:var(--panel);border:1px solid var(--line);border-radius:var(--r-full);font-weight:600;color:var(--ink-soft)}.mh-runtime i{width:8px;height:8px;border-radius:50%;background:#188038}.mh-runtime.warn i{background:#f9ab00}.mh-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border:1px solid var(--line);border-radius:var(--r);background:var(--panel);overflow:hidden;margin-bottom:18px}.mh-stat{padding:18px 20px;border-right:1px solid var(--line)}.mh-stat:last-child{border-right:0}.mh-stat b{display:block;font:600 25px/1 var(--ff-mono);letter-spacing:-.05em}.mh-stat span{display:block;color:var(--ink-mute);margin-top:8px}.mh-control{display:grid;grid-template-columns:minmax(220px,1fr) 140px 140px auto;gap:10px;align-items:end;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:17px;margin-bottom:18px}.mh-control label{margin:0 0 6px}.mh-control select,.mh-control input{width:100%;height:42px}.mh-actions{display:flex;gap:8px;align-items:center;justify-content:flex-end}.mh-btn{border:0;border-radius:var(--r-full);min-height:42px;padding:0 17px;font:600 13px var(--ff);cursor:pointer;white-space:nowrap}.mh-btn.primary{background:var(--accent);color:white}.mh-btn.primary:hover{background:var(--accent-deep)}.mh-btn.secondary{background:var(--panel-2);color:var(--ink)}.mh-btn.danger{background:#fce8e6;color:var(--danger)}.mh-btn:disabled{opacity:.46;cursor:not-allowed}.mh-note{display:flex;align-items:center;gap:11px;padding:12px 14px;border-radius:10px;background:#e6f4ea;color:#137333;margin-bottom:18px}.mh-note.warn{background:#fef7e0;color:#7c5b00}.mh-note svg{width:19px;height:19px;flex:0 0 auto}.mh-progress{display:none;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px;margin-bottom:18px}.mh-progress.show{display:block}.mh-progress-top{display:flex;justify-content:space-between;gap:12px;margin-bottom:10px}.mh-progress-top b{font-size:14px}.mh-progress-top span{font-family:var(--ff-mono);color:var(--ink-mute)}.mh-track{height:7px;background:var(--panel-2);border-radius:9px;overflow:hidden}.mh-track i{display:block;height:100%;width:0;background:var(--accent);transition:width .3s}.mh-list-head{display:flex;align-items:center;justify-content:space-between;margin:4px 0 10px}.mh-list-head h2{font-size:18px;margin:0}.mh-list-head span{color:var(--ink-mute)}.mh-list{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}.mh-row{display:grid;grid-template-columns:minmax(0,1fr) 110px 150px;align-items:center;gap:18px;padding:14px 18px;border-bottom:1px solid var(--line)}.mh-row:last-child{border-bottom:0}.mh-row:hover{background:#f8fafd}.mh-job{min-width:0}.mh-company{display:flex;align-items:center;gap:8px;color:var(--ink-soft);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.035em}.mh-company span{font:400 10px var(--ff-mono);color:var(--ink-mute);text-transform:none}.mh-job>a{display:block;color:var(--ink);font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin:2px 0}.mh-job>a:hover{color:var(--accent)}.mh-job small{color:var(--ink-mute)}.mh-fit b,.mh-fit span{display:block}.mh-fit b{font:600 14px var(--ff-mono)}.mh-fit span{font-size:10.5px;color:var(--ink-mute)}.mh-state{justify-self:end;font:500 11px var(--ff-mono);padding:5px 9px;border-radius:var(--r-full);background:var(--panel-2);color:var(--ink-soft)}.mh-state.awaiting_approval,.mh-state.approved{background:var(--accent-soft);color:var(--accent-deep)}.mh-state.auto_submitted,.mh-state.human_submitted{background:#e6f4ea;color:#137333}.mh-state.failed,.mh-state.submission_failed,.mh-state.blocked{background:#fce8e6;color:var(--danger)}.mh-empty{text-align:center;padding:50px 20px;color:var(--ink-soft)}.mh-empty b,.mh-empty span{display:block}.mh-empty span{color:var(--ink-mute);margin-top:6px}.mh-modal[hidden]{display:none}.mh-modal{position:fixed;inset:0;z-index:100;display:grid;place-items:center;padding:20px;background:rgba(32,33,36,.42)}.mh-dialog{width:min(520px,100%);background:#fff;border-radius:16px;padding:24px;box-shadow:0 24px 80px rgba(0,0,0,.24)}.mh-dialog h2{font-size:21px;margin:0 0 8px}.mh-dialog p{color:var(--ink-soft);margin:0 0 16px}.mh-confirm{display:flex;gap:10px;align-items:flex-start;background:#fef7e0;color:#7c5b00;border-radius:9px;padding:11px 12px;margin-bottom:18px}.mh-confirm input{margin-top:3px}.mh-dialog-actions{display:flex;justify-content:flex-end;gap:8px}.mh-toast{position:fixed;right:22px;bottom:22px;z-index:120;background:#202124;color:#fff;padding:12px 16px;border-radius:9px;box-shadow:0 8px 30px rgba(0,0,0,.2)}.mh-toast[hidden]{display:none}
@media(max-width:900px){.mh-grid{grid-template-columns:repeat(2,1fr)}.mh-stat:nth-child(2){border-right:0}.mh-stat:nth-child(-n+2){border-bottom:1px solid var(--line)}.mh-control{grid-template-columns:1fr 1fr}.mh-actions{grid-column:1/-1;justify-content:flex-start}}
@media(max-width:760px){main{padding:18px 14px 28px}.mh-head{display:block}.mh-runtime{display:inline-flex;margin-top:14px}.mh-grid{grid-template-columns:1fr 1fr}.mh-stat{padding:15px}.mh-control{grid-template-columns:1fr}.mh-actions{grid-column:auto;display:grid;grid-template-columns:1fr 1fr}.mh-actions .primary{grid-column:1/-1}.mh-row{grid-template-columns:minmax(0,1fr) auto}.mh-fit{display:none}.mh-state{font-size:9px}.mh-job>a{white-space:normal}.mh-list-head span{display:none}}
</style>"""
    body = f"""
<style>.mh-state.submit_approved{{background:var(--accent-soft);color:var(--accent-deep)}}.mh-confirm{{flex-direction:column}}.mh-confirm input{{width:100%;background:#fff}}</style>
<section class="mh">
  <header class="mh-head"><div><h1>Массовый найм</h1><p>Независимая база компаний и REMOTE-вакансий. Очередь и браузерный worker отделены от основного каталога JobFinder.</p></div><div class="mh-runtime {runtime_class}"><i></i>{escape(runtime_text)}</div></header>
  <div class="mh-grid">
    <div class="mh-stat"><b>{_n(companies.get('total'))}</b><span>компаний найдено</span></div>
    <div class="mh-stat"><b>{_n(jobs.get('active'))}</b><span>REMOTE-вакансий</span></div>
    <div class="mh-stat"><b>{_n(ready)}</b><span>готово к запуску</span></div>
    <div class="mh-stat"><b>{_n(submitted)}</b><span>отправлено</span></div>
  </div>
  <form class="mh-control" id="mh-controls" onsubmit="return false">
    <div><label for="mh-profile">Профиль кандидата</label><select id="mh-profile">{options}</select></div>
    <div><label for="mh-count">Размер пакета</label><input id="mh-count" type="number" min="1" max="250" value="25"></div>
    <div><label for="mh-fit">Мин. совпадение</label><input id="mh-fit" type="number" min="0" max="100" value="35"></div>
    <div class="mh-actions"><button class="mh-btn secondary" id="mh-sync" type="button" {'disabled' if not available else ''}>Обновить базу</button><button class="mh-btn secondary" id="mh-build" type="button" {'disabled' if not available or not profile_ready else ''}>Сформировать</button><button class="mh-btn primary" id="mh-start" type="button" {'disabled' if not can_start else ''}>Запустить подачу</button><button class="mh-btn danger" id="mh-stop" type="button" {'disabled' if run.get('state') != 'running' else ''}>Остановить</button></div>
  </form>
  <div class="mh-note {'warn' if not available or not profile_ready else ''}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg><span>{escape(snapshot.get('message') or 'Отправка начинается только после подтверждения выбранного пакета. Основной контур заявок продолжает работать отдельно.')}</span></div>
  <div class="mh-progress {'show' if run.get('state') == 'running' else ''}" id="mh-progress"><div class="mh-progress-top"><b id="mh-current">{escape(str(run.get('current') or 'Подготовка…'))}</b><span id="mh-counter">{_n(run.get('done'))} / {_n(run.get('total'))}</span></div><div class="mh-track"><i id="mh-bar"></i></div></div>
  <div class="mh-list-head"><h2>Очередь подачи</h2><span>Последние {len(snapshot.get('rows') or [])} заявок</span></div><div class="mh-list">{rows}</div>
</section>
<div class="mh-modal" id="mh-modal" hidden><div class="mh-dialog" role="dialog" aria-modal="true" aria-labelledby="mh-modal-title"><h2 id="mh-modal-title">Подтвердить массовую подачу</h2><p>Профиль: <b id="mh-confirm-profile"></b>. Будут автоматически заполнены и отправлены <b id="mh-confirm-count"></b> подготовленных REMOTE-заявок только из нового контура.</p><label class="mh-confirm"><span>Для подтверждения введите <b id="mh-confirm-phrase"></b></span><input id="mh-confirm-text" type="text" autocomplete="off" spellcheck="false"></label><div class="mh-dialog-actions"><button class="mh-btn secondary" type="button" id="mh-cancel">Отмена</button><button class="mh-btn primary" type="button" id="mh-confirm" disabled>Подтвердить и запустить</button></div></div></div>
<div class="mh-toast" id="mh-toast" hidden></div>
<script>window.MASS_HIRING_RUN={run_json};
const $=s=>document.querySelector(s), toast=t=>{{const e=$('#mh-toast');e.textContent=t;e.hidden=false;setTimeout(()=>e.hidden=true,3500)}};
const payload=()=>new URLSearchParams({{profile:$('#mh-profile').value,count:$('#mh-count').value,min_fit:$('#mh-fit').value,confirmation:$('#mh-confirm-text')?.value||''}});
async function post(url){{const r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:payload()}});const j=await r.json();if(!r.ok||j.error)throw new Error(j.error||'Операция не выполнена');return j}}
$('#mh-sync')?.addEventListener('click',async e=>{{const btn=e.currentTarget;btn.disabled=true;try{{await post('/mass-hiring/sync');window.MASS_HIRING_SYNC=true;toast('Обновление запущено');poll()}}catch(x){{toast(x.message)}}finally{{btn.disabled=false}}}});
$('#mh-build')?.addEventListener('click',async e=>{{const btn=e.currentTarget;btn.disabled=true;try{{const j=await post('/mass-hiring/build');toast(`Очередь обновлена: ${{j.prepared||0}} готово`);location.reload()}}catch(x){{toast(x.message)}}finally{{btn.disabled=false}}}});
$('#mh-start')?.addEventListener('click',()=>{{const n=Math.min(Number($('#mh-count').value)||1,{ready});const phrase=`SEND ${{n}}`;$('#mh-confirm-profile').textContent=$('#mh-profile').selectedOptions[0]?.textContent||$('#mh-profile').value;$('#mh-confirm-count').textContent=n;$('#mh-confirm-phrase').textContent=phrase;$('#mh-confirm-text').value='';$('#mh-confirm-text').dataset.phrase=phrase;$('#mh-confirm').disabled=true;$('#mh-modal').hidden=false;$('#mh-confirm-text').focus()}});
$('#mh-cancel')?.addEventListener('click',()=>$('#mh-modal').hidden=true);$('#mh-confirm-text')?.addEventListener('input',e=>$('#mh-confirm').disabled=e.target.value.trim()!==e.target.dataset.phrase);
$('#mh-confirm')?.addEventListener('click',async e=>{{e.currentTarget.disabled=true;try{{const j=await post('/mass-hiring/start');$('#mh-modal').hidden=true;toast(`Запущено: ${{j.total||0}} заявок`);$('#mh-progress').classList.add('show');poll()}}catch(x){{toast(x.message);e.currentTarget.disabled=false}}}});
$('#mh-stop')?.addEventListener('click',async()=>{{try{{await post('/mass-hiring/stop');toast('Остановка запрошена')}}catch(x){{toast(x.message)}}}});
async function poll(){{try{{const j=await (await fetch('/mass-hiring/status')).json();const total=j.total||0,done=j.done||0;$('#mh-current').textContent=j.current||'Обработка…';$('#mh-counter').textContent=`${{done}} / ${{total}}`;$('#mh-bar').style.width=(total?Math.min(100,done/total*100):0)+'%';if(j.sync?.state==='running')window.MASS_HIRING_SYNC=true;if(j.state==='running'||j.sync?.state==='running')setTimeout(poll,1500);else if(window.MASS_HIRING_RUN.state==='running'||window.MASS_HIRING_SYNC)location.reload()}}catch(_e){{setTimeout(poll,3000)}}}}if(window.MASS_HIRING_RUN.state==='running')poll();
</script>"""
    return mailcrm_ui._page(
        "mass_hiring", css + body, page_title="JobFinder — Массовый найм")
