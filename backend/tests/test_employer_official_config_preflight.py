import json
from contextlib import contextmanager

from backend.tools import employer_official_config_preflight as preflight


class Cursor:
    def execute(self, _sql, _params=()):
        pass

    def fetchone(self):
        return {"active_total": 10000, "sam_linked_active": 734,
                "sam_linked_with_gaps": 700, "sec_linked_active": 3,
                "sec_linked_with_gaps": 2, "irs_linked_active": 0,
                "fdic_linked_active": 9}


def install_db(monkeypatch):
    @contextmanager
    def fake_cur(*_args, **_kwargs):
        yield Cursor()
    monkeypatch.setattr(preflight.company_db, "_cur", fake_cur)


def test_report_is_secret_safe_and_only_exposes_presence_and_format(monkeypatch):
    install_db(monkeypatch)
    sam_secret = "A" * 40
    sec_identity = "JobFinder Ops security@example.com"
    report = preflight.build_report(environ={
        "SAM_API_KEY": sam_secret, "SEC_USER_AGENT": sec_identity})
    encoded = json.dumps(report)
    assert sam_secret not in encoded
    assert sec_identity not in encoded
    assert report["owner_config"]["SAM_API_KEY"]["ready"] is True
    assert report["owner_config"]["SEC_USER_AGENT"]["ready"] is True
    assert report["owner_config"]["SAM_API_KEY"]["linked_active"] == 734
    assert report["owner_config"]["SEC_USER_AGENT"]["linked_with_gaps"] == 2


def test_missing_and_placeholder_values_are_not_ready(monkeypatch):
    install_db(monkeypatch)
    missing = preflight.build_report(environ={})
    assert all(not item["present"] and not item["ready"]
               for item in missing["owner_config"].values())
    invalid = preflight.build_report(environ={
        "SAM_API_KEY": "replace-me", "SEC_USER_AGENT": "just-a-name"})
    assert all(not item["format_valid"] for item in invalid["owner_config"].values())


def test_report_documents_no_key_paths_and_commands(monkeypatch):
    install_db(monkeypatch)
    report = preflight.build_report(environ={})
    assert report["no_key_sources"]["FDIC"] == {
        "requires_key": False, "linked_active": 9}
    assert report["no_key_sources"]["IRS_990"]["requires_key"] is False
    commands = report["resume_commands"]
    assert "SAM_API_KEY" not in commands["sam_exact_uei"]
    assert "SEC_USER_AGENT" not in commands["sec_exact_cik"]
    assert "fdic-linked" in commands["fdic_linked_batch"]
    assert "irs-xml" in commands["irs_exact_filing"]


def test_require_ready_exit_code_never_prints_secret(monkeypatch, capsys):
    install_db(monkeypatch)
    monkeypatch.setenv("SAM_API_KEY", "Z" * 40)
    monkeypatch.setenv("SEC_USER_AGENT", "JobFinder Ops owner@example.com")
    assert preflight.main(["--require-ready"]) == 0
    output = capsys.readouterr().out
    assert "Z" * 40 not in output
    assert "owner@example.com" not in output
    monkeypatch.delenv("SAM_API_KEY")
    assert preflight.main(["--require-ready"]) == 2
