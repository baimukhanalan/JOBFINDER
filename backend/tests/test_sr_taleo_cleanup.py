"""Disk-leak regression: the SmartRecruiters + Taleo apply lanes MUST reclaim their per-run
browser-profile dir in a `finally` (they used to leak — 269 sr_stealth_profile_* = 4.4 GB in
backend/data/ and 449 taleo_prof_* = 6.3 GB in /tmp). No network, no real browser."""
import asyncio
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mass_hiring_apply_sr_cron as sr_cron  # noqa: E402
from backend.tools import taleo_recon  # noqa: E402


# ---- SmartRecruiters cron (apply_one) -----------------------------------------------------------

def test_sr_apply_one_removes_per_run_profile_dir(monkeypatch):
    """apply_one() reclaims the SR_PROFILE_DIR it hands the recon subprocess, even after a clean run."""
    jobid = 987654321
    expected = os.path.join(sr_cron.REPO, "backend", "data", f"sr_stealth_profile_{os.getpid()}_{jobid}")
    shutil.rmtree(expected, ignore_errors=True)

    def fake_run(cmd, cwd=None, env=None, capture_output=None, text=None, timeout=None):
        # The real recon creates its SR_PROFILE_DIR; simulate that so cleanup has something to remove.
        assert env["SR_PROFILE_DIR"] == expected
        os.makedirs(env["SR_PROFILE_DIR"], exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, stdout='[result] {"jobid": 987654321, "ack": true}\n', stderr="")

    monkeypatch.setattr(sr_cron.subprocess, "run", fake_run)
    try:
        res = sr_cron.apply_one(jobid, keep=1)
        assert res.get("ack") is True
        assert not os.path.isdir(expected), "sr_cron.apply_one leaked its per-run stealth profile dir"
    finally:
        shutil.rmtree(expected, ignore_errors=True)


def test_sr_apply_one_removes_profile_dir_on_timeout(monkeypatch):
    """Even when the subprocess times out (and gets pkill'd before its own finally can run), the cron
    finally still reclaims the dir."""
    jobid = 987654322
    expected = os.path.join(sr_cron.REPO, "backend", "data", f"sr_stealth_profile_{os.getpid()}_{jobid}")
    shutil.rmtree(expected, ignore_errors=True)

    def fake_run(cmd, cwd=None, env=None, capture_output=None, text=None, timeout=None):
        if cmd and cmd[0] == "pkill":
            return subprocess.CompletedProcess(cmd, 0)
        os.makedirs(env["SR_PROFILE_DIR"], exist_ok=True)  # recon started, then hung
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(sr_cron.subprocess, "run", fake_run)
    try:
        res = sr_cron.apply_one(jobid, keep=1)
        assert res.get("error") == "timeout"
        assert not os.path.isdir(expected), "sr_cron.apply_one leaked its profile dir on a timeout-kill"
    finally:
        shutil.rmtree(expected, ignore_errors=True)


# ---- Taleo recon (run) --------------------------------------------------------------------------

class _FakePage:
    url = "https://ttec.taleo.net/careersection/jobapply.ftl?job=1"

    async def goto(self, *a, **k):
        pass

    async def wait_for_timeout(self, *a, **k):
        pass

    async def screenshot(self, *a, **k):
        raise RuntimeError("no screenshots in the test")  # _shot swallows this -> writes no files

    async def content(self):
        return "<html></html>"

    async def evaluate(self, *a, **k):
        return ""


class _FakeCtx:
    pages: list = []

    async def new_page(self):
        return _FakePage()

    async def close(self):
        pass


class _FakePW:
    def __init__(self):
        self.chromium = self

    async def launch_persistent_context(self, profile_dir, **k):
        os.makedirs(profile_dir, exist_ok=True)  # the real launch materializes the profile dir
        return _FakeCtx()


class _FakePWCtx:
    async def __aenter__(self):
        return _FakePW()

    async def __aexit__(self, *a):
        return False


class _FakeStrat:
    async def prefill(self, *a, **k):
        return {"unfilled": [], "review_items": [], "page_type": "application_form"}


class _FakeStratRaises:
    async def prefill(self, *a, **k):
        raise RuntimeError("selectors broke mid-walk")


def _wire_taleo(monkeypatch, strat_cls):
    import playwright.async_api as pw_api
    import backend.applier.strategies.taleo as taleo_strat

    monkeypatch.setattr(pw_api, "async_playwright", lambda: _FakePWCtx())
    monkeypatch.setattr(taleo_strat, "TaleoStrategy", strat_cls)
    monkeypatch.setattr(taleo_recon, "is_licensed", lambda title: False)
    monkeypatch.setattr(taleo_recon, "_resolve_taleo_url", lambda url: _FakePage.url)
    monkeypatch.setattr(taleo_recon, "_build_persona", lambda row: {
        "profile_form": {"full_name": "Test P", "email": "test@takhet.com", "city": "Columbus",
                         "state": "Ohio", "zip": "43004", "country": "United States"},
        "facts": {}, "resume_path": "/nonexistent/resume.pdf",
        "state_code": "OH", "jobid": "1", "profile_id": "demo_test"})

    class _Cursor:
        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return (1, "Customer Service Rep", "https://x/apply", "TTEC", "Remote, US", "ttec")

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(taleo_recon.mail_db, "conn", lambda: _Conn())


def _expected_taleo_dir(job_id):
    import tempfile
    return os.path.join(tempfile.gettempdir(), f"taleo_prof_{job_id}_{os.getpid()}")


def test_taleo_run_removes_profile_dir(monkeypatch):
    job_id = 987654323
    expected = _expected_taleo_dir(job_id)
    shutil.rmtree(expected, ignore_errors=True)
    _wire_taleo(monkeypatch, _FakeStrat)
    try:
        asyncio.run(taleo_recon.run(job_id, keep_minutes=0, fresh=True))
        assert not os.path.isdir(expected), "taleo_recon.run leaked its per-run profile dir"
    finally:
        shutil.rmtree(expected, ignore_errors=True)
        shutil.rmtree(os.path.join(taleo_recon.REPO, "logs", "taleo_recon", str(job_id)), ignore_errors=True)


def test_taleo_run_removes_profile_dir_on_exception(monkeypatch):
    """The dir is reclaimed in `finally` even when the browser walk raises mid-run."""
    job_id = 987654324
    expected = _expected_taleo_dir(job_id)
    shutil.rmtree(expected, ignore_errors=True)
    _wire_taleo(monkeypatch, _FakeStratRaises)
    try:
        asyncio.run(taleo_recon.run(job_id, keep_minutes=0, fresh=True))
        assert not os.path.isdir(expected), "taleo_recon.run leaked its profile dir after an exception"
    finally:
        shutil.rmtree(expected, ignore_errors=True)
        shutil.rmtree(os.path.join(taleo_recon.REPO, "logs", "taleo_recon", str(job_id)), ignore_errors=True)
