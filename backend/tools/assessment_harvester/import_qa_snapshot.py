"""Import the qa_bot snapshot's ANSWERED questions into the unified assessment bank, so the harvester
REPLAYS the correct answer instead of guessing/random. This is the corpus-side counterpart of
`answer_key.py` (which solves live): here the answers are the qa_bot snapshot's already-derived ones.

Source (gitignored, copied from the qa_bot snapshot into `backend/data/qa_snapshot/`):
  - `questions.jsonl`     — 1251 SHL questions (id, section, prompt, text, options; `answer_key` null)
  - `SHL_answers_all.csv` — the derived answer per id (recommended_answer, reasoning, confidence)

Join key = the question `id`. Everything is banked under platform 'amcat' (the AMCAT/TP battery the
snapshot came from) with the SAME dedup_key the live harvester uses
(`norm(question) || sorted-norm(options) || msig`), msig="" (the snapshot carries no CDN image URLs) —
so an imported item UPGRADES the matching live-harvested item in place, or is net-new banked coverage.

Sections (owner-selected 2026-09-08; `calculator.py` deliberately skipped):
  * Sales Competency Test (120)      — best/worst SJT. answer_key = {kind:'best_worst', best, worst}.
  * Basic Analytical Ability (228)   — single-choice cognitive (mostly high-confidence).
  * WriteX - Email Writing (6)       — free-response email; the historical email is stored so
                                       `writex.draft_email` replays it verbatim.

Idempotent: bank.record dedups on the key; the answer_key is (re)written with the authoritative
`qa_bot_csv` answer. CLI:
    python -m backend.tools.assessment_harvester.import_qa_snapshot                       # all three
    python -m backend.tools.assessment_harvester.import_qa_snapshot --sections sales,analytical
    python -m backend.tools.assessment_harvester.import_qa_snapshot --dry-run
"""
from __future__ import annotations

import csv
import json
import os
import re

from backend.tools.assessment_harvester import bank

_HERE = os.path.dirname(__file__)
_DEFAULT_DIR = os.path.join(_HERE, "..", "..", "data", "qa_snapshot")
_ANSWER_SOURCE = "qa_bot_csv"

_SECTION = {
    "sales": "Sales Competency Test",
    "analytical": "Basic Analytical Ability",
    "writex": "WriteX - Email Writing",
}
_BEST_WORST_RE = re.compile(r"BEST:\s*(.*?)\s*WORST:\s*(.*)$", re.S)


def _norm(s: str) -> str:
    return bank.norm(s)


def opts_of(q: dict) -> list[str]:
    """Option label strings, in source order (options may be plain strings or {label|text} objects)."""
    out = []
    for o in q.get("options") or []:
        if isinstance(o, str):
            out.append(o)
        elif isinstance(o, dict):
            out.append(o.get("label") or o.get("text") or "")
    return [x for x in out if (x or "").strip()]


def load_snapshot(data_dir: str | None = None) -> tuple[list[dict], dict]:
    d = data_dir or _DEFAULT_DIR
    qs = [json.loads(l) for l in open(os.path.join(d, "questions.jsonl"), encoding="utf-8") if l.strip()]
    with open(os.path.join(d, "SHL_answers_all.csv"), newline="", encoding="utf-8-sig") as f:
        rows = {r["id"]: r for r in csv.DictReader(f)}
    return qs, rows


def _index_of(options: list[str], want: str) -> int | None:
    w = _norm(want)
    if not w:
        return None
    for i, o in enumerate(options):
        if _norm(o) == w:
            return i
    return None


def map_single_answer(recommended: str, options: list[str]) -> tuple[int | None, str | None]:
    """Map a single-choice recommended answer to (index, option_text). Exact normalized match first,
    then an unambiguous substring match (the answer text contains exactly one option)."""
    idx = _index_of(options, recommended)
    if idx is not None:
        return idx, options[idx]
    r = _norm(recommended)
    hits = [i for i, o in enumerate(options) if _norm(o) and _norm(o) in r]
    if len(hits) == 1:
        return hits[0], options[hits[0]]
    return None, None


def _analytical_type(options: list[str]) -> str:
    numeric = sum(1 for o in options if re.fullmatch(r"[\d.,%$£€+\-*/: ]{1,16}", o.strip()))
    if options and numeric >= max(2, len(options) - 1):
        return "numerical"
    return "unknown"


def _image_dependent(q: dict) -> bool:
    blob = ((q.get("prompt") or "") + " " + (q.get("text") or "")).lower()
    if q.get("passage") or q.get("screenshots"):
        return True
    return bool(re.search(r"click on the image|refer to (the |given )?(image|diagram|figure|graph|chart|table)"
                          r"|as shown|in the (image|diagram|figure|graph|chart)", blob))


def _upsert(platform: str, question: str, options: list[str], item_type: str, is_ability: bool,
            chosen: dict, answer_key: dict, dry: bool) -> str:
    """Bank (or find) the item, then write its answer_key. Returns 'upgraded' | 'new'."""
    key = bank.dedup_key(platform, question, options, "")
    existed = key in bank._load().get("items", {})
    if dry:
        return "upgraded" if existed else "new"
    if not existed:
        bank.record(platform=platform, item_type=item_type, question=question,
                    options=[{"text": o, "image": None, "audio": None} for o in options],
                    chosen_answer=chosen, source={"mailbox": None, "invite_url": None},
                    kind_meta={"is_scored": True, "is_ability": is_ability}, msig="")
    bank.set_answer(platform, question, options, answer_key, "")
    return "upgraded" if existed else "new"


