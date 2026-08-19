"""SQLite cache of drafted answers, keyed by (profile, niche, normalized question).

Repetitive application questions recur across forms, so answers are cached and the
LLM is hit only for genuinely new questions. The key includes the applying PERSON and
the résumé NICHE: different people (and variants stating e.g. different years totals)
must never share an answer — five applicants submitting word-for-word identical texts
is exactly the volume problem this prevents. The company name is genericized to <co>
inside stored answers so one person's answer is reusable across companies.
"""
import os
import re
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "uploads", "answer_cache.db")
DB_PATH = os.path.abspath(DB_PATH)
_lock = threading.Lock()
_migrated_paths: set[str] = set()


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    if DB_PATH not in _migrated_paths:
        c.execute("PRAGMA journal_mode=WAL")  # concurrent readers across processes
        c.execute("DROP TABLE IF EXISTS answers")  # legacy schema (no profile column); it's a cache
        c.execute("""CREATE TABLE IF NOT EXISTS answers_v2 (
            profile TEXT NOT NULL,
            niche TEXT NOT NULL DEFAULT '',
            key TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (profile, niche, key)
        )""")
        _migrated_paths.add(DB_PATH)
    return c


def normalize(question: str, company: str = "") -> str:
    q = (question or "").lower().strip()
    if company:
        q = re.sub(r"\b" + re.escape(company.lower().strip()) + r"\b", "<co>", q)
    # blank out common company-name leftovers like "at <Name>" / "join <Name>"
    q = re.sub(r"\b(at|join|with|for)\s+[a-z0-9][\w.&'-]+(\s+[a-z0-9][\w.&'-]+)?\b", r"\1 <co>", q)
    q = re.sub(r"[^a-z0-9<>]+", " ", q)        # drop punctuation
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _genericize(answer: str, company: str) -> str:
    """Replace the company name in a stored answer with <co> so the answer is
    reusable across companies ("I want to work at Acme" must never be served
    for a Zapier application)."""
    if not company:
        return answer
    return re.sub(re.escape(company.strip()), "<co>", answer, flags=re.IGNORECASE)


def _personalize(answer: str, company: str) -> str:
    return answer.replace("<co>", company.strip() if company else "your company")


def get_many(questions: list[str], company: str = "", *,
             profile: str = "default", niche: str = "") -> dict:
    """Return {question: answer} for this person's cached questions (bumps hit count)."""
    out = {}
    with _lock, _conn() as c:
        for q in questions:
            k = normalize(q, company)
            if not k:
                continue
            row = c.execute("SELECT answer FROM answers_v2 WHERE profile=? AND niche=? AND key=?",
                            (profile, niche, k)).fetchone()
            if row:
                out[q] = _personalize(row[0], company)
                c.execute("UPDATE answers_v2 SET hits=hits+1, updated_at=datetime('now') "
                          "WHERE profile=? AND niche=? AND key=?", (profile, niche, k))
    return out


def put_many(qa: dict, company: str = "", *,
             profile: str = "default", niche: str = "") -> None:
    """Store {question: answer} pairs (company name genericized to <co>)."""
    with _lock, _conn() as c:
        for q, a in qa.items():
            k = normalize(q, company)
            if not k or not a:
                continue
            c.execute(
                "INSERT INTO answers_v2(profile, niche, key, question, answer) VALUES(?,?,?,?,?) "
                "ON CONFLICT(profile, niche, key) DO UPDATE SET answer=excluded.answer, "
                "updated_at=datetime('now') WHERE excluded.answer != answers_v2.answer",
                (profile, niche, k, q, _genericize(a, company)))


def stats() -> dict:
    with _lock, _conn() as c:
        n = c.execute("SELECT COUNT(*), COALESCE(SUM(hits),0) FROM answers_v2").fetchone()
    return {"cached_questions": n[0], "cache_hits_served": n[1]}
