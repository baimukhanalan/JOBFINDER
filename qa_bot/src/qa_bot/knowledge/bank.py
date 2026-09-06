import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, replace
from difflib import SequenceMatcher
from qa_bot.domain.answer import AnswerProposal, Selection
from qa_bot.domain.question import ResponseKind
from qa_bot.execution.validator import validate_answer


def normalize(text):
    # Preserve case, punctuation, units, digits, option order and negation.
    return " ".join(unicodedata.normalize("NFC", text).split())


def fingerprint(q):
    media = []
    for a in q.assets:
        if not a.sha256 or not re.fullmatch(r"[0-9a-f]{64}", a.sha256):
            raise ValueError("media bytes must be hashed before exact reuse")
        media.append((a.id, a.media_type, a.sha256))
    content = {
        "version": 1, "section": normalize(q.section), "type_id": q.type_id,
        "instruction": normalize(q.instruction), "text": normalize(q.question_text),
        "context": [normalize(t) for t in q.context],
        "options": [(o.id, o.position, normalize(o.label), o.image_ref) for o in q.options],
        "tables": [asdict(t) for t in q.tables], "media": media,
        "response": asdict(q.response_contract), "constraints": [asdict(c) for c in q.field_constraints],
        "interaction": q.interaction_contract,
    }
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest(), canonical


def decode_answer(data):
    data = json.loads(data)
    data["kind"] = ResponseKind(data["kind"])
    data["selections"] = tuple(Selection(**s) for s in data["selections"])
    data["evidence"] = tuple(data["evidence"])
    return AnswerProposal(**data)


class QuestionBank:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS answers(
                key TEXT PRIMARY KEY, canonical TEXT NOT NULL, answer TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('candidate','approved')),
                reviewer TEXT, reason TEXT);
            CREATE TABLE IF NOT EXISTS corpus(
                id TEXT PRIMARY KEY, content TEXT NOT NULL, candidate TEXT,
                status TEXT NOT NULL CHECK(status='candidate'));
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def save_candidate(self, q, answer):
        errors = validate_answer(q, answer)
        if errors:
            raise ValueError(",".join(errors))
        key, canonical = fingerprint(q)
        payload = json.dumps(asdict(answer), ensure_ascii=False)
        old = self.db.execute("SELECT canonical,status FROM answers WHERE key=?", (key,)).fetchone()
        if old and (old[0] != canonical or old[1] == "approved"):
            raise ValueError("collision or protected approved answer")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO answers VALUES(?,?,?,'candidate',NULL,NULL)",
                            (key, canonical, payload))

    def approve(self, q, *, reviewer, reason):
        if not reviewer.strip() or not reason.strip():
            raise ValueError("human reviewer and justification required")
        key, canonical = fingerprint(q)
        row = self.db.execute("SELECT canonical,answer FROM answers WHERE key=?", (key,)).fetchone()
        if not row or row[0] != canonical:
            raise ValueError("candidate missing or collision")
        a = replace(decode_answer(row[1]), question_id=q.question_id, content_hash=q.content_hash)
        if validate_answer(q, a):
            raise ValueError("candidate no longer valid")
        with self.db:
            self.db.execute("UPDATE answers SET status='approved',reviewer=?,reason=? WHERE key=?",
                            (reviewer, reason, key))

    def lookup(self, q):
        key, canonical = fingerprint(q)
        row = self.db.execute("SELECT canonical,answer FROM answers WHERE key=? AND status='approved'",
                              (key,)).fetchone()
        if not row or row[0] != canonical:
            return None
        a = replace(decode_answer(row[1]), question_id=q.question_id, content_hash=q.content_hash)
        return None if validate_answer(q, a) else a

    def similar(self, q, limit=5):
        _, canonical = fingerprint(q)
        rows = self.db.execute("SELECT key,canonical,status FROM answers").fetchall()
        return sorted(((SequenceMatcher(None, canonical, c).ratio(), k, s)
                       for k, c, s in rows), reverse=True)[:limit]

    def corpus_candidates(self, text, limit=5):
        """Review suggestions only; source corpus cannot become an approved hit."""
        target = normalize(text)
        ranked = []
        for identity, raw, candidate in self.db.execute("SELECT id,content,candidate FROM corpus"):
            record = json.loads(raw)
            score = SequenceMatcher(None, target, normalize(record.get("text", ""))).ratio()
            ranked.append({"id": identity, "similarity": score, "status": "candidate",
                           "recommended_answer": candidate})
        return sorted(ranked, key=lambda r: r["similarity"], reverse=True)[:limit]

    def import_corpus(self, records, recommendations):
        count = 0
        with self.db:
            for record in records:
                # Raw source stays quarantined: no guessed QuestionSpec/approved promotion.
                identity = record["id"]
                content = json.dumps(record, ensure_ascii=False, sort_keys=True)
                candidate = recommendations.get(identity)
                old = self.db.execute("SELECT content,candidate FROM corpus WHERE id=?", (identity,)).fetchone()
                if old and old != (content, candidate):
                    raise ValueError("source ID conflict")
                self.db.execute("INSERT OR IGNORE INTO corpus VALUES(?,?,?,'candidate')",
                                (identity, content, candidate))
                count += 1
        return count
