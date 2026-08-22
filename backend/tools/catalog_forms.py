"""Scrape application-form questions for ashby/lever/workable catalog rows via Playwright.

Their public APIs don't expose the apply-form questions (greenhouse does), so we
render the apply page and read the field-level questions: text/select/textarea/file
controls by their label, and radio/checkbox groups by their fieldset legend. Writes
into job_catalog.questions via catalog_db.set_questions.

Workable is best-effort: the form generally scrapes fine (real apply page is the
posting URL + /apply, see `_apply_url`), but treat 0 questions on any given board
as an accepted outcome (odd markup, A/B'd layout, etc.), not a bug.

WARNING: --limit defaults to 0, which means UNBOUNDED — with no --limit, a run
scrapes every missing-question row for the selected ATS(es), one Playwright page
load per row. Always pass --limit for a manual or cron invocation unless you
deliberately want a full unbounded scrape.

    python -m backend.tools.catalog_forms                    # ashby + lever + workable, ALL missing (unbounded)
    python -m backend.tools.catalog_forms ashby               # one ATS, all missing (unbounded)
    python -m backend.tools.catalog_forms --limit 200          # all three ATS, capped per-ATS
    python -m backend.tools.catalog_forms ashby lever --limit 50
"""
from __future__ import annotations

from playwright.sync_api import sync_playwright

from backend.tools import catalog_db

