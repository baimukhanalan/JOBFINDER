"""Self-hosted mail provider: Maildir inbound read + local-Postfix outbound send.

Self-hosted mail tests. No network, no real mail server: inbound is a
fake Maildir tree on tmp_path; outbound uses a fake smtplib.SMTP.
"""
from email.message import EmailMessage

import pytest

from backend.tools import mail_sink


def _write_maildir_msg(base, domain, local, *, frm, to, subject, body, sub="new"):
    d = base / domain / local / sub
    d.mkdir(parents=True, exist_ok=True)
    m = EmailMessage()
    m["From"] = frm
    m["To"] = to
    m["Subject"] = subject
    m["Message-ID"] = f"<{subject.replace(' ', '')}@test>"
    m["Date"] = "Tue, 19 Aug 2026 10:00:00 +0000"
    m.set_content(body)
    (d / f"msg-{local}-{sub}-{abs(hash(subject)) % 99999}").write_bytes(m.as_bytes())


# ---------------------------------------------------------------------------
# inbound: _poll_maildir
# ---------------------------------------------------------------------------
def test_poll_maildir_reads_classifies_resolves(tmp_path, monkeypatch):
    base = tmp_path / "vhosts"
    domain = "jobs.test"
    # A recruiter emailed the per-application plus-address; Postfix delivered it to
    # the base local-part's Maildir (recipient_delimiter=+), To header keeps the +jid.
    _write_maildir_msg(base, domain, "alice",
                       frm="Recruiter <talent@acme.test>",
                       to="alice+job-42@jobs.test",
                       subject="Interview invitation",
                       body="Hi Alice, please pick a time on our Calendly for a call.")
    monkeypatch.setattr(mail_sink, "MAILDIR_BASE", str(base))
    monkeypatch.setattr(mail_sink, "DOMAIN", domain)
    monkeypatch.setattr(mail_sink, "address_map", lambda: {"alice": "alice@jobs.test"})

    msgs: dict = {}
    res = mail_sink._poll_maildir(msgs, 1000)
    assert res["new"] == 1 and res["total"] == 1
    (rec,) = list(msgs.values())
    assert rec["profile"] == "alice"          # resolved via the base address
    assert rec["jid"] == "job-42"             # resolved via the +tag
    assert rec["kind"] == "interview"         # classified
    assert "calendly" in rec["body"].lower()
    assert "acme.test" in rec["from"]
    assert rec["id"].startswith("md:")

    # dedup: re-polling the same Maildir adds nothing new
    res2 = mail_sink._poll_maildir(msgs, 1001)
    assert res2["new"] == 0 and res2["total"] == 1


def test_poll_maildir_missing_domain_dir_is_soft(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_sink, "MAILDIR_BASE", str(tmp_path / "nope"))
    monkeypatch.setattr(mail_sink, "DOMAIN", "jobs.test")
    res = mail_sink._poll_maildir({}, 1)
    assert res["new"] == 0  # absent path -> no crash, nothing captured


def test_poll_maildir_requires_domain(monkeypatch):
    monkeypatch.setattr(mail_sink, "DOMAIN", "")
    res = mail_sink._poll_maildir({}, 1)
    assert res["new"] == 0 and "MAIL_DOMAIN" in res["error"]


# ---------------------------------------------------------------------------
# outbound: sh_send
# ---------------------------------------------------------------------------
class _FakeSMTP:
    last = None

    def __init__(self, host, port, timeout=0):
        self.host, self.port = host, port
        self.did = []
        _FakeSMTP.last = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.did.append("starttls")

    def login(self, user, password):
        self.did.append(("login", user, password))

    def send_message(self, msg):
        self.sent = msg
        self.did.append("send")


def test_sh_send_submits_with_from_and_threading(monkeypatch):
    monkeypatch.setattr(mail_sink, "SH_SMTP_LOGIN", "bot@jobs.test")
    monkeypatch.setattr(mail_sink, "SH_SMTP_PASSWORD", "secret")
    monkeypatch.setattr(mail_sink, "SH_SMTP_HOST", "127.0.0.1")
    monkeypatch.setattr(mail_sink, "SH_SMTP_PORT", 587)
    monkeypatch.setattr(mail_sink.smtplib, "SMTP", _FakeSMTP)

    res = mail_sink.sh_send("talent@acme.test", "Re: Interview invitation",
                            "Thanks, Tuesday works.", frm="alice@jobs.test",
                            headers={"In-Reply-To": "<x@acme.test>",
                                     "References": "<x@acme.test>"})
    assert res["ok"] is True and res["via"] == "smtp"
    sess = _FakeSMTP.last
    assert "starttls" in sess.did
    assert ("login", "bot@jobs.test", "secret") in sess.did
    assert sess.sent["From"] == "alice@jobs.test"          # candidate identity
    assert sess.sent["To"] == "talent@acme.test"
    assert sess.sent["In-Reply-To"] == "<x@acme.test>"     # threaded


def test_sh_send_refuses_without_credentials(monkeypatch):
    monkeypatch.setattr(mail_sink, "SH_SMTP_LOGIN", "")
    monkeypatch.setattr(mail_sink, "SH_SMTP_PASSWORD", "")
    res = mail_sink.sh_send("x@y.test", "s", "b", frm="a@jobs.test")
    assert res["ok"] is False and "LOGIN" in res["error"]


def test_sh_send_refuses_without_from(monkeypatch):
    monkeypatch.setattr(mail_sink, "SH_SMTP_LOGIN", "bot@jobs.test")
    monkeypatch.setattr(mail_sink, "SH_SMTP_PASSWORD", "secret")
    res = mail_sink.sh_send("x@y.test", "s", "b", frm="")
    assert res["ok"] is False and "From" in res["error"]


# ---------------------------------------------------------------------------
# dispatch + status
# ---------------------------------------------------------------------------
def test_send_dispatch_selfhost_calls_sh_send(monkeypatch):
    monkeypatch.setattr(mail_sink, "PROVIDER", "selfhost")
    called = {}

    def fake_sh_send(*a, **k):
        called["hit"] = (a, k)
        return {"ok": True}

    monkeypatch.setattr(mail_sink, "sh_send", fake_sh_send)
    out = mail_sink._send("x@y.test", "s", "b", frm="a@jobs.test")
    assert out["ok"] and "hit" in called


def test_status_selfhost(monkeypatch):
    monkeypatch.setattr(mail_sink, "PROVIDER", "selfhost")
    monkeypatch.setattr(mail_sink, "DOMAIN", "jobs.test")
    monkeypatch.setattr(mail_sink, "SH_SMTP_LOGIN", "bot@jobs.test")
    monkeypatch.setattr(mail_sink, "SH_SMTP_PASSWORD", "secret")
    st = mail_sink.status()
    assert st["provider"] == "selfhost"
    assert st["configured"] is True
    assert st["smtp_login_set"] is True
    assert st["smtp"].endswith(":587")
