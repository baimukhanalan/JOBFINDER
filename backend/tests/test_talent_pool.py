"""Network-free tests for the talent-pool résumé-DROP lane (`talent_pool_recon`).

Locks the reverse-engineered Randstad `join_randstad` webform payload (captured live 2026-09-22),
the page-parsing (dropzone token + invisible-reCAPTCHA widget), and the ack matcher — so a future
site change is caught here. No live HTTP.
"""
from backend.tools import talent_pool_recon as tpr


# --- name / field helpers -------------------------------------------------------------------------

def test_split_name():
    assert tpr.split_name("Mary Watson") == ("Mary", "Watson")
    assert tpr.split_name("Mary Jane Watson") == ("Mary", "Jane Watson")  # middle+last kept as last
    assert tpr.split_name("Cher") == ("Cher", "Cher")
    assert tpr.split_name("") == ("", "")


def test_phone_digits_formats_us():
    assert tpr.phone_digits("(614) 555-0123") == "(614) 555-0123"
    assert tpr.phone_digits("+1 614-555-0123") == "(614) 555-0123"
    assert tpr.phone_digits("6145550123") == "(614) 555-0123"
    assert tpr.phone_digits("bad") == "bad"


def test_default_location_uses_city_state_abbr():
    assert tpr.default_job_location({"city": "Columbus", "state": "Ohio"}) == "Columbus, OH"
    assert tpr.default_job_location({"city": "Austin", "state": "TX"}) == "Austin, TX"
    assert tpr.default_job_location({"location": "Remote"}) == "Remote"


def test_default_title_prefers_persona_else_csr():
    assert tpr.default_job_title({"title": "Support Specialist"}) == "Support Specialist"
    assert tpr.default_job_title({"experience": [{"title": "Call Center Agent"}]}) == "Call Center Agent"
    assert tpr.default_job_title({}) == "Customer Service Representative"


# --- Randstad payload shape (the captured contract) -----------------------------------------------

def _persona():
    return {"full_name": "Recon Tester", "email": "recon.tester999@takhet.com",
            "phone": "(614) 555-0123", "city": "Columbus", "state": "Ohio", "zip": "43215",
            "title": "Customer Service Representative"}


def test_build_randstad_form_matches_captured_shape():
    f = tpr.build_randstad_form(_persona())
    assert f["first_name"] == "Recon"
    assert f["last_name"] == "Tester"
    assert f["email_address"] == "recon.tester999@takhet.com"
    assert f["job_location"] == "Columbus, OH"
    assert f["job_title"] == "Customer Service Representative"
    assert f["phone_number"] == "(614) 555-0123"
    assert f["webform_id"] == "join_randstad"
    assert f["op"] == "join randstad"
    assert f["validation_input"] == ""
    assert f["resume[uploaded_files]"] == ""            # empty in a dry run
    assert "sms_consent[true]" not in f                 # unchecked by default
    assert "cms_captcha" not in f                       # no token in a dry run


def test_build_randstad_form_splits_full_name_when_no_first_last():
    f = tpr.build_randstad_form({"full_name": "Mary Jane Watson", "email": "m@x.com",
                                 "city": "Austin", "state": "TX"})
    assert f["first_name"] == "Mary"
    assert f["last_name"] == "Jane Watson"


def test_build_randstad_form_carries_file_id_token_and_sms():
    f = tpr.build_randstad_form(_persona(), resume_file_id="fid-42",
                                recaptcha_token="03AF...tok", sms_consent=True)
    assert f["resume[uploaded_files]"] == "fid-42"
    assert f["cms_captcha"] == "03AF...tok"
    assert f["g-recaptcha-response"] == "03AF...tok"
    assert f["sms_consent[true]"] == "true"


def test_missing_fields_flags_incomplete_persona():
    assert tpr.randstad_missing_fields(tpr.build_randstad_form(_persona())) == []
    bare = tpr.build_randstad_form({"full_name": "", "email": ""})
    assert set(tpr.randstad_missing_fields(bare)) >= {"first_name", "last_name", "email_address"}


