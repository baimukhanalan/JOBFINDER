"""Rules v2: pure tests on _match_field/_resolve_value — no browser, no LLM."""
from backend.applier.analyzer import _match_field, _resolve_value

FACTS = {
    "shifts_nights": "Yes", "shifts_weekends": "Yes", "overtime": "Yes",
    "notice_period": "Two weeks", "timezone": "CST", "typing_wpm": "65",
    "languages": ["English", "Russian"], "education_level": "Bachelor's degree",
    "salary_hourly": "20-24", "drug_test_ok": "Yes", "drivers_license": "Yes",
    "criminal_record": "No", "background_check_ok": "Yes", "quiet_workspace": "Yes",
}


def _resolve(text: str, facts: dict = FACTS):
    m = _match_field(text)
    assert m is not None, f"no rule matched: {text!r}"
    return m[0], _resolve_value(m[0], {}, "", {}, facts)


def test_convicted_resolves_no_not_yes():
    key, val = _resolve("Have you ever been convicted of a felony?")
    assert key == "_fact:criminal_record" and val == "No"


def test_criminal_history_resolves_no():
    key, val = _resolve("Do you have a criminal history?")
    assert key == "_fact:criminal_record" and val == "No"


def test_background_check_consent_yes():
    key, val = _resolve("Are you willing to undergo a background check?")
    assert key == "_fact:background_check_ok" and val == "Yes"


def test_weekend_availability():
    key, val = _resolve("Are you available to work weekends?")
    assert key == "_fact:shifts_weekends" and val == "Yes"


def test_night_shift():
    key, val = _resolve("Can you work overnight shifts?")
    assert key == "_fact:shifts_nights" and val == "Yes"


def test_education_level():
    key, val = _resolve("What is your highest level of education?")
    assert key == "_fact:education_level" and val == "Bachelor's degree"


def test_languages_list_joined():
    key, val = _resolve("What languages do you speak fluently?")
    assert key == "_fact:languages" and val == "English, Russian"


def test_typing_speed():
    key, val = _resolve("What is your typing speed (WPM)?")
    assert val == "65"


def test_hourly_rate():
    key, val = _resolve("What is your expected hourly rate?")
    assert key == "_fact:salary_hourly" and val == "20-24"


def test_missing_fact_resolves_none():
    key, val = _resolve("Are you willing to take a drug test?", facts={})
    assert key == "_fact:drug_test_ok" and val is None


def test_foreign_work_auth_still_blocked():
    assert _match_field("Are you legally authorized to work in the UK?") is None


def test_open_ended_still_unmatched():
    assert _match_field("Describe a time you handled an angry customer") is None


def test_identity_rules_untouched():
    key, _ = _resolve("First Name")
    assert key == "_first_name"


def test_composite_start_date_label_prefers_start_date():
    key, _ = _resolve("Start date and notice period")
    assert key == "_start_date"


def test_pure_notice_period_still_fact():
    key, val = _resolve("Notice period")
    assert key == "_fact:notice_period" and val == "Two weeks"


def test_company_name_with_saturday_not_weekend_rule():
    m = _match_field("Have you worked at Saturday Night Technologies before?")
    assert m is None or m[0] != "_fact:shifts_weekends"


def test_overnight_shipping_not_night_shift():
    m = _match_field("Do you handle overnight shipping?")
    assert m is None or m[0] != "_fact:shifts_nights"


def test_spoken_languages_label():
    key, _ = _resolve("Spoken languages")
    assert key == "_fact:languages"


def test_boolean_fact_becomes_yes_no():
    from backend.applier.analyzer import _resolve_value
    assert _resolve_value("_fact:drug_test_ok", {}, "", {}, {"drug_test_ok": True}) == "Yes"
    assert _resolve_value("_fact:drug_test_ok", {}, "", {}, {"drug_test_ok": False}) == "No"


def test_open_ended_with_rule_keywords_stays_open():
    assert _match_field("Describe a time you worked overtime") is None
    assert _match_field("Tell us about your weekend availability") is None
    assert _match_field("Explain your notice period") is None


def test_known_answer_single_common_word_rejected():
    from backend.applier.analyzer import _known_answer_matches
    assert not _known_answer_matches("Why do you want to work here?",
                                     "years of work experience")


def test_known_answer_real_overlap_accepted():
    from backend.applier.analyzer import _known_answer_matches
    assert _known_answer_matches("Why do you want to work here?",
                                 "why do you want to work at acme")


# --- A3: question-text sanitizer (_clean_text) ---

def test_clean_text_strips_uuid():
    from backend.applier.analyzer import _clean_text
    t = _clean_text("Briefly describe your relevant experience "
                    "f8f08c1c-998e-4c5d-9a1b-1234567890ab f8f08c1c-998e-4c5d-9a1b-1234567890ab")
    assert "f8f08c1c" not in t
    assert "Briefly describe your relevant experience" in t


def test_clean_text_strips_workable_ids_and_opaque_tokens():
    from backend.applier.analyzer import _clean_text
    assert _clean_text("QA_11864454 vrROcNoYwNhErrl6 Do you have a webcam?") == \
        "Do you have a webcam?"


