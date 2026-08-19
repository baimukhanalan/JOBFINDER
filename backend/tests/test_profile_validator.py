from backend.applier.profile_validator import is_submittable, validate_profile


def _profile(**over):
    base = {"full_name": "Jane Roe", "phone": "(512) 209-4417",
            "email": "jane@realmail.com"}
    base.update(over)
    return base


def test_real_contact_data_passes():
    assert validate_profile(_profile()) == []
    assert is_submittable(_profile())


def test_fictional_555_01xx_phone_blocks():
    for phone in ["(512) 555-0186", "512-555-0100", "512.555.0199", "5125550142"]:
        problems = validate_profile(_profile(phone=phone))
        assert problems, phone
        assert any("555-01" in p or "fictional" in p for p in problems)


def test_ordinary_555_number_is_not_blocked():
    # only the 0100-0199 block is reserved-fictional
    assert validate_profile(_profile(phone="(512) 555-2368")) == []


def test_placeholder_email_blocks():
    assert validate_profile(_profile(email="someone@example.com"))
    assert validate_profile(_profile(email="x@sample.io"))


def test_missing_fields_block():
    assert validate_profile(_profile(phone=""))
    assert validate_profile(_profile(email=""))
    assert validate_profile(_profile(full_name=""))