# Entry/container-based extractor covering the three DOM styles we scrape:
#  - Lever: native <select>/<input> + .application-label sibling label.
#  - Workable: radio/checkbox groups in <fieldset role=radiogroup aria-labelledby=..>
#    with an EMPTY <legend> — the question text is the aria-labelledby target span.
#  - Ashby: radio/checkbox groups + Yes/No <button> pairs + input[role=combobox]
#    dropdowns, all inside .ashby-application-form-field-entry with the label in a
#    .ashby-application-form-question-title div (never a <legend>).
# Combobox options aren't in the static DOM — a Python pass (_open_comboboxes) opens
# each and reads them; here they're emitted as type "select" flagged _combo.
_JS = r"""()=>{
  const seen=new Set(), out=[];
  const clean=(s)=>(s||'').replace(/\s+/g,' ').trim();
  const stripCtrl=(el)=>{const c=el.cloneNode(true);c.querySelectorAll('input,select,textarea,svg,button,option').forEach(n=>n.remove());return clean(c.innerText);};
  const byId=(a)=>{if(!a)return '';const l=document.getElementById(a.split(' ')[0]);return l?clean(l.innerText):'';};
  const lf=(el)=>{
    const al=byId(el.getAttribute('aria-labelledby')); if(al) return al;       // Ashby/Workable
    if(el.id){const l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]');if(l)return clean(l.innerText);}
    const wl=el.closest('label'); if(wl){const t=stripCtrl(wl); if(t) return t;}
    if(el.getAttribute('aria-label'))return clean(el.getAttribute('aria-label'));
    const card=el.closest('.ashby-application-form-field-entry,[class*="_fieldEntry_"],.application-question,[class*=application-question],[class*=question],fieldset,li');
    if(card){const ll=card.querySelector('.ashby-application-form-question-title,.application-label,[class*="_label_"],[class*="_heading_"],label,legend,[class*=label]');
      if(ll){const t=stripCtrl(ll); if(t) return t;}}
    return '';
  };
  const contOf=(el)=> el.closest('.ashby-application-form-field-entry,[class*="_fieldEntry_"]')
     || el.closest('fieldset,[role=radiogroup],[role=group]')
     || el.closest('.application-question,[class*=application-question],li');
  const groupQ=(c)=>{
    if(!c) return '';
    const a=c.getAttribute&&byId(c.getAttribute('aria-labelledby')); if(a) return a;  // Workable
    const t=c.querySelector('.ashby-application-form-question-title,[class*="_label_"],[class*="_heading_"],.application-label');
    if(t){const x=stripCtrl(t); if(x) return x;}                                       // Ashby / Lever
    const lg=c.querySelector('legend'); if(lg){const x=clean(lg.innerText); if(x) return x;}
    return '';
  };
  const optLabel=(el)=>{
    const wl=el.closest('label'); if(wl){const t=stripCtrl(wl); if(t) return t;}
    if(el.id){const l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]');if(l)return clean(l.innerText);}
    return clean(el.getAttribute('aria-label')||'');
  };
  // EEOC/demographic questions are left blank by policy (fill side skips them too) —
  // don't pollute the catalog with survey items.
  const DEMO=/(gender|racial|\brace\b|ethnic|hispanic|latin[ox]?\b|veteran|disabilit|transgender|lgbtq|sexual orientation|pronoun)/i;
  const isDemo=(q)=>DEMO.test(q||'');
  // ---- Ashby Yes/No <button> groups (FIRST — mark their containers so the hidden
  //      backing checkbox below doesn't emit the same question without options) ----
  const btnConts=new Set();
  document.querySelectorAll('.ashby-application-form-field-entry,[class*="_fieldEntry_"]').forEach(entry=>{
    const btns=[...entry.querySelectorAll('button')].filter(b=>{const t=clean(b.textContent);return t && !/^upload/i.test(t) && (/_option_/.test(b.className)||/yesno/i.test(b.className)||/^(yes|no)$/i.test(t));});
    if(btns.length<2) return;
    btnConts.add(entry);
    const t=entry.querySelector('.ashby-application-form-question-title,[class*="_label_"],[class*="_heading_"]');
    const q=t?stripCtrl(t):''; if(!q||seen.has(q)||isDemo(q)) return;
    seen.add(q); out.push({label:q,type:'choice',required:false,options:btns.map(b=>clean(b.textContent))});
  });
  // Group question resolved by climbing ancestors for a label element that isn't the
  // option's own label (handles Lever's per-option wrappers + Ashby/Workable).
  const questionFor=(el)=>{
    let n=el.parentElement;
    for(let i=0;i<7 && n;i++, n=n.parentElement){
      const a=n.getAttribute&&byId(n.getAttribute('aria-labelledby')); if(a) return a;
      // Only the STABLE question-title classes — NOT generic _label_ (Ashby tags each
      // option's own label with a hashed _label_ too, which would grab an option).
      const t=n.querySelector('.ashby-application-form-question-title,.application-label,legend');
      if(t && !t.contains(el)){const x=stripCtrl(t); if(x&&x.length>2) return x;}
    }
    return lf(el);
  };
  // Radios group by NAME (Lever puts each option in its own wrapper but they share a
  // name); checkboxes group by CONTAINER (Ashby multi-selects use a distinct name per
  // option, so name-grouping would split them).
  const radioG=new Map(), cbG=new Map();
  document.querySelectorAll('input[type=radio]').forEach(el=>{
    const c=contOf(el); if(c&&btnConts.has(c)) return;
    const k=el.name?('n:'+el.name):c; if(k==null) return;
    if(!radioG.has(k)) radioG.set(k,{q:questionFor(el),opts:[]});
    const g=radioG.get(k); const ol=optLabel(el); if(ol&&!g.opts.includes(ol)) g.opts.push(ol);
  });
  document.querySelectorAll('input[type=checkbox]').forEach(el=>{
    const c=contOf(el); if(!c||btnConts.has(c)) return;
    if(!cbG.has(c)) cbG.set(c,{q:questionFor(el),opts:[]});
    const g=cbG.get(c); const ol=optLabel(el); if(ol&&!g.opts.includes(ol)) g.opts.push(ol);
  });
  const emitGroup=(g,isCb)=>{
    const q=clean(g.q).replace(/^\*\s*/,'');
    if(!q||seen.has(q)||isDemo(q)) return;
    const opts=g.opts.filter(o=>o&&o.replace(/^\*\s*/,'')!==q);
    seen.add(q);
    const it={label:q, type:(isCb&&opts.length>1?'multi_select':'choice'), required:false};
    if(opts.length) it.options=opts;
    out.push(it);
  };
  radioG.forEach(g=>emitGroup(g,false));
  cbG.forEach(g=>emitGroup(g,true));
  // ---- text / textarea / native select / combobox ----
  document.querySelectorAll('input:not([type=radio]):not([type=checkbox]):not([type=hidden]):not([type=submit]):not([type=button]),select,textarea').forEach(el=>{
    const q=lf(el); const tag=el.tagName.toLowerCase();
    const isCombo = (el.getAttribute('role')==='combobox') || /autocomplete/i.test(el.className||'');
    const t = tag==='select'?'select':(tag==='textarea'?'textarea':(isCombo?'select':(el.type||'text')));
    if(q && !seen.has(q) && !isDemo(q)){
      seen.add(q);
      const it={label:q, type:t, required:!!(el.required||el.getAttribute('aria-required')==='true')};
      if(tag==='select'){
        const opts=[...el.options].map(o=>(o.textContent||'').trim())
          .filter(x=>x && !/^(select|choose|--|please|pick)/i.test(x));
        if(opts.length) it.options=opts;
      }
      if(isCombo) it._combo=true;  // Python _open_comboboxes fetches its options
      out.push(it);
    }
  });
  return out;
}"""


def _apply_url(ats: str, url: str) -> str:
    u = (url or "").split("?")[0].rstrip("/")
    if ats == "ashby" and not u.endswith("/application"):
        u += "/application"
    if ats == "lever" and not u.endswith("/apply"):
        u += "/apply"
    if ats == "workable" and not u.endswith("/apply"):
        # Brief assumed the posting URL itself was the apply form; live-verified
        # that's just the description page (0 inputs) — the actual form is at
        # the same URL + /apply (Workable 301-redirects to add the company slug
        # + trailing slash, e.g. .../j/<code>/apply -> .../<co>/j/<code>/apply/).
        u += "/apply"
    return u


