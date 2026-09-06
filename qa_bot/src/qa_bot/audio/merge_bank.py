"""Merge explicitly selected prior speech banks without synthesis or audio conversion."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from qa_bot.audio.replay_bank import SpeechReplayBank, prompt_fingerprint, normalize_prompt
from qa_bot.audio.service import validate_mp3, wav_duration


class SpeechBankConflict(ValueError):
    pass


def _read_source(directory: Path):
    directory = directory.resolve()
    database = directory / "speech.sqlite3"
    if not database.is_file():
        raise FileNotFoundError("source speech bank is missing")
    # A read transaction gives a consistent snapshot even if a source is still running.
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        rows = [dict(r) for r in db.execute("SELECT * FROM speech_answers")]
        events = [dict(r) for r in db.execute("SELECT * FROM speech_usage ORDER BY id")]
    assets = {}
    root = (directory / "answers").resolve()
    for row in rows:
        key, canonical = prompt_fingerprint(row["question_text"])
        if key != row["question_key"] or canonical != row["canonical"]:
            raise SpeechBankConflict("source question fingerprint mismatch")
        if normalize_prompt(row["answer_text"]) != row["answer_text"]:
            raise SpeechBankConflict("source answer is not canonical")
        pair = []
        for path_name, hash_name in (("mp3_path", "audio_sha256"), ("wav_path", "wav_sha256")):
            path = (root / row[path_name]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise SpeechBankConflict("source artifact missing or outside bank")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != row[hash_name]:
                raise SpeechBankConflict("source artifact hash mismatch")
            pair.append(data)
        validate_mp3(pair[0])
        wav_duration(pair[1])
        assets[key] = tuple(pair)
    if any(e["question_key"] not in assets for e in events):
        raise SpeechBankConflict("source usage references a missing answer")
    return rows, events, assets


def merge_banks(destination: str | Path, sources) -> dict:
    """Validate all sources first, then merge atomically; never overwrite a conflict.

    Copied runs contain the same usage history. Preserve the maximum multiplicity
    of each exact event across copies, so repeated imports remain idempotent.
    """
    destination = Path(destination).resolve()
    selected = [Path(p).resolve() for p in sources]
    if not selected or destination in selected:
        raise ValueError("explicit separate source bank directories required")
    snapshots = [_read_source(p) for p in selected]
    destination.mkdir(parents=True, exist_ok=True)
    imported = existing = usage_added = 0
    with SpeechReplayBank(destination / "speech.sqlite3", destination / "answers") as bank:
        with bank.db:
            bank.db.execute("BEGIN IMMEDIATE")
            known = {r["question_key"]: dict(r) for r in bank.db.execute("SELECT * FROM speech_answers")}
            additions = {}
            compare = ("canonical", "question_text", "answer_text", "audio_sha256", "wav_sha256")
            for rows, _, assets in snapshots:
                for row in rows:
                    key = row["question_key"]
                    if key in known:
                        if any(known[key][f] != row[f] for f in compare):
                            raise SpeechBankConflict("exact question has conflicting answer or audio: " + key)
                        if key not in additions:
                            bank._decode(known[key], source="replay")
                        existing += 1
                    else:
                        known[key] = row
                        additions[key] = (row, assets[key])
            # No publish until every source and destination collision has passed.
            for key, (row, pair) in additions.items():
                row = dict(row)
                paths = bank._paths(key)
                for path, data in zip(paths, pair):
                    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                        staged = Path(stream.name)
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.replace(staged, path)
                    finally:
                        staged.unlink(missing_ok=True)
                row["mp3_path"], row["wav_path"] = [str(p.relative_to(bank.artifact_dir)) for p in paths]
                # Explicit known schema only; never execute source-controlled identifiers.
                columns = ("question_key", "canonical", "question_text", "answer_text", "voice", "model",
                           "mp3_path", "wav_path", "audio_sha256", "wav_sha256", "source_profile",
                           "source_test", "source_question", "reuse_count", "created_at", "last_used_at")
                bank.db.execute("INSERT INTO speech_answers (" + ",".join(columns) + ") VALUES (" +
                                ",".join("?" for _ in columns) + ")", tuple(row[c] for c in columns))
                imported += 1
            event_columns = ("question_key", "source_profile", "source_test", "source_question", "delivery", "used_at")
            present = Counter(tuple(row[c] for c in event_columns) for row in bank.db.execute("SELECT * FROM speech_usage"))
            for _, events, _ in snapshots:
                wanted = Counter(tuple(row[c] for c in event_columns) for row in events)
                for event, count in wanted.items():
                    for _ in range(max(0, count - present[event])):
                        bank.db.execute("INSERT INTO speech_usage (" + ",".join(event_columns) + ") VALUES (?,?,?,?,?,?)", event)
                        usage_added += 1
                    present[event] = max(present[event], count)
            bank.db.execute("""UPDATE speech_answers SET reuse_count=max(reuse_count,
                (SELECT count(*) FROM speech_usage u WHERE u.question_key=speech_answers.question_key AND u.delivery='replay'))""")
    return {"imported": imported, "existing": existing, "usage_added": usage_added, "sources": len(selected)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(merge_banks(args.destination, args.source)))


if __name__ == "__main__":
    main()
