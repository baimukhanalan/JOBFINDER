"""One-click apply server side: /job_pack, /resume_file, /mark_ext.

- all three are token-gated (401 without X-Assist-Token)
- /job_pack: report.json passthrough with [review] prefix stripped to metadata
- /resume_file: the tailored PDF bytes
- /mark_ext: writes status.json via the same locked path as /mark
- jid is sanitized -> path traversal can't escape PREFILL_ROOT
"""
import json

import pytest
from fastapi.testclient import TestClient

import backend.dashboard_app as dash

PDF = b"%PDF-1.4 dummy resume bytes"

REPORT = {
    "job_title": "Support Specialist",
    "company": "Acme",
    "apply_url": "https://jobs.example.com/apply/123",
    "resume_niche": "customer support",
    "drafted_answers": {
        "Why do you want to work here?": "[review] Because of the mission.",
        "Describe your experience.": "Five years in support.",
    },
    "review_items": [{"question": "Why do you want to work here?",
                      "answer": "Because of the mission.", "kind": "draft"}],
    "choice_picks": {"Which shift do you prefer?": "Morning"},
    "unfilled": ["Record a Loom video"],
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "ASSIST_TOKEN", "tok")
    monkeypatch.setattr(dash, "PREFILL_ROOT", tmp_path)
    # status writes go through backend.status_store, which resolves the prefill
    # root from runner.OUT_ROOT at call time — keep both roots in sync here
    from backend.applier import runner
    monkeypatch.setattr(runner, "OUT_ROOT", tmp_path)
    d = tmp_path / "kate" / "acme-support-123"
    d.mkdir(parents=True)
    (d / "report.json").write_text(json.dumps(REPORT), encoding="utf-8")
    (d / "resume.pdf").write_bytes(PDF)
    return TestClient(dash.app)


TOK = {"x-assist-token": "tok"}
Q = {"profile": "kate", "jid": "acme-support-123"}


# --- token gate -------------------------------------------------------------------

def test_job_pack_401_without_token(client):
    assert client.get("/job_pack", params=Q).status_code == 401


def test_resume_file_401_without_token(client):
    assert client.get("/resume_file", params=Q).status_code == 401


def test_mark_ext_401_without_token(client):
    assert client.post("/mark_ext", json={**Q, "to": "submitted"}).status_code == 401


# --- /job_pack --------------------------------------------------------------------

def test_job_pack_404_unknown_jid(client):
    r = client.get("/job_pack", params={"profile": "kate", "jid": "nope"}, headers=TOK)
    assert r.status_code == 404


def test_job_pack_404_on_traversal_jid(client):
    r = client.get("/job_pack", params={"profile": "kate", "jid": "../../etc"},
                   headers=TOK)
    assert r.status_code == 404


def test_job_pack_contents(client):
    r = client.get("/job_pack", params=Q, headers=TOK)
    assert r.status_code == 200
    d = r.json()
    assert d["jid"] == "acme-support-123" and d["profile"] == "kate"
    assert d["job_title"] == "Support Specialist" and d["company"] == "Acme"
    assert d["apply_url"] == "https://jobs.example.com/apply/123"
    assert d["niche"] == "customer support"
    # the '[review]' wire prefix never leaves the server — stripped + review map
    assert d["answers"] == {
        "Why do you want to work here?": "Because of the mission.",
        "Describe your experience.": "Five years in support.",
    }
    assert d["review"] == {"Why do you want to work here?": True}
    assert d["review_items"] == REPORT["review_items"]
    assert d["choice_picks"] == {"Which shift do you prefer?": "Morning"}
    assert d["unfilled"] == ["Record a Loom video"]
    assert d["has_resume"] is True


def test_job_pack_has_resume_false_without_pdf(client, tmp_path):
    d = tmp_path / "kate" / "no-pdf-job"
    d.mkdir()
    (d / "report.json").write_text(json.dumps({"company": "X"}), encoding="utf-8")
    r = client.get("/job_pack", params={"profile": "kate", "jid": "no-pdf-job"},
                   headers=TOK)
    assert r.status_code == 200
    assert r.json()["has_resume"] is False


# --- /resume_file -----------------------------------------------------------------

def test_resume_file_returns_pdf(client):
    r = client.get("/resume_file", params=Q, headers=TOK)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == PDF


def test_resume_file_404_unknown(client):
    r = client.get("/resume_file", params={"profile": "kate", "jid": "nope"},
                   headers=TOK)
    assert r.status_code == 404


def test_resume_file_404_on_traversal(client):
    r = client.get("/resume_file", params={"profile": "../..", "jid": "etc"},
                   headers=TOK)
    assert r.status_code == 404


# --- /mark_ext --------------------------------------------------------------------

def test_mark_ext_writes_status(client, tmp_path):
    r = client.post("/mark_ext", headers=TOK, json={**Q, "to": "submitted"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "status": "submitted"}
    st = json.loads((tmp_path / "kate" / "status.json").read_text(encoding="utf-8"))
    assert set(st) == {"acme-support-123"}
    assert st["acme-support-123"]["status"] == "submitted"


def test_mark_ext_accepts_all_states(client, tmp_path):
    for to in ("submitted", "rejected", "interview", "pending"):
        assert client.post("/mark_ext", headers=TOK,
                           json={**Q, "to": to}).status_code == 200
    st = json.loads((tmp_path / "kate" / "status.json").read_text(encoding="utf-8"))
    assert st["acme-support-123"]["status"] == "pending"


def test_mark_ext_400_on_bad_status(client, tmp_path):
    for bad in ("withdrawn", "", "SUBMITTED"):
        r = client.post("/mark_ext", headers=TOK, json={**Q, "to": bad})
        assert r.status_code == 400
    assert not (tmp_path / "kate" / "status.json").exists()


# --- /mark (existing dashboard route, now on the shared helper) --------------------

def test_mark_still_works(client, tmp_path):
    r = client.post("/mark/acme-support-123?profile=kate",
                    data={"to": "submitted"}, follow_redirects=False)
    assert r.status_code == 303
    st = json.loads((tmp_path / "kate" / "status.json").read_text(encoding="utf-8"))
    assert set(st) == {"acme-support-123"}
    assert st["acme-support-123"]["status"] == "submitted"


def test_mark_undo_pops_entry(client, tmp_path):
    client.post("/mark/acme-support-123?profile=kate",
                data={"to": "submitted"}, follow_redirects=False)
    r = client.post("/mark/acme-support-123?profile=kate",
                    data={"to": ""}, follow_redirects=False)
    assert r.status_code == 303
    st = json.loads((tmp_path / "kate" / "status.json").read_text(encoding="utf-8"))
    assert st == {}