# --- page parsing ---------------------------------------------------------------------------------

_PAGE = (
    '<form action="/api/form/submit" method="post">'
    '<input name="first_name" required type="text">'
    '<input data-upload-path="/dropzonejs/upload?token=ABC123def" data-drupal-selector="x">'
    '<div data-captcha-widget-id="cms_captcha" data-size="invisible" class="captcha"></div>'
    '<input name="webform_id" type="hidden" value="join_randstad">'
    '</form>'
    '<script>grecaptcha.render(e,{sitekey:"6LcAbCdEfGhIjKlMnOpQrStUvWxYz0123456789A"})</script>'
)


def test_parse_upload_path():
    assert tpr.parse_upload_path(_PAGE) == "/dropzonejs/upload?token=ABC123def"
    assert tpr.parse_upload_path("<div>no dropzone</div>") is None


def test_parse_captcha_static_html_recaptcha_scripts():
    # a page whose only captcha signal is grecaptcha scripts (no FriendlyCaptcha render classes)
    c = tpr.parse_captcha(_PAGE)
    assert c["present"] is True
    assert c["widget_id"] == "cms_captcha"
    assert c["size"] == "invisible"
    assert c["kind"] == "recaptcha_v2_invisible"
    assert c["sitekey"] == "6LcAbCdEfGhIjKlMnOpQrStUvWxYz0123456789A"


def test_parse_captcha_detects_friendly_from_live_render():
    # the LIVE Randstad page renders the cms_captcha div as FriendlyCaptcha (confirmed 2026-09-22)
    live = ('<div data-captcha-widget-id="cms_captcha" data-size="invisible" '
            'class="webform-element-width captcha bluex-friendly-captcha">Anti-Robot Verification</div>'
            '<script src="https://cdn.jsdelivr.net/npm/friendly-challenge/widget.min.js"></script>')
    c = tpr.parse_captcha(live)
    assert c["present"] is True
    assert c["kind"] == "friendly_captcha"     # FriendlyCaptcha wins over the bare widget div
    assert c["widget_id"] == "cms_captcha"


def test_parse_captcha_absent():
    c = tpr.parse_captcha("<form>no captcha</form>")
    assert c["present"] is False
    assert c["kind"] is None


# --- ack matcher ----------------------------------------------------------------------------------

def test_randstad_ack_positive():
    assert tpr.randstad_ack(200, "Thank you! We'll be in touch.") is True
    assert tpr.randstad_ack(200, '{"redirect":"/thank-you","settings":{}}') is True


def test_randstad_ack_rejects_errors_and_non200():
    assert tpr.randstad_ack(200, "Please complete the reCAPTCHA verification.") is False
    assert tpr.randstad_ack(200, '{"messages":{"error":["captcha invalid"]}}') is False
    assert tpr.randstad_ack(403, "Thank you") is False
    assert tpr.randstad_ack(500, "") is False


# --- registry / recon verdicts --------------------------------------------------------------------

def test_registry_verdicts():
    # Randstad is the one viable server-side résumé drop
    assert tpr.viable_pools() == ["randstad"]
    r = tpr.POOLS["randstad"]
    assert r["reachable_serverside"] and r["resume_drop"] and not r["account_required"]
    assert r["captcha"] == "friendly_captcha"   # live wall = FriendlyCaptcha, NopeCHA can't solve it
    # every other surveyed pool is walled / no-résumé / captcha-lead-form
    for k in ("kelly", "adecco", "roberthalf", "ttec", "teleperformance", "concentrix", "foundever"):
        assert tpr.POOLS[k]["viable"] is False
    # BPO "talent communities" that are reachable but résumé-less are marked as such
    assert tpr.POOLS["foundever"]["reachable_serverside"] and not tpr.POOLS["foundever"]["resume_drop"]
    assert tpr.POOLS["teleperformance"]["reachable_serverside"] and not tpr.POOLS["teleperformance"]["resume_drop"]