# Reads the options of an Ashby-style input[role=combobox] by OPENING it (options
# live in a floating portal, never the static DOM). Static SingleSelect -> options;
# a geo/typeahead (0 options on open) -> left empty. Mirrors harvest_react_selects.
_COMBO_OPEN_JS = r"""()=>[...document.querySelectorAll('input[role="combobox"],.ashby-application-form-input-autocomplete')].map(el=>{
  const c=el.closest('.ashby-application-form-field-entry,[class*="_fieldEntry_"],.application-question,fieldset,li');
  let q='';
  if(c){const t=c.querySelector('.ashby-application-form-question-title,[class*="_label_"],[class*="_heading_"],label,legend');
    if(t){const cl=t.cloneNode(true);cl.querySelectorAll('input,select,textarea,svg,button').forEach(n=>n.remove());q=(cl.innerText||'').replace(/\s+/g,' ').trim();}}
  return q;
})"""


def _open_comboboxes(page, fields: list) -> None:
    """Fill in options for combobox fields (flagged _combo) by opening each widget."""
    combo_labels = {(f.get("label") or "").strip() for f in fields if f.get("_combo")}
    if not combo_labels:
        for f in fields:
            f.pop("_combo", None)
        return
    try:
        labels = page.evaluate(_COMBO_OPEN_JS)
        boxes = page.query_selector_all(
            "input[role='combobox'], .ashby-application-form-input-autocomplete")
    except Exception:
        boxes, labels = [], []
    by_label: dict[str, list] = {}
    for i, box in enumerate(boxes):
        lab = (labels[i] if i < len(labels) else "").strip()
        if not lab or lab not in combo_labels or lab in by_label:
            continue
        opts: list[str] = []
        try:
            box.click()
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(500)
            for o in page.query_selector_all("[role='listbox'] [role='option'], [role='option']")[:50]:
                t = (o.inner_text() or "").strip()
                if t and t not in opts:
                    opts.append(t)
            page.keyboard.press("Escape")
            page.wait_for_timeout(120)
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        by_label[lab] = opts
    for f in fields:
        if f.pop("_combo", None) and by_label.get((f.get("label") or "").strip()):
            f["options"] = by_label[(f.get("label") or "").strip()]


def _scrape(page, url: str) -> list:
    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
    except Exception:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            return []
    page.wait_for_timeout(2200)
    try:
        fields = page.evaluate(_JS)
    except Exception:
        return []
    try:
        _open_comboboxes(page, fields)
    except Exception:
        for f in fields:
            f.pop("_combo", None)
    return fields


def scrape_one(ats: str, url: str) -> list:
    """Scrape ONE apply page's questions (single Playwright load) -> job_catalog rows.
    Used for on-demand re-scrape so a clicked job's questions are complete/current
    before the draft is generated (custom ATS whose questions aren't from an API)."""
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_context().new_page()
            fields = _scrape(page, _apply_url(ats, url))
            b.close()
    except Exception:
        return []
    return [{"label": f["label"][:300], "required": bool(f.get("required")),
             "type": f.get("type", ""),
             **({"options": f["options"]} if f.get("options") else {})}
            for f in fields if f.get("label")]


def run(ats_list=("ashby", "lever", "workable"), limit: int = 0,
        refresh_all: bool = False) -> int:
    catalog_db.ensure_schema()
    total = 0
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_context().new_page()
        for ats in ats_list:
            # refresh_all=True re-scrapes rows that already have questions, to add the
            # select/radio options we now capture (mirrors catalog_collector --refresh-gh).
            rows = catalog_db.rows_missing_questions(ats, missing_only=not refresh_all)
            if limit:
                rows = rows[:limit]
            print(f"{ats}: {len(rows)} rows to scrape", flush=True)
            got = 0
            for i, r in enumerate(rows, 1):
                fields = _scrape(page, _apply_url(ats, r["url"]))
                if fields:
                    qs = [{"label": f["label"][:300], "required": bool(f.get("required")),
                           "type": f.get("type", ""),
                           **({"options": f["options"]} if f.get("options") else {})}
                          for f in fields]
                    catalog_db.set_questions(ats, r["company_key"], r["external_id"], qs)
                    got += 1
                    total += 1
                if i % 10 == 0:
                    print(f"  {ats} {i}/{len(rows)} (got {got})", flush=True)
            print(f"{ats} done: {got}/{len(rows)} got questions", flush=True)
        b.close()
    print(f"DONE forms scrape: +{total}", flush=True)
    print("catalog counts ->", catalog_db.counts(), flush=True)
    return total


if __name__ == "__main__":
    import argparse

    _KNOWN = ("ashby", "lever", "workable")
    ap = argparse.ArgumentParser(
        description="Scrape ATS apply-form questions into job_catalog.questions.")
    ap.add_argument("ats", nargs="*", choices=_KNOWN, default=list(_KNOWN),
                     help="ATS(es) to scrape (default: all three)")
    ap.add_argument("--limit", type=int, default=0,
                     help="cap rows scraped per ATS (bounded/cron runs). Default 0 = "
                          "UNBOUNDED: scrapes ALL missing-question rows for the "
                          "selected ATS(es), one page load each")
    ap.add_argument("--refresh", action="store_true",
                     help="re-scrape ALL rows (not just missing) to add options to "
                          "already-collected questions")
    args = ap.parse_args()
    run(ats_list=tuple(args.ats) or _KNOWN, limit=args.limit, refresh_all=args.refresh)
