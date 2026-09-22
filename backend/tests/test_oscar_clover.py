"""Network-free tests for the Oscar/Clover Greenhouse mass-hiring auto-apply wiring.

Oscar and Clover are STANDARD Greenhouse boards (recon 2026-09-22: the embed form renders, the
board-api returns the standard question set), so they are driven through the EXISTING Greenhouse
co-pilot auto-submit path — no new strategy. These tests lock the pure URL/answer helpers that
resolve a `mass_hiring_jobs` row to the Greenhouse EMBED apply URL + drafted answers, and prove the
OSCAR_CLOVER_ADVANCE gate keeps a dry run from driving the co-pilot. No live HTTP / DB / browser.
"""
from backend.tools import oscar_clover_recon as oc


# --- apply-URL parsing / embed-URL resolution ----------------------------------------------------

def test_parse_apply_url_board_view():
    slug, jid = oc.parse_apply_url("https://job-boards.greenhouse.io/oscar/jobs/8168047")
    assert slug == "oscar" and jid == "8168047"
    slug, jid = oc.parse_apply_url("https://job-boards.greenhouse.io/cloverhealth/jobs/8203418")
    assert slug == "cloverhealth" and jid == "8203418"


def test_parse_apply_url_embed_form():
    slug, jid = oc.parse_apply_url(
        "https://boards.greenhouse.io/embed/job_app?for=cloverhealth&token=8203418")
    assert slug == "cloverhealth" and jid == "8203418"


def test_parse_apply_url_non_greenhouse_is_empty():
    assert oc.parse_apply_url("https://example.com/jobs/123") == ("", "")
    assert oc.parse_apply_url("") == ("", "")


def test_board_slug_prefers_url_then_source_map():
    # the 'clover' SOURCE maps to the 'cloverhealth' BOARD (slug != source)
    assert oc.board_slug("clover", "https://job-boards.greenhouse.io/cloverhealth/jobs/1") \
        == "cloverhealth"
    # no url -> source map fallback
    assert oc.board_slug("clover", "") == "cloverhealth"
    assert oc.board_slug("oscar", "") == "oscar"


def test_gh_job_id_from_url_then_source_id():
    assert oc.gh_job_id("https://job-boards.greenhouse.io/oscar/jobs/8168047") == "8168047"
    # no url tail -> numeric source_id fallback
    assert oc.gh_job_id("https://example.com/x", "8168047") == "8168047"
    # non-numeric source_id -> empty (never fabricate a token)
    assert oc.gh_job_id("https://example.com/x", "sha1abc") == ""


def test_embed_apply_url_shape():
    assert oc.embed_apply_url("oscar", "8168047") == \
        "https://boards.greenhouse.io/embed/job_app?for=oscar&token=8168047"


def test_embed_url_for_resolves_row():
    # oscar row
    assert oc.embed_url_for("oscar", "https://job-boards.greenhouse.io/oscar/jobs/8168047", "8168047") \
        == "https://boards.greenhouse.io/embed/job_app?for=oscar&token=8168047"
    # clover row (source 'clover' -> board 'cloverhealth')
    assert oc.embed_url_for("clover", "https://job-boards.greenhouse.io/cloverhealth/jobs/8203418",
                            "8203418") \
        == "https://boards.greenhouse.io/embed/job_app?for=cloverhealth&token=8203418"


def test_embed_url_for_falls_back_to_apply_url_when_unresolvable():
    # a non-greenhouse url with a non-numeric id can't resolve -> keep the original apply_url
    assert oc.embed_url_for("oscar", "https://example.com/apply", "sha1") == "https://example.com/apply"


# --- drafted_answers (mirror of materialize_prefill) ---------------------------------------------

def _draft():
    return {
        "answers": [
            {"label": "Are you legally authorized to work in the US?", "value": "Yes",
             "source": "identity"},
            {"label": "Résumé", "value": "demo_resume.pdf", "source": "file"},   # dropped
            {"label": "", "value": "x", "source": "none"},                        # dropped
            {"label": "Skills", "value": ["Customer Service", "Data Entry"], "source": "llm"},
        ],
        "country": "United States",
        "cover_letter": "I am excited to apply.",
        "resume": {
            "personal_info": {"location": "Columbus, Ohio"},
            "education": [{"school": "Ohio State University", "degree": "BS", "field": "Business"}],
            "experience": [{"company": "Acme Corp", "title": "CSR", "dates": "2019-2023"}],
        },
    }


