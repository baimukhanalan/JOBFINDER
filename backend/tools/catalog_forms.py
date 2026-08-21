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
    if(!(n in groups)){const fs=el.closest('fieldset');let q='';if(fs){const lg=fs.querySelector('legend');if(lg)q=lg.innerText.trim();}groups[n]={q:q,opts:[]};}
    const ol=lf(el); if(ol) groups[n].opts.push(ol);  // each radio's own label is a choice
  });
  Object.values(groups).forEach(g=>{if(g.q&&!seen.has(g.q)){seen.add(g.q);const it={label:g.q,type:'choice',required:false};if(g.opts.length)it.options=g.opts;out.push(it);}});
  document.querySelectorAll('input:not([type=radio]):not([type=checkbox]):not([type=hidden]):not([type=submit]):not([type=button]),select,textarea').forEach(el=>{
    const q=lf(el); const tag=el.tagName.toLowerCase();
    const t = tag==='select'?'select':(tag==='textarea'?'textarea':(el.type||'text'));
    if(q && !seen.has(q)){
      seen.add(q);
      const it={label:q, type:t, required:!!(el.required||el.getAttribute('aria-required')==='true')};
      if(tag==='select'){
        const opts=[...el.options].map(o=>(o.textContent||'').trim())
          .filter(x=>x && !/^(select|choose|--|please|pick)/i.test(x));
        if(opts.length) it.options=opts;  // dropdown <option> labels
      }
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
