"""Pure-logic tests for the self-scheduling Workday prober (no drive, no network)."""
import backend.tools.workday_probe_promote as p
import backend.tools.mass_hiring_apply_workday_cron as wc


def test_classify_verdicts():
    # real workday_recon log shapes
    assert p.classify("... register captcha presence: {'grecaptcha': False, 'sitekey': None} ...\n"
                      "workday create-account: created=True\n[application CONFIRMED — on-page success page]")[0] == "confirmed"
    assert p.classify("workday register captcha presence: {'grecaptcha': True, 'frames': 1}")[0] == "captcha"
    assert p.classify("register captcha presence: {'grecaptcha': False}\nworkday create-account: created=True")[0] == "no_captcha_incomplete"
    assert p.classify("nothing useful")[0] == "error"
    # confirmed=1 line from the driver is also a pass
    assert p.classify("create-account: created=True\njob 1 persona=x confirmed=1")[0] == "confirmed"


def test_next_pending_skips_verified(monkeypatch):
    monkeypatch.setattr(wc, "_read_verified", lambda: {"sagility"})
    monkeypatch.setattr(p, "_read_json", lambda path: set())
    # sagility verified -> next is highmark (first still-pending in PENDING order)
    assert p.next_pending() == "highmark"


def test_next_pending_none_when_all_done(monkeypatch):
    monkeypatch.setattr(wc, "_read_verified", lambda: set(p.PENDING))
    monkeypatch.setattr(p, "_read_json", lambda path: set())
    assert p.next_pending() is None


def test_quiet_gate(monkeypatch):
    monkeypatch.setattr(p, "_load1", lambda: 3.0)
    monkeypatch.setattr(p, "_jobfinder_headful_drives", lambda: 0)
    assert p.box_is_quiet()[0] is True
    monkeypatch.setattr(p, "_load1", lambda: 15.0)
    assert p.box_is_quiet()[0] is False           # high load
    monkeypatch.setattr(p, "_load1", lambda: 3.0)
    monkeypatch.setattr(p, "_jobfinder_headful_drives", lambda: 1)
    assert p.box_is_quiet()[0] is False           # a live drive owns :98


def test_run_once_busy_skips(monkeypatch):
    monkeypatch.setattr(p, "box_is_quiet", lambda: (False, "load1=20"))
    monkeypatch.setattr(p, "next_pending", lambda: "sagility")
    driven = []
    monkeypatch.setattr(p, "_drive", lambda job: driven.append(job) or "")
    r = p.run_once()
    assert r.get("skipped") and not driven      # never drove while busy


def test_run_once_confirmed_promotes(monkeypatch):
    monkeypatch.setattr(p, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(p, "next_pending", lambda: "sagility")
    monkeypatch.setattr(p, "_drive", lambda job: "create-account: created=True\n[application CONFIRMED — on-page success page]")
    promoted = []
    monkeypatch.setattr(wc, "add_verified", lambda t: promoted.append(t))
    r = p.run_once()
    assert r["verdict"] == "confirmed" and promoted == ["sagility"]


def test_run_once_incomplete_does_not_promote(monkeypatch):
    monkeypatch.setattr(p, "box_is_quiet", lambda: (True, "quiet"))
    monkeypatch.setattr(p, "next_pending", lambda: "sagility")
    monkeypatch.setattr(p, "_drive", lambda job: "register captcha presence: {'grecaptcha': False}\ncreate-account: created=True")
    promoted = []
    monkeypatch.setattr(wc, "add_verified", lambda t: promoted.append(t))
    r = p.run_once()
    assert r["verdict"] == "no_captcha_incomplete" and promoted == []   # NEVER promote on partial