def test_drafted_answers_identity_and_list_join():
    d = oc._drafted_answers(_draft(), "demo_jane_doe1")
    assert d["Are you legally authorized to work in the US?"] == "Yes"
    assert d["Skills"] == "Customer Service, Data Entry"     # list joined
    # file/none sources are excluded
    assert "Résumé" not in d


def test_drafted_answers_supplemental_typeaheads():
    d = oc._drafted_answers(_draft(), "demo_jane_doe1")
    assert d["School"] == "Ohio State University"
    assert d["Company name"] == "Acme Corp"
    assert d["Title"] == "CSR"
    # a 2-year range end date is used, not today's year
    assert d["Start date year"] == "2019"
    assert d["End date year"] == "2023"
    # city + country geo labels supplied
    assert d["City"] == "Columbus"
    assert d["Country"] == "United States"


def test_drafted_answers_never_ticks_current_role():
    d = oc._drafted_answers(_draft(), "demo_jane_doe1")
    assert "Current role" not in d          # unsatisfiable on natera-style GH forms


def test_drafted_answers_cover_letter_synthetic_only():
    d_syn = oc._drafted_answers(_draft(), "demo_jane_doe1")
    assert d_syn["Cover Letter"] == "I am excited to apply."
    d_real = oc._drafted_answers(_draft(), "michael")       # non-demo -> no auto cover letter
    assert "Cover Letter" not in d_real


# --- ack matcher ----------------------------------------------------------------------------------

def test_ack_regex_matches_greenhouse_receipts():
    assert oc._ACK_RE.search("Thank you for applying to Oscar Health")
    assert oc._ACK_RE.search("We have received your application")
    assert oc._ACK_RE.search("Your application has been submitted")
    assert not oc._ACK_RE.search("Weekly remote jobs digest")


# --- ADVANCE gate: dry run never drives the co-pilot ---------------------------------------------

def test_advance_gate_env(monkeypatch):
    monkeypatch.delenv("OSCAR_CLOVER_ADVANCE", raising=False)
    assert oc.oscar_clover_advance() is False
    monkeypatch.setenv("OSCAR_CLOVER_ADVANCE", "1")
    assert oc.oscar_clover_advance() is True
    monkeypatch.setenv("OSCAR_CLOVER_ADVANCE", "no")
    assert oc.oscar_clover_advance() is False


def test_run_dry_run_does_not_call_fill(monkeypatch):
    monkeypatch.delenv("OSCAR_CLOVER_ADVANCE", raising=False)
    called = {"n": 0}

    def _fake_fill(jid, pid, *, wait_submit):        # must NOT be invoked in a dry run
        called["n"] += 1
        return {"state": "done", "submit": {"confirmed": True}}

    # stub the persona/prefill build + DB row so the test stays network/DB-free
    monkeypatch.setattr(oc, "_build_prefill", lambda row: {
        "profile_id": "demo_x1", "jobid": "mh_17091", "embed_url": "https://boards.greenhouse.io/x",
        "slug": "oscar", "gh_id": "1", "email": "x@takhet.com", "full_name": "X Y", "n_questions": 3})

    class _Cur:
        def execute(self, *a): pass
        def fetchone(self): return (17091, "oscar", "8168047", "Oscar Health",
                                    "COB Verification Specialist",
                                    "https://job-boards.greenhouse.io/oscar/jobs/8168047")

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(oc.mail_db, "conn", lambda: _Conn())
    rep = oc.run(17091, keep_minutes=0, fill_fn=_fake_fill)
    assert rep["advanced"] is False
    assert rep["submitted"] is False
    assert called["n"] == 0                          # the co-pilot was NOT driven
    assert rep["embed_url"] == "https://boards.greenhouse.io/x"
