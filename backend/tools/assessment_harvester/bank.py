"""Unified, platform-scoped assessment question bank.

One gitignored JSON at `backend/data/assessment_bank.json`, versioned envelope
`{schema_version: 2, items: {<dedup_key>: entry}}`. Mirrors the atomic-write / lazy-cache pattern of
`shl_assessment._bank_save` (pid-suffixed tmp -> os.replace, best-effort try/except, module cache).

Entry shape (see `record`):
    platform, item_type, question, options[]={text,image,audio}, media={image,audio,prompt_text},
    chosen_answer={text,index,value,source}, source={mailbox,invite_url},
    kind_meta={is_scored,is_ability}, hits, created_ts, updated_ts

Dedup key = f"{platform}::{norm(question)}||{'|'.join(sorted(norm(o) for o in option_texts))}||{media_sig}"
where media_sig is a caller-supplied stable signature of the question's image(s) ("" for text items).
Keeps the SHL bank's order-independent / case-insensitive property, adds platform isolation and
picture-question disambiguation.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json as _json
import os as _os
import re as _re

SCHEMA_VERSION = 2
_BANK_PATH = _os.path.join(_os.path.dirname(__file__), "..", "..", "data", "assessment_bank.json")
_BANK: dict | None = None


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm(s: str) -> str:
    return _re.sub(r"\s+", " ", (s or "").strip().lower())


def media_sig(img_srcs: list[str] | None) -> str:
    """Stable signature of an item's question image(s). Uses the image SRC/URLs (item-stable on a
    CDN), not a screenshot (which shifts per session). '' when the item is text-only."""
    srcs = sorted({(s or "").strip() for s in (img_srcs or []) if (s or "").strip()})
    if not srcs:
        return ""
    return hashlib.sha1("\n".join(srcs).encode("utf-8", "ignore")).hexdigest()[:16]


def dedup_key(platform: str, question: str, option_texts: list[str], msig: str = "") -> str:
    opts = "|".join(sorted(norm(o) for o in (option_texts or []) if o and o.strip()))
    return f"{platform}::{norm(question)}||{opts}||{msig or ''}"


def _load() -> dict:
    global _BANK
    if _BANK is None:
        try:
            with open(_BANK_PATH, encoding="utf-8") as f:
                data = _json.load(f)
            if not isinstance(data, dict) or "items" not in data:
                data = {"schema_version": SCHEMA_VERSION, "items": {}}
        except Exception:
            data = {"schema_version": SCHEMA_VERSION, "items": {}}
        _BANK = data
    return _BANK


def _save() -> None:
    bank = _load()
    try:
        _os.makedirs(_os.path.dirname(_BANK_PATH), exist_ok=True)
        tmp = f"{_BANK_PATH}.{_os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(bank, f, ensure_ascii=False)
        _os.replace(tmp, _BANK_PATH)
    except Exception:
        pass


def record(*, platform: str, item_type: str, question: str,
           options: list[dict] | None = None, media: dict | None = None,
           chosen_answer: dict | None = None, source: dict | None = None,
           kind_meta: dict | None = None, msig: str = "") -> str:
    """Upsert one harvested item. `options` is an ORDERED list of {text,image,audio}. Returns the
    dedup key. Idempotent on the key: a recurrence bumps `hits` and refreshes `updated_ts` (keeping
    the first-seen `created_ts` and the original chosen answer/source)."""
    options = options or []
    opt_texts = [o.get("text", "") for o in options]
    key = dedup_key(platform, question, opt_texts, msig)
    bank = _load()
    items = bank.setdefault("items", {})
    prev = items.get(key)
    if prev:
        prev["hits"] = int(prev.get("hits", 1)) + 1
        prev["updated_ts"] = _now()
        _save()
        return key
    items[key] = {
        "platform": platform,
        "item_type": item_type,
        "question": (question or "")[:2000],
        "options": [{"text": (o.get("text") or "")[:500],
                     "image": o.get("image"), "audio": o.get("audio")} for o in options],
        "media": media or {"image": None, "audio": None, "prompt_text": None},
        "chosen_answer": chosen_answer or {"text": None, "index": None, "value": None, "source": None},
        "source": source or {"mailbox": None, "invite_url": None},
        "kind_meta": kind_meta or {"is_scored": True, "is_ability": False},
        "hits": 1,
        "created_ts": _now(),
        "updated_ts": _now(),
    }
    _save()
    return key


def set_answer(platform: str, question: str, option_texts: list[str], answer_key: dict,
               msig: str = "") -> bool:
    """Store/overwrite the `answer_key` on a banked item (for a freshly live-solved question so it
    replays next time). Returns True if the item existed. Persists."""
    e = _load().get("items", {}).get(dedup_key(platform, question, option_texts, msig))
    if not e:
        return False
    e["answer_key"] = answer_key
    _save()
    return True


def answer_for(platform: str, question: str, option_texts: list[str], msig: str = "") -> dict | None:
    """Return the stored `answer_key` ({text,index,source,needs_vision}) for a banked MCQ, or None.
    Used to REPLAY the pre-computed correct answer on a recurrence instead of guessing/random."""
    e = _load().get("items", {}).get(dedup_key(platform, question, option_texts, msig))
    return (e or {}).get("answer_key")


def size() -> int:
    return len(_load().get("items", {}))


def counts_by(field: str = "item_type") -> dict:
    out: dict = {}
    for e in _load().get("items", {}).values():
        k = e.get(field) if field in ("item_type", "platform") else e.get("kind_meta", {}).get(field)
        out[k] = out.get(k, 0) + 1
    return out


def reload() -> None:
    """Drop the module cache (tests / long-running processes)."""
    global _BANK
    _BANK = None


_SHL_BANK_PATH = _os.path.join(_os.path.dirname(__file__), "..", "..", "data", "shl_answer_bank.json")
_ABILITY_KINDS = {"numerical", "verbal", "logical", "picture", "ability"}


def migrate_from_shl(shl_path: str | None = None) -> int:
    """One-time import of the existing SHL etalon bank (`shl_answer_bank.json`) into the unified bank
    under platform 'shl'. Each SHL entry {q, options[], answer, kind, n} -> a unified item. Idempotent
    (re-keys on the unified dedup key, so re-running won't duplicate). Returns items imported."""
    path = shl_path or _SHL_BANK_PATH
    try:
        with open(path, encoding="utf-8") as f:
            shl = _json.load(f)
    except Exception:
        return 0
    imported = 0
    for key, entry in shl.items():
        q = entry.get("q", "")
        ans = entry.get("answer", "")
        kind = entry.get("kind", "unknown")
        opts = entry.get("options") or []
        # older SHL entries carry no `options` list — but the SHL key encodes them: it is
        # f"{norm(q)}||{'|'.join(sorted(norm(o)))}". Reconstruct the (normalized) option texts from
        # the key so each distinct item stays distinct instead of collapsing on shared question text.
        if not opts and isinstance(key, str) and "||" in key:
            seg = key.split("||", 1)[1]
            if seg:
                opts = [s for s in seg.split("|") if s]
        options = [{"text": str(o)[:500], "image": None, "audio": None} for o in opts]
        idx = next((i for i, o in enumerate(opts) if norm(str(o)) == norm(ans)), None)
        record(platform="shl", item_type=kind, question=q, options=options,
               chosen_answer={"text": ans, "index": idx, "value": None, "source": "bank_replay"},
               source={"mailbox": None, "invite_url": None},
               kind_meta={"is_scored": True, "is_ability": kind in _ABILITY_KINDS},
               media={"image": None, "audio": None, "prompt_text": None})
        imported += 1
    return imported
