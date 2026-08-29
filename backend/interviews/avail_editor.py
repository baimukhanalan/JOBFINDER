"""Shared weekly-availability editor — MULTIPLE time windows per day.

Used by both the operator «Пользователи» edit page (`users_ui`) and the interviewer
cabinet (`cabinet_ui`). A day can hold any number of windows (e.g. 06:30–14:00 AND
18:00–01:00); a day with zero windows is a day off. Each window is independently
same-day (`start<end`), overnight (`end<start`, wraps past midnight) or 24h
(`start==end`) — the slot layer materialises them all (see `slots`).

Form encoding: each window contributes a `start_<dow>` + `end_<dow>` input PAIR; the
route reads them with `form.getlist("start_<dow>")` / `getlist("end_<dow>")` and zips —
so adding/removing a window in JS needs NO index bookkeeping. Neutral Russian, no stack
names. Styling reuses the shared shell tokens; caller includes `CSS` + `JS` once.
"""
from __future__ import annotations

_WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг",
             "Пятница", "Суббота", "Воскресенье"]


def _hhmm(m) -> str:
    m = int(m or 0)
    return f"{m // 60:02d}:{m % 60:02d}"


def _win_row(dow: int, start_min, end_min) -> str:
    return (
        '<div class="avd-win">'
        f'<input type="time" name="start_{dow}" value="{_hhmm(start_min)}" aria-label="начало">'
        '<span class="avd-sep">–</span>'
        f'<input type="time" name="end_{dow}" value="{_hhmm(end_min)}" aria-label="конец">'
        '<button type="button" class="avd-x" onclick="avdRm(this)" aria-label="убрать промежуток">×</button>'
        '</div>')


def render_days(availability: list[dict]) -> str:
    """The 7 day blocks with their windows. `availability` = the responsible's stored
    windows (may be several per dow, may skip days)."""
    by_dow: dict[int, list] = {}
    for r in availability or []:
        if r.get("enabled", True):
            by_dow.setdefault(int(r["dow"]), []).append(r)
    days = []
    for d in range(7):
        wins = sorted(by_dow.get(d, []), key=lambda r: int(r.get("start_min") or 0))
        rows = "".join(_win_row(d, r["start_min"], r["end_min"]) for r in wins)
        empty_hidden = " hidden" if wins else ""
        days.append(
            f'<div class="avd-day" data-dow="{d}">'
            f'<div class="avd-head"><b class="avd-dow">{_WEEKDAYS[d]}</b>'
            f'<button type="button" class="avd-add" onclick="avdAdd({d})">+ промежуток</button></div>'
            f'<div class="avd-wins" id="avd-wins-{d}">{rows}</div>'
            f'<div class="avd-empty"{empty_hidden}>выходной — промежутков нет</div>'
            '</div>')
    return f'<div class="avd-days">{"".join(days)}</div>'


CSS = """
.avd-days{display:flex;flex-direction:column;gap:10px}
.avd-day{background:var(--panel);border:1px solid var(--line);border-radius:var(--r-sm);padding:11px 13px}
.avd-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}
.avd-dow{font-size:14.5px;color:var(--ink)}
.avd-add{background:var(--accent-soft);color:var(--accent-deep,var(--accent));border:1px solid transparent;border-radius:var(--r-full);padding:6px 13px;font-size:12.5px;font-weight:700;cursor:pointer;min-height:34px}
.avd-add:hover{border-color:var(--accent)}
.avd-wins{display:flex;flex-direction:column;gap:7px}
.avd-wins:empty{display:none}
.avd-win{display:flex;align-items:center;gap:8px}
.avd-win input[type=time]{flex:1;min-width:0;padding:9px 10px;text-align:center}
.avd-sep{color:var(--ink-mute);flex:0 0 auto}
.avd-x{flex:0 0 auto;width:36px;height:36px;border:1px solid var(--line-strong);background:var(--panel);color:var(--danger);border-radius:8px;font-size:17px;line-height:1;cursor:pointer}
.avd-x:hover{background:#fdeceb}
.avd-empty{color:var(--ink-mute);font-size:12.5px}
.avd-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px;align-items:center}
"""

JS = """
<script>
function avdMakeWin(d, s, e){
  var w=document.createElement('div'); w.className='avd-win';
  w.innerHTML='<input type="time" name="start_'+d+'" value="'+(s||'09:00')+'" aria-label="начало">'
    +'<span class="avd-sep">\\u2013</span>'
    +'<input type="time" name="end_'+d+'" value="'+(e||'17:00')+'" aria-label="конец">'
    +'<button type="button" class="avd-x" onclick="avdRm(this)" aria-label="убрать промежуток">\\u00d7</button>';
  return w;
}
function avdAdd(d){
  var box=document.getElementById('avd-wins-'+d); if(!box) return;
  box.appendChild(avdMakeWin(d));
  var em=box.closest('.avd-day').querySelector('.avd-empty'); if(em) em.hidden=true;
}
function avdRm(btn){
  var w=btn.closest('.avd-win'), box=w.parentNode, day=box.closest('.avd-day');
  w.remove();
  if(box.children.length===0){var em=day.querySelector('.avd-empty'); if(em) em.hidden=false;}
}
// copy Monday's windows to every other day
function avdCopyMon(){
  var mon=document.getElementById('avd-wins-0'); if(!mon) return;
  var wins=[].map.call(mon.querySelectorAll('.avd-win'), function(w){
    var i=w.querySelectorAll('input[type=time]'); return [i[0].value, i[1].value];});
  for(var d=1; d<7; d++){
    var box=document.getElementById('avd-wins-'+d); if(!box) continue;
    box.innerHTML='';
    wins.forEach(function(p){ box.appendChild(avdMakeWin(d, p[0], p[1])); });
    var em=box.closest('.avd-day').querySelector('.avd-empty'); if(em) em.hidden=(wins.length>0);
  }
}
</script>
"""
