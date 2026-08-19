"""Batch digest + profile reality gate + staleness cutoff.

Pure / monkeypatched — no browser, no LLM, no network, no Telegram, no real profiles.
"""
import asyncio
import json
import logging
import os
import time

import backend.applier.batch as batch
import bot.notify as notify_mod
from backend.profiles.store import Profile


def _profile(pid: str, **kw) -> Profile:
    """A DELIVERABLE profile by default (real-looking phone, non-placeholder email)."""
    base = dict(id=pid, full_name=f"{pid.title()} Person",
                email=f"{pid}@realmail.io", phone="312-880-1234")
    base.update(kw)
    return Profile(**base)


def _write_report(out_dir, jid, days_old=0.0, **extra):
    d = out_dir / jid
    d.mkdir(parents=True)
    rep = {"apply_url": f"https://jobs.example/{jid}", "job_title": jid,
           "company": "Co", "submitted": False, **extra}
    f = d / "report.json"
    f.write_text(json.dumps(rep), encoding="utf-8")
    if days_old:
        old = time.time() - days_old * 86400
        os.utime(f, (old, old))
    return rep["apply_url"]


def _stub_env(monkeypatch, tmp_path, jobs=(), profile=None, notify_ok=True):
    """Wire batch_prefill's seams: OUT_ROOT, profile, db collector, prefill, notify.
    Returns (sent_notifications, prefilled_urls)."""
    sent, prefilled = [], []
    monkeypatch.setattr(batch, "OUT_ROOT", tmp_path)
    monkeypatch.setattr(batch, "get_profile", lambda pid: profile or _profile(pid))

    def fake_notify(text, parse_mode=None):
        sent.append((text, parse_mode))
        return notify_ok
    monkeypatch.setattr("bot.notify.notify", fake_notify)

    async def collect_db(limit=0, keywords=None):
        return list(jobs)
    monkeypatch.setattr(batch.boards, "collect_from_db", collect_db)

    async def fake_prefill(job, profile, **kw):
        prefilled.append(job["apply_url"])
        return {"apply_url": job["apply_url"], "job_title": job.get("title", ""),
                "company": job.get("company", ""), "page_type": "application_form",
                "failed": 0, "filled": 5, "match_score": 42, "submitted": False}
    monkeypatch.setattr(batch, "prefill_application", fake_prefill)
    return sent, prefilled


# --- reality gate: undeliverable profiles never spend prefill slots ----------------

def test_validator_gate_blocks_fictional_phone(monkeypatch, tmp_path, caplog):
    sent, prefilled = _stub_env(monkeypatch, tmp_path,
                                profile=_profile("michael", phone="415-555-0134"))
    collected = []

    async def no_collect(*a, **kw):
        collected.append(1)
        return []
    monkeypatch.setattr(batch.boards, "collect", no_collect)
    monkeypatch.setattr(batch.boards, "collect_from_db", no_collect)

    with caplog.at_level(logging.ERROR, logger="backend.applier.batch"):
        res = asyncio.run(batch.batch_prefill(profile_id="michael", source="both"))

    reasons = res["summary"]["blocked_reasons"]
    assert any("555-01" in r for r in reasons)
    assert res["summary"]["notify"] == "ok"
    assert not collected and not prefilled           # ALL prefill work skipped
    assert not (tmp_path / "michael").exists()       # no queue dir even created
    assert any("blocked" in r.message.lower() for r in caplog.records)
    msg, parse_mode = sent[0]
    assert parse_mode is None                        # plain text, quotes stay readable
    assert "michael" in msg
    for r in reasons:
        assert r in msg
    assert "fix it in /setup: https://jobfinder.systeam.kz/setup" in msg


