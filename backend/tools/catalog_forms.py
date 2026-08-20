"""Scrape application-form questions for ashby/lever catalog rows via Playwright.

Their public APIs don't expose the apply-form questions (greenhouse does), so we
render the apply page and read the field-level questions: text/select/textarea/file
controls by their label, and radio/checkbox groups by their fieldset legend. Writes
into job_catalog.questions via catalog_db.set_questions.

    python -m backend.tools.catalog_forms            # ashby + lever, all missing
    python -m backend.tools.catalog_forms ashby      # one ATS
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from backend.tools import catalog_db

_JS = r"""()=>{
  const seen=new Set(), out=[];
  const lf=(el)=>{
    if(el.id){const l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]');if(l)return l.innerText.trim();}
    const wl=el.closest('label'); if(wl)return wl.innerText.trim();
    if(el.getAttribute('aria-label'))return el.getAttribute('aria-label').trim();
    return '';
  };
  const groups={};
  document.querySelectorAll('input[type=radio],input[type=checkbox]').forEach(el=>{
    const n=el.name||''; if(!n)return;
    if(!(n in groups)){const fs=el.closest('fieldset');let q='';if(fs){const lg=fs.querySelector('legend');if(lg)q=lg.innerText.trim();}groups[n]=q;}
  });
  Object.values(groups).forEach(q=>{if(q&&!seen.has(q)){seen.add(q);out.push({label:q,type:'choice',required:false});}});
  document.querySelectorAll('input:not([type=radio]):not([type=checkbox]):not([type=hidden]):not([type=submit]):not([type=button]),select,textarea').forEach(el=>{
    const q=lf(el); const tag=el.tagName.toLowerCase();
    const t = tag==='select'?'select':(tag==='textarea'?'textarea':(el.type||'text'));
    if(q && !seen.has(q)){seen.add(q); out.push({label:q, type:t, required:!!(el.required||el.getAttribute('aria-required')==='true')});}
  });
  return out;
}"""


def _apply_url(ats: str, url: str) -> str:
    u = (url or "").split("?")[0].rstrip("/")
    if ats == "ashby" and not u.endswith("/application"):
        u += "/application"
    if ats == "lever" and not u.endswith("/apply"):
        u += "/apply"
    return u


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
        return page.evaluate(_JS)
    except Exception:
        return []


def run(ats_list=("ashby", "lever"), limit: int = 0) -> int:
    catalog_db.ensure_schema()
    total = 0
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_context().new_page()
        for ats in ats_list:
            rows = catalog_db.rows_missing_questions(ats)
            if limit:
                rows = rows[:limit]
            print(f"{ats}: {len(rows)} rows to scrape", flush=True)
            got = 0
            for i, r in enumerate(rows, 1):
                fields = _scrape(page, _apply_url(ats, r["url"]))
                if fields:
                    qs = [{"label": f["label"][:300], "required": bool(f.get("required")),
                           "type": f.get("type", "")} for f in fields]
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
    ats = tuple(a for a in sys.argv[1:] if a in ("ashby", "lever")) or ("ashby", "lever")
    run(ats_list=ats)