def test_clean_text_strips_date_placeholder_and_ca_id():
    from backend.applier.analyzer import _clean_text
    assert _clean_text("MM/DD/YYYY CA_21641 Start date") == "Start date"


def test_clean_text_strips_lever_cards_and_greenhouse_question_ids():
    from backend.applier.analyzer import _clean_text
    assert _clean_text("cards[abc123][field0] Do you require sponsorship?") == \
        "Do you require sponsorship?"
    assert _clean_text("LinkedIn Profile question_61781728 LinkedIn Profile") == \
        "LinkedIn Profile"


def test_clean_text_strips_type_here_placeholder():
    from backend.applier.analyzer import _clean_text
    assert "Type here" not in _clean_text("Tell us about yourself Type here...")


def test_clean_text_collapses_duplicated_question():
    from backend.applier.analyzer import _clean_text
    assert _clean_text("Which shift do you prefer? Which shift do you prefer?") == \
        "Which shift do you prefer?"


def test_clean_text_collapses_adjacent_identical_parts():
    # merged radio groups carry the question in BOTH label and nearbyText —
    # the duplicate part must not survive into the display text
    from backend.applier.analyzer import _clean_text
    assert _clean_text("He/him pronouns He/him pronouns") == "He/him pronouns"


def test_clean_text_short_residue_falls_back_to_raw():
    from backend.applier.analyzer import _clean_text
    # everything stripped -> too short to be a question, keep raw so the human sees SOMETHING
    assert _clean_text("217238 Yes") == "217238 Yes"


def test_clean_text_keeps_long_english_words():
    # 14+ char REAL words (no digits) must survive the opaque-token rule
    from backend.applier.analyzer import _clean_text
    assert _clean_text("Describe your responsibilities") == "Describe your responsibilities"


def test_clean_text_keeps_select_verb_inside_sentences():
    # 'select' is a VERB here, not the dropdown placeholder — must never be stripped
    from backend.applier.analyzer import _clean_text
    assert _clean_text("Please select your preferred shift") == \
        "Please select your preferred shift"
    assert _clean_text("Select all that apply") == "Select all that apply"


def test_clean_text_select_placeholder_sentinel_stripped():
    # 'Select...' / bare 'Select' / 'Select one' ARE placeholders: stripped from
    # mixed text; placeholder-only input falls back to raw (same policy as ids)
    from backend.applier.analyzer import _clean_text
    assert _clean_text("Which shift do you prefer? Select...") == \
        "Which shift do you prefer?"
    assert _clean_text("Select...") == "Select..."          # length fallback -> raw
    assert _clean_text("Select") == "Select"                # bare sentinel -> raw
    assert _clean_text("Select one") == "Select one"        # bare sentinel -> raw


# --- A6: '\bstate\b' must not fire on verb usage ---

def test_state_verb_not_state_field():
    m = _match_field("Please state your salary expectations")
    assert m is not None and m[0] == "_salary"


def test_state_label_still_matches():
    assert _match_field("State")[0] == "_state"
    assert _match_field("State of residence")[0] == "_state"
    assert _match_field("Province")[0] == "_state"


# --- Resume/CV pattern: word-bounded (bare 'cv' matched Workable opaque ids) ---

def test_resume_cv_labels_still_match():
    assert _match_field("Resume/CV")[0] == "_resume"
    assert _match_field("Upload your resume")[0] == "_resume"
    assert _match_field("Curriculum Vitae")[0] == "_resume"


def test_underscore_joined_resume_names_still_match():
    # plain \b would miss these: '_' is a word char, so \bresume\b never fires
    assert _match_field("_systemfield_resume")[0] == "_resume"
    assert _match_field("resume_upload")[0] == "_resume"


def test_cv_inside_opaque_token_not_resume():
    # Workable random ids ('elQuzcvxczyuwMep') matched the old bare 'cv' ->
    # 22 thirty-second set_input_files timeouts on QA_/CA_ radio questions live
    m = _match_field("QA_11864455 elQuzcvxczyuwMep")
    assert m is None or m[0] != "_resume"


def test_curriculum_alone_not_resume():
    # 'curriculum development' (teaching JDs) is not a CV upload
    m = _match_field("Curriculum development experience")
    assert m is None or m[0] != "_resume"


# --- Ashby bare "Name" -> full_name (name left empty since the 06-14 cleanup) ---

def test_bare_name_label_is_full_name():
    assert _match_field("Name")[0] == "full_name"


def test_ashby_systemfield_name_is_full_name():
    # label 'Name' + name/id '_systemfield_name' exactly as the analyzer joins them
    assert _match_field("Name _systemfield_name _systemfield_name")[0] == "full_name"


def test_company_and_manager_name_not_hijacked():
    for text in ("Company name", "Manager name", "Name of your previous employer"):
        m = _match_field(text)
        assert m is None or m[0] != "full_name", text


def test_first_last_name_precedence_kept():
    assert _match_field("First name")[0] == "_first_name"
    assert _match_field("Last name")[0] == "_last_name"
