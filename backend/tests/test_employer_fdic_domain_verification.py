import json
from contextlib import contextmanager

import pytest

from backend.tools import employer_fdic_domain_verification as verification


def verify(page_html, *, final_url="https://bank.example/", legal_name="Exact Bank, N.A."):
    return verification.verify_identity_page(
        proposed_domain="bank.example", final_url=final_url, page_html=page_html,
        legal_name=legal_name)


def test_exact_identity_in_title_schema_or_footer_passes():
    pages = [
        "<title>Personal Banking | Exact Bank, N.A.</title>",
        '<script type="application/ld+json">{"@type":"BankOrCreditUnion",'
        '"name":"Exact Bank, N.A."}</script>',
        '<div class="legal">Copyright Exact Bank, N.A.</div>',
        "<footer>Exact Bank, N.A. Member FDIC</footer>",
    ]
    assert [verify(page)["passed"] for page in pages] == [True, True, True, True]


def test_arbitrary_body_text_is_not_an_identity_factor():
    result = verify("<main>Welcome to Exact Bank, N.A.</main>")
    assert result["passed"] is False
    assert result["reason"] == "exact_identity_not_in_title_schema_legal_footer"


def test_conflicting_redirect_fails_even_with_exact_identity():
    result = verify("<title>Exact Bank, N.A.</title>",
                    final_url="https://unrelated.example/")
    assert result == {"passed": False, "reason": "conflicting_redirect",
                      "proposed_domain": "bank.example",
                      "final_url": "https://unrelated.example/"}


def test_generic_parent_brand_does_not_prove_exact_bank_legal_identity():
    result = verify("<title>Santander Corporate Website</title>"
                    "<footer>Copyright Santander</footer>",
                    legal_name="Santander Bank, N.A.")
    assert result["passed"] is False


class FakeCursor:
    rowcount = 1

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows


def passed(company_id=7, cert="123"):
    proposal = {"entity_id": f"fdic_cert:{cert}", "domain": "bank.example",
                "provenance": {"source_url": "https://api.fdic.gov/banks",
                               "observed_at": "2026-08-26T00:00:00Z"}}
    identity = {"passed": True, "proposed_domain": "bank.example",
                "final_url": "https://bank.example/", "matched_name": "Exact Bank",
                "context_type": "title", "context_excerpt": "Exact Bank"}
    return {"company_id": company_id, "fdic_cert": cert,
            "domain_proposal": proposal, "identity": identity,
            "careers_enrichment": {"careers_url": "https://bank.example/careers",
                                   "careers_confidence": 0.9,
                                   "ats": "workday", "ats_slug": "exactbank",
                                   "ats_url": "https://exactbank.wd5.myworkdayjobs.com/jobs"}}


def test_apply_pass_sets_two_factors_domain_and_post_pass_careers(monkeypatch):
    cursor = FakeCursor([(7, "123", False)])

    @contextmanager
    def fake_cur(_dict_rows=True):
        yield cursor

    monkeypatch.setattr(verification.company_db, "_cur", fake_cur)
    assert verification.apply_passes([passed()]) == {"selected": 1, "updated": 1}
    master_sql, master_params = cursor.calls[1]
    discovery_sql, discovery_params = cursor.calls[2]
    assert "domain_verified=TRUE" in master_sql
    evidence = json.loads(master_params[1])
    assert [item["class"] for item in evidence] == [
        "structured_corporate_source", "official_site_identity"]
    assert {item["provider"] for item in evidence} == {
        "fdic_bankfind", "official_site_identity"}
    assert discovery_params[:5] == (
        "bank.example", "https://bank.example/careers", "workday", "exactbank",
        "https://exactbank.wd5.myworkdayjobs.com/jobs")
    assert "domain_verified" not in discovery_sql


def test_apply_revalidates_certificate_and_rejects_failed_identity(monkeypatch):
    cursor = FakeCursor([(7, "different-cert", False)])

    @contextmanager
    def fake_cur(_dict_rows=True):
        yield cursor

    monkeypatch.setattr(verification.company_db, "_cur", fake_cur)
    with pytest.raises(RuntimeError, match="certificate changed"):
        verification.apply_passes([passed()])
    cursor.rows = [(7, "123", False)]
    failed = passed()
    failed["identity"]["passed"] = False
    with pytest.raises(RuntimeError, match="only passed"):
        verification.apply_passes([failed])


def test_careers_detection_runs_only_after_identity_pass(monkeypatch):
    rows = [
        {"id": 1, "legal_name": "Exact Bank", "trade_name": "",
         "brand_name": "", "cert": "1"},
        {"id": 2, "legal_name": "Wrong Bank", "trade_name": "",
         "brand_name": "", "cert": "2"},
    ]
    cursor = FakeCursor(rows)

    @contextmanager
    def fake_cur(_dict_rows=True):
        yield cursor

    class Response:
        url = "https://bank.example/"
        text = "<title>Exact Bank</title>"

    proposals = lambda cert: {"proposed_domain_evidence": {
        "entity_id": f"fdic_cert:{cert}", "domain": "bank.example",
        "provenance": {"source_url": "fdic", "observed_at": "now"}}}
    calls = []
    monkeypatch.setattr(verification.company_db, "_cur", fake_cur)
    monkeypatch.setattr(verification, "_get", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(verification, "enrich_company",
                        lambda row, client=None: calls.append(row["id"]) or {})
    monkeypatch.setattr(verification, "apply_passes",
                        lambda items: {"selected": len(items), "updated": len(items)})
    result = verification.verify_linked_fdic_domains(
        fdic_fetcher=proposals, client=object(), min_interval=0)
    assert calls == [1]
    assert (result["passed"], result["failed"]) == (1, 1)
