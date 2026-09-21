"""Network-free tests for the ManpowerGroup (Manpower/Experis) guest-apply lane.

Locks the reverse-engineered `JobApplyWithEmail` request shape (captured live 2026-09-21) so a future
site change that breaks the payload is caught here, and proves the MANPOWER_ADVANCE gate transmits
nothing when off. No live HTTP — `submit()` is driven with advance=False (dry run) + an injected
fake client.
"""
import json

from backend.applier.strategies import manpower


# --- pure payload shaping -------------------------------------------------------------------------

def _persona():
    return {"full_name": "Recon Tester", "email": "recon.tester999@takhet.com",
            "phone": "(614) 555-0123", "city": "Columbus", "state": "Ohio", "zip": "43215",
            "country": "United States",
            "resume": {"skills_grouped": {"Skills": ["Customer Service", "Data Entry"]}}}


def test_phone_local_strips_to_10_digits():
    assert manpower.phone_local("(614) 555-0123") == "6145550123"
    assert manpower.phone_local("+1 614-555-0123") == "6145550123"
    assert manpower.phone_local("16145550123") == "6145550123"
    assert manpower.phone_local("") == ""


def test_build_profile_data_matches_captured_shape():
    pd = manpower.build_profile_data(_persona())
    # exact captured structure (2026-09-21)
    assert pd["Consent"] == {"NAConsentCheck": "false"}
    pi = pd["PersonalInfo"]
    assert pi["firstName"] == "Recon"
    assert pi["lastName"] == "Tester"
    assert pi["email"] == "recon.tester999@takhet.com"
    assert pi["personalContact"] == "6145550123"       # bare 10-digit
    assert pi["country"] == "United States"
    assert pi["address"] == {"city": "Columbus", "state": "Ohio", "zip": "43215"}
    assert pd["EditExpertiseAndSkills"]["skills"] == ["Customer Service", "Data Entry"]


def test_skills_are_never_empty():
    # the apply form requires >=1 skill; a persona with no résumé skills falls back to a CSR default
    pd = manpower.build_profile_data({"full_name": "A B", "email": "a@b.com", "phone": "6145550123",
                                      "city": "X", "state": "Ohio", "zip": "43215"})
    assert pd["EditExpertiseAndSkills"]["skills"] == ["Customer Service"]


def test_build_profile_data_splits_first_last_from_full_name():
    pd = manpower.build_profile_data({"full_name": "Mary Jane Watson", "email": "m@x.com",
                                      "phone": "6145550123", "zip": "43215", "state": "Ohio",
                                      "city": "Columbus"})
    assert pd["PersonalInfo"]["firstName"] == "Mary"
    assert pd["PersonalInfo"]["lastName"] == "Watson"


def test_build_job_details_shape():
    jd = manpower.build_job_details("5877370", "a51b640c-a127-4d29-b1e7-feb900a98d71")
    assert jd["jobId"] == "5877370"
    assert jd["jobItemID"] == "a51b640c-a127-4d29-b1e7-feb900a98d71"
    assert jd["referer"] == "direct"
    assert jd["utmSource"] == "" and jd["utmCampaign"] == ""


def test_is_success_and_entity_id():
    assert manpower.is_success({"status": 1000}) is True
    assert manpower.is_success({"data": {"status": 1000}}) is True
    assert manpower.is_success({"status": 0}) is False        # the garbage-body / rejected response
    assert manpower.is_success({}) is False
    assert manpower.entity_id({"data": {"entityID": "abc"}}) == "abc"
    assert manpower.entity_id({"changedEntityId": "xyz"}) == "xyz"


def test_extract_job_item_id_prefers_the_one_next_to_the_jobid():
    html = ('...{"jobID":"5877370","x":1,"jobItemID":"a51b640c-a127-4d29-b1e7-feb900a98d71"}...'
            '{"jobID":"999","jobItemID":"deadbeef-0000-0000-0000-000000000000"}')
    assert manpower.extract_job_item_id(html, "5877370") == "a51b640c-a127-4d29-b1e7-feb900a98d71"
    # unknown jobid -> first GUID fallback
    assert manpower.extract_job_item_id('"jobItemID":"11111111-2222-3333-4444-555566667777"', "0") \
        == "11111111-2222-3333-4444-555566667777"
    assert manpower.extract_job_item_id("", "5877370") is None


def test_apply_host_and_url_per_brand():
    assert manpower.apply_host("manpower") == "www.manpower.com"
    assert manpower.apply_host("experis") == "www.experis.com"
    assert manpower.apply_url("experis") == \
        "https://www.experis.com/api/services/Applicant/JobApplyWithEmail"


# --- submit(): gate + wire shape (no live HTTP) ---------------------------------------------------

class _FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


class _FakeClient:
    """Records the POST args so we can assert the exact multipart the lane would send."""
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def post(self, url, files=None, headers=None, timeout=None):
        self.calls.append({"url": url, "files": files, "headers": headers})
        return _FakeResp(self._payload)


def test_submit_dry_run_transmits_nothing():
    fake = _FakeClient({"status": 1000})
    rep = manpower.submit("manpower", "5877370", "a51b640c-a127-4d29-b1e7-feb900a98d71",
                          _persona(), client=fake, advance=False)
    assert rep["advanced"] is False
    assert rep["submitted"] is False
    assert fake.calls == []                                  # NOT posted
    assert rep["profile_data"]["PersonalInfo"]["firstName"] == "Recon"   # payload still built
    assert rep["job_details"]["jobId"] == "5877370"


def test_submit_advance_posts_the_captured_multipart_and_reads_success():
    fake = _FakeClient({"status": 1000, "data": {"entityID": "APP123"}})
    rep = manpower.submit("manpower", "5877370", "a51b640c-a127-4d29-b1e7-feb900a98d71",
                          _persona(), client=fake, advance=True)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://www.manpower.com/api/services/Applicant/JobApplyWithEmail"
    # multipart string parts (no filename), exactly two: profileData + jobDetails
    assert set(call["files"].keys()) == {"profileData", "jobDetails"}
    assert call["files"]["profileData"][0] is None
    prof = json.loads(call["files"]["profileData"][1])
    assert prof["PersonalInfo"]["email"] == "recon.tester999@takhet.com"
    jd = json.loads(call["files"]["jobDetails"][1])
    assert jd["jobItemID"] == "a51b640c-a127-4d29-b1e7-feb900a98d71"
    assert call["headers"]["Origin"] == "https://www.manpower.com"
    assert "jobapply?id=a51b640c" in call["headers"]["Referer"]
    assert rep["success"] is True
    assert rep["submitted"] is True
    assert rep["entity_id"] == "APP123"


def test_submit_missing_jobitemid_is_guarded():
    fake = _FakeClient({"status": 1000})
    rep = manpower.submit("manpower", "5877370", "", _persona(), client=fake, advance=True)
    assert rep["submitted"] is False
    assert fake.calls == []
    assert "jobItemID" in rep["note"]


def test_submit_missing_persona_fields_is_guarded():
    fake = _FakeClient({"status": 1000})
    rep = manpower.submit("manpower", "5877370", "guid-here",
                          {"full_name": "", "email": "", "zip": ""}, client=fake, advance=True)
    assert rep["submitted"] is False
    assert fake.calls == []
    assert "missing" in rep["note"]


def test_submit_reports_failure_status():
    fake = _FakeClient({"status": 0, "message": "invalid"})
    rep = manpower.submit("manpower", "5877370", "guid-here", _persona(), client=fake, advance=True)
    assert rep["submitted"] is True
    assert rep["success"] is False
    assert rep["api_status"] == 0
