"""Compute + persist a CORRECT-answer key over the banked MCQ questions (`assessment_bank.json`), so a
recurrence can REPLAY the stored answer instantly instead of re-deciding — the whole point of harvesting
the questions once. Mirrors the SHL etalon's solve-once / replay-on-recurrence answer bank.

The LOCAL model (`settings.llm_*`, Sumrak) answers: knowledge / numerical / verbal / logical reasoning ->
the CORRECT option; personality / situational-judgement -> the answer of a competent professional. The
local model is TEXT-ONLY (verified — it 422s on image content), so image-dependent items (diagram/table
math, ~5% of the bank) get a best-effort TEXT guess flagged `needs_vision=True` (only a vision model or
OCR would make those reliable). Everything text-answerable (~95%) is solved from the question + options.

Each answered item gets `item["answer_key"] = {text, index, source, needs_vision, ts}`. Run:
    python -m backend.tools.assessment_harvester.answer_key            # fill missing
    python -m backend.tools.assessment_harvester.answer_key --redo     # recompute all
    python -m backend.tools.assessment_harvester.answer_key --platform amcat
"""
from __future__ import annotations

import asyncio
import re

from backend.config import settings

# Free-response families carry no MCQ answer key (typing = type the text, speaking = read it, listening
# audio is captured, video = fake camera).
_FREE = {"speaking", "listening", "typing", "video"}

_SYS = (
    "You are a strong candidate taking a job assessment. "
    "For any knowledge, numerical, verbal, logical or data-interpretation question, work out and pick the "
    "CORRECT answer. For personality or situational-judgement questions, answer as a competent, reliable, "
    "professional, customer-focused person. Reply with ONLY the number of the single best option.")


def _has_image(item: dict) -> bool:
    if (item.get("media") or {}).get("image"):
        return True
    return any(o.get("image") for o in (item.get("options") or []))


async def _pick(client, question: str, options: list[str]) -> int | None:
    """Ask the local model for the best option; returns a 0-based index or None on any failure."""
    numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    prompt = (f"{_SYS}\n\nQuestion: {question or '(choose the single best option)'}\n\n"
              f"Options:\n{numbered}\n\nBest option number:")
    try:
        r = await client.post(
            f"{settings.llm_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_key}", "Content-Type": "application/json"},
            json={"model": settings.llm_model,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.0, "max_tokens": 8, "stream": False})
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None
    m = re.search(r"\d+", txt or "")
    if not m:
        return None
    idx = int(m.group()) - 1
    return idx if 0 <= idx < len(options) else None


async def compute_all(concurrency: int = 4, redo: bool = False, platform: str | None = None,
                      progress=None) -> dict:
    """Fill `answer_key` for every MCQ item (>=2 text options) that lacks one (or all, with redo).
    Returns stats. Persists the bank once at the end."""
    import httpx
    from backend.tools.assessment_harvester import bank

    items = bank._load().get("items", {})
    todo = []
    for k, e in items.items():
        if e.get("item_type") in _FREE:
            continue
        if platform and e.get("platform") != platform:
            continue
        opts = [o.get("text", "") for o in (e.get("options") or []) if (o.get("text") or "").strip()]
        if len(opts) < 2:
            continue
        if e.get("answer_key") and not redo:
            continue
        todo.append((e, opts))

    stats = {"total": len(todo), "solved": 0, "image_lowconf": 0, "failed": 0}
    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async with httpx.AsyncClient(timeout=60) as client:
        async def one(e, opts):
            nonlocal done
            async with sem:
                idx = await _pick(client, e.get("question", ""), opts)
            img = _has_image(e)
            if idx is None:
                stats["failed"] += 1
            else:
                e["answer_key"] = {
                    "text": opts[idx], "index": idx,
                    "source": "local_textonly_guess" if img else "local_llm",
                    "needs_vision": img, "ts": bank._now()}
                stats["solved"] += 1
                if img:
                    stats["image_lowconf"] += 1
            done += 1
            if progress and done % 25 == 0:
                progress(done, stats["total"], stats)

        await asyncio.gather(*(one(e, opts) for e, opts in todo))

    bank._save()
    return stats


def coverage() -> dict:
    """How many MCQ items have an answer key, split by needs_vision."""
    from backend.tools.assessment_harvester import bank
    items = bank._load().get("items", {})
    out = {"mcq": 0, "keyed": 0, "keyed_text": 0, "keyed_image_lowconf": 0, "unkeyed": 0}
    for e in items.values():
        if e.get("item_type") in _FREE:
            continue
        opts = [o for o in (e.get("options") or []) if (o.get("text") or "").strip()]
        if len(opts) < 2:
            continue
        out["mcq"] += 1
        ak = e.get("answer_key")
        if ak:
            out["keyed"] += 1
            out["keyed_image_lowconf" if ak.get("needs_vision") else "keyed_text"] += 1
        else:
            out["unkeyed"] += 1
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--redo", action="store_true", help="recompute every answer, not just missing")
    ap.add_argument("--platform", default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--coverage", action="store_true", help="print coverage and exit")
    args = ap.parse_args()
    if args.coverage:
        print("coverage:", coverage())
        return

    def prog(d, t, s):
        print(f"  {d}/{t} solved={s['solved']} img_lowconf={s['image_lowconf']} failed={s['failed']}",
              flush=True)

    stats = asyncio.run(compute_all(concurrency=args.concurrency, redo=args.redo,
                                    platform=args.platform, progress=prog))
    print("ANSWER KEY DONE:", stats)
    print("coverage:", coverage())


if __name__ == "__main__":
    main()