def test_all_loop_continues_past_blocked_profile(monkeypatch, tmp_path):
    profs = {"kate": _profile("kate", phone="212-555-0100"),   # fictional -> blocked
             "michael": _profile("michael")}
    monkeypatch.setattr(batch, "load_profiles", lambda: profs)
    facts, etalons = tmp_path / "facts", tmp_path / "etalons"
    facts.mkdir()
    etalons.mkdir()
    fams = {"kate": "data", "michael": "qa"}
    for pid in profs:
        (facts / f"{pid}.json").write_text(
            f'{{"role_family": "{fams[pid]}"}}', encoding="utf-8")
        (etalons / f"{pid}.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(batch, "FACTS_DIR", facts)
    monkeypatch.setattr(batch, "ETALONS_DIR", etalons)
    # both profiles must draw an assignment so batch_prefill runs (kate then hits the
    # reality gate inside it, michael prefills an empty pool -> queue 0).
    monkeypatch.setattr(batch, "_online_roles", lambda: [
        {"apply_url": "https://x/data", "title": "Data Analyst", "family": "data"},
        {"apply_url": "https://x/qa", "title": "QA Engineer", "family": "qa"}])
    monkeypatch.setattr(batch, "_load_assignments", lambda: {})
    monkeypatch.setattr(batch, "_record_assignment", lambda *a, **k: None)
    _stub_env(monkeypatch, tmp_path / "prefill")
    monkeypatch.setattr(batch, "get_profile", lambda pid: profs[pid])

    res = asyncio.run(batch.batch_prefill_all(source="db"))
    assert res["profiles"]["kate"]["blocked_reasons"]      # kate refused
    m = res["profiles"]["michael"]                          # michael still ran...
    assert "blocked_reasons" not in m and "error" not in m  # ...normally, not blocked/errored


# --- staleness cutoff: old pending items drop out but never re-enter ---------------

def test_stale_pending_dropped_and_archived(tmp_path):
    stale_url = _write_report(tmp_path, "old-job", days_old=15)
    fresh_url = _write_report(tmp_path, "new-job", days_old=2)
    pending, done = batch._prior_state(tmp_path)
    assert fresh_url in pending and stale_url not in pending
    assert not done
    archived = json.loads((tmp_path / "archived.json").read_text(encoding="utf-8"))
    assert archived == [{"jid": "old-job", "url": stale_url, "reason": "stale"}]


def test_stale_archived_only_once_across_runs(tmp_path):
    _write_report(tmp_path, "old-job", days_old=15)
    batch._prior_state(tmp_path)
    batch._prior_state(tmp_path)  # report dir still on disk — must not re-append
    archived = json.loads((tmp_path / "archived.json").read_text(encoding="utf-8"))
    assert len(archived) == 1


def test_terminal_status_beats_staleness(tmp_path):
    url = _write_report(tmp_path, "old-sub", days_old=20)
    (tmp_path / "status.json").write_text(json.dumps({"old-sub": {"status": "submitted"}}))
    pending, done = batch._prior_state(tmp_path)
    assert url in done and not pending
    assert not (tmp_path / "archived.json").exists()


def test_stale_job_cannot_reenter_as_new(monkeypatch, tmp_path):
    out_dir = tmp_path / "michael"
    stale_url = _write_report(out_dir, "old-job", days_old=15)
    # the posting is STILL OPEN (collector returns it) — must be skipped anyway
    sent, prefilled = _stub_env(monkeypatch, tmp_path, jobs=[
        {"apply_url": stale_url, "title": "old-job", "company": "Co"}])

    res = asyncio.run(batch.batch_prefill(profile_id="michael", source="db"))
    assert prefilled == []                       # never re-prefilled
    assert res["summary"]["queue_size"] == 0     # and not re-queued either

    res2 = asyncio.run(batch.batch_prefill(profile_id="michael", source="db"))
    assert prefilled == [] and res2["summary"]["queue_size"] == 0


# --- _build_digest: pure output shape ----------------------------------------------

def _item(company, title, jid, mtime, score, page_type="application_form",
          failed=0, **extra):
    return {"company": company, "job_title": title, "_jid": jid, "_mtime": mtime,
            "match_score": score, "page_type": page_type, "failed": failed, **extra}


def test_digest_counts_links_and_order():
    queue = [
        _item("Acme", "Support Eng", "acme-support-eng", 100.0, 40),
        _item("Beta", "CS Rep", "beta-cs-rep", 200.0, 10),        # freshest -> first
        _item("Gamma", "Helpdesk", "gamma-helpdesk", 100.0, 90),  # Acme's age, higher score
        _item("Delta", "Agent", "delta-agent", 300.0, 50, page_type="login_required"),
        _item("Epsi", "Rep", "epsi-rep", 300.0, 50, failed=2),
        {"job_title": "boom", "error": "kaput"},                  # errors count nowhere
    ]
    lines = batch._build_digest("michael", queue, new_count=2,
                                status_age_days=None).split("\n")
    assert lines[0].startswith("<b>3 ready</b> / 2 need info / 2 new this run")
    assert "michael" in lines[0]
    assert lines[1] == ('<a href="https://jobfinder.systeam.kz/#job-beta-cs-rep">'
                        'Beta — CS Rep</a> (score 10)')
    assert lines[2].startswith('<a href="https://jobfinder.systeam.kz/#job-gamma-helpdesk">')
    assert lines[3].startswith('<a href="https://jobfinder.systeam.kz/#job-acme-support-eng">')
    assert "untouched" not in "\n".join(lines)


def test_digest_caps_ready_links_at_five():
    queue = [_item(f"Co{i}", f"T{i}", f"co{i}", float(i), 1) for i in range(7)]
    assert batch._build_digest("kate", queue, 0, None).count("<a href=") == 5


def test_digest_escapes_html_in_names():
    queue = [_item("A&B <Corp>", "QA <lead>", "a-b-corp-qa-lead", 1.0, 5)]
    d = batch._build_digest("kate", queue, 0, None)
    assert "A&amp;B &lt;Corp&gt;" in d and "<Corp>" not in d


def test_digest_untouched_warning():
    queue = [_item("Acme", "T", "acme-t", 1.0, 1)]
    assert "queue untouched for 3 days" in batch._build_digest("kate", queue, 0, 3.4)
    assert "untouched" not in batch._build_digest("kate", queue, 0, 1.5)   # <48h
    assert "untouched" not in batch._build_digest("kate", queue, 0, None)  # no status.json
    assert "untouched" not in batch._build_digest("kate", [], 0, 9.0)      # empty queue


# --- batch sends the digest and reports the outcome ---------------------------------

def test_batch_sends_html_digest_and_reports_notify_ok(monkeypatch, tmp_path):
    sent, _ = _stub_env(monkeypatch, tmp_path, jobs=[
        {"apply_url": "https://jobs.example/a", "title": "Support Eng", "company": "Acme"}])
    res = asyncio.run(batch.batch_prefill(profile_id="michael", source="db"))
    assert res["summary"]["notify"] == "ok"
    text, parse_mode = sent[0]
    assert parse_mode == "HTML"
    assert text.split("\n")[0].startswith("<b>1 ready</b> / 0 need info / 1 new this run")
    assert "#job-acme-support-eng" in text          # jid == the runner's report dir slug
    # private digest keys never leak into the persisted queue
    written = json.loads((tmp_path / "michael" / "review_queue.json").read_text())
    assert all(not k.startswith("_") for it in written for k in it)


def test_batch_reports_notify_fail(monkeypatch, tmp_path):
    _stub_env(monkeypatch, tmp_path, notify_ok=False)
    res = asyncio.run(batch.batch_prefill(profile_id="michael", source="db"))
    assert res["summary"]["notify"] == "fail"


# --- bot.notify: parse_mode + bool + warning on failure -----------------------------

def test_notify_parse_mode_optional(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "telegram_bot_token", "T")
    monkeypatch.setattr(settings, "telegram_chat_id", "C")
    posted = []

    class _R:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(notify_mod.httpx, "post",
                        lambda url, json=None, timeout=None: posted.append(json) or _R())
    assert notify_mod.notify("hi") is True
    assert "parse_mode" not in posted[0]            # default keeps old behavior
    assert notify_mod.notify("<b>hi</b>", parse_mode="HTML") is True
    assert posted[1]["parse_mode"] == "HTML"


def test_notify_failure_returns_false_and_warns(monkeypatch, caplog):
    from backend.config import settings
    monkeypatch.setattr(settings, "telegram_bot_token", "T")
    monkeypatch.setattr(settings, "telegram_chat_id", "C")

    def boom(*a, **kw):
        raise RuntimeError("net down")
    monkeypatch.setattr(notify_mod.httpx, "post", boom)
    with caplog.at_level(logging.WARNING, logger="bot.notify"):
        assert notify_mod.notify("hi") is False
    assert any("send failed" in r.message for r in caplog.records)
