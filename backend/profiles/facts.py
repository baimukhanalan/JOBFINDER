"""Per-person fact sheet: one JSON file per profile with screener-answering facts
(shifts, salary range, languages, tools, consents, ...). Single source of truth for
the deterministic rules in backend.applier.analyzer AND the LLM prompts in
backend.services.tailor.{choices,answers}.

Real people's files are gitignored; backend/data/facts/sample.json is a committed
fake that documents the schema. A missing/invalid file -> {} (the engine degrades
to pre-fact-sheet behavior, nothing crashes).
"""
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FACTS_DIR = PROJECT_ROOT / "backend" / "data" / "facts"

_SAFE_ID = re.compile(r"^[a-z0-9_-]+$")


def load_facts(profile_id: str) -> dict:
    """Facts for one person, {} when absent/invalid. Keys are flat, values are
    strings or lists of strings (see backend/data/facts/sample.json)."""
    if not profile_id or not _SAFE_ID.match(profile_id):
        return {}
    path = FACTS_DIR / f"{profile_id}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("facts %s unreadable (%s) — ignoring", path.name, e)
        return {}
    return data if isinstance(data, dict) else {}
