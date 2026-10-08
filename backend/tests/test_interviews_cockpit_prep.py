"""Cockpit prep — pure manifest mapping + guarded staging (no DB, no network)."""
from backend.interviews import cockpit_prep as c
from backend.interviews import interview_prep


def _fake_pack(**kw):
    p = interview_prep.PrepPack(mailbox="x@takhet.com")
    p.candidate_name = "Emma Simmons"; p.company = "acme"; p.has_resume = True
    p.resume_filename = "Emma Simmons - resume.pdf"; p.resume_path = "/tmp/r.pdf"
    p.join_url = "https://z.zoom.us/j/1"; p.brief = "b"
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_build_manifest_maps_fields(monkeypatch):
    monkeypatch.setattr(interview_prep, "build_pack", lambda mb, iv_row=None: _fake_pack())
    m, pack = c.build_manifest("x@takhet.com")
    assert m["candidate_name"] == "Emma Simmons" and m["company"] == "acme"
    assert m["has_resume"] is True and m["resume_mac_path"].endswith("/x@takhet.com/resume.pdf")
    assert m["join_url"] == "https://z.zoom.us/j/1" and m["cockpit_url"]


def test_build_manifest_no_resume_has_no_mac_path(monkeypatch):
    monkeypatch.setattr(interview_prep, "build_pack",
                        lambda mb, iv_row=None: _fake_pack(has_resume=False, resume_path=None))
    m, _ = c.build_manifest("x@takhet.com")
    assert m["has_resume"] is False and m["resume_mac_path"] is None


def test_stage_to_mac_soft_fails_offline(monkeypatch):
    # ssh mkdir fails → mac offline → never raises, reports not-online
    monkeypatch.setattr(c, "_run", lambda cmd, timeout=60: (False, "ssh: connect timeout"))
    res = c.stage_to_mac({"mailbox": "x@takhet.com"}, "/tmp/r.pdf")
    assert res["mac_online"] is False and res["manifest"] is None
