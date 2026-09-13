"""LinkedIn URL is unique per persona (stable per pid, different across personas) — a fixed campaign
name must NOT produce the same linkedin.com/in/<slug> every time (that constant is what Ashby's
per-tenant spam model clustered on and self-flagged Salmon)."""
from backend.tools import synth_persona as sp

JOB = {"title": "Data Analyst", "location": "Kazakhstan", "regions": ["OTHER"], "open_anywhere": True}


def _li(raw, pid):
    return sp._build_candidate(raw, "kz", JOB, email=f"{pid}@takhet.com", pid=pid)["profile"]["linkedin_url"]


def test_same_name_different_persona_different_linkedin():
    raw = {"full_name": "Dana Yerlan", "headline": "Data Analyst", "city": "Almaty"}
    a = _li(raw, "demo_dana_yerlan_1")
    b = _li(raw, "demo_dana_yerlan_2")
    assert a != b, "a repeated name must not reuse one LinkedIn URL"
    assert a.startswith("https://www.linkedin.com/in/dana-yerlan-")
    assert b.startswith("https://www.linkedin.com/in/dana-yerlan-")


def test_stable_for_same_persona():
    raw = {"full_name": "Dana Yerlan", "headline": "Data Analyst", "city": "Almaty"}
    assert _li(raw, "demo_dana_yerlan_1") == _li(raw, "demo_dana_yerlan_1")


def test_synth_persona_end_to_end_unique(monkeypatch):
    # two synth personas for the same job under the SAME forced name get different LinkedIn URLs
    c1 = sp.synth_persona(JOB, name="Dana Yerlan", email="dana.yerlan11@takhet.com", pid="demo_a")
    c2 = sp.synth_persona(JOB, name="Dana Yerlan", email="dana.yerlan22@takhet.com", pid="demo_b")
    assert c1["profile"]["linkedin_url"] != c2["profile"]["linkedin_url"]