def import_analytical(qs, rows, *, platform="amcat", dry=False) -> dict:
    st = {"total": 0, "new": 0, "upgraded": 0, "skipped_no_options": 0, "skipped_unmapped": 0}
    for q in qs:
        if q.get("section") != _SECTION["analytical"]:
            continue
        st["total"] += 1
        options = opts_of(q)
        if len(options) < 2:
            st["skipped_no_options"] += 1        # image-only/graphic options — no text to map
            continue
        c = rows.get(q["id"], {})
        idx, text = map_single_answer((c.get("recommended_answer") or "").strip(), options)
        if idx is None:
            st["skipped_unmapped"] += 1
            continue
        question = (q.get("text") or "").strip()
        ak = {"text": text, "index": idx, "source": _ANSWER_SOURCE, "needs_vision": False,
              "confidence": (c.get("confidence") or "").strip(),
              "reasoning": (c.get("reasoning") or "").strip()[:600],
              "image_dependent": _image_dependent(q), "src_id": q["id"], "ts": bank._now()}
        chosen = {"text": text, "index": idx, "value": None, "source": _ANSWER_SOURCE}
        st[_upsert(platform, question, options, _analytical_type(options), True, chosen, ak, dry)] += 1
    return st


def import_sales(qs, rows, *, platform="amcat", dry=False) -> dict:
    st = {"total": 0, "new": 0, "upgraded": 0, "skipped_unmapped": 0}
    for q in qs:
        if q.get("section") != _SECTION["sales"]:
            continue
        st["total"] += 1
        options = opts_of(q)
        c = rows.get(q["id"], {})
        m = _BEST_WORST_RE.search((c.get("recommended_answer") or "").strip())
        if not m or len(options) < 2:
            st["skipped_unmapped"] += 1
            continue
        bi, btext = map_single_answer(m.group(1).strip(), options)
        wi, wtext = map_single_answer(m.group(2).strip(), options)
        if bi is None or wi is None or bi == wi:
            st["skipped_unmapped"] += 1
            continue
        question = (q.get("text") or "").strip()
        ak = {"kind": "best_worst",
              "text": btext, "index": bi,                       # single-index replay defaults to BEST
              "best": {"text": btext, "index": bi},
              "worst": {"text": wtext, "index": wi},
              "source": _ANSWER_SOURCE, "needs_vision": False,
              "confidence": (c.get("confidence") or "").strip(),
              "reasoning": (c.get("reasoning") or "").strip()[:600],
              "src_id": q["id"], "ts": bank._now()}
        chosen = {"text": btext, "index": bi, "value": f"worst={wtext}", "source": _ANSWER_SOURCE}
        st[_upsert(platform, question, options, "sales", False, chosen, ak, dry)] += 1
    return st


def import_writex(qs, rows, *, platform="amcat", dry=False) -> dict:
    """WriteX has no MCQ options — bank the topic as a 'writing' item and store the historical email as
    its answer_key (kind='email') so `writex.draft_email` replays it verbatim on a recurrence."""
    from backend.tools.assessment_harvester import writex as _wx
    st = {"total": 0, "new": 0, "upgraded": 0, "skipped_unmapped": 0}
    for q in qs:
        if q.get("section") != _SECTION["writex"]:
            continue
        st["total"] += 1
        c = rows.get(q["id"], {})
        answer = (c.get("recommended_answer") or "").strip()
        parsed = _wx.parse_email(answer)
        if not parsed:
            st["skipped_unmapped"] += 1
            continue
        question = (q.get("text") or "").strip()      # the topic (WriteX has no separate options)
        ak = {"kind": "email", "text": answer,
              "to": parsed["to"], "subject": parsed["subject"], "body": parsed["body"],
              "source": _ANSWER_SOURCE, "needs_vision": False,
              "confidence": (c.get("confidence") or "").strip(), "src_id": q["id"], "ts": bank._now()}
        chosen = {"text": None, "index": None, "value": "email_example", "source": _ANSWER_SOURCE}
        st[_upsert(platform, question, [], "writing", False, chosen, ak, dry)] += 1
    return st


def run(sections=("sales", "analytical", "writex"), *, data_dir=None, platform="amcat", dry=False) -> dict:
    qs, rows = load_snapshot(data_dir)
    out = {}
    if "analytical" in sections:
        out["analytical"] = import_analytical(qs, rows, platform=platform, dry=dry)
    if "sales" in sections:
        out["sales"] = import_sales(qs, rows, platform=platform, dry=dry)
    if "writex" in sections:
        out["writex"] = import_writex(qs, rows, platform=platform, dry=dry)
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sections", default="sales,analytical,writex",
                    help="comma list of: sales, analytical, writex")
    ap.add_argument("--data-dir", default=None, help="override backend/data/qa_snapshot")
    ap.add_argument("--platform", default="amcat")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    secs = tuple(s.strip() for s in args.sections.split(",") if s.strip())
    before = bank.size()
    stats = run(secs, data_dir=args.data_dir, platform=args.platform, dry=args.dry_run)
    print(("DRY-RUN " if args.dry_run else "") + "IMPORT:", json.dumps(stats, ensure_ascii=False, indent=2))
    if not args.dry_run:
        print(f"bank size {before} -> {bank.size()}")


if __name__ == "__main__":
    main()
