"""_merge_radio_groups: collapse same-name radio/checkbox inputs into one question."""
import asyncio

from backend.applier import analyzer
from backend.applier.analyzer import _field_selector, _merge_radio_groups


def _radio(name, sel, label="", nearby="", value=""):
    return {"selector": sel, "tag": "input", "type": "radio", "label": label,
            "ariaLabel": "", "placeholder": "", "name": name, "id": "",
            "title": "", "nearbyText": nearby, "required": True,
            "options": [], "value": value}


def test_merges_same_name_radios_into_group():
    fields = [
        _radio("shift", '[id="r1"]', label="Day", nearby="Which shift do you prefer?"),
        _radio("shift", '[id="r2"]', label="Night", nearby="Which shift do you prefer?"),
        {"selector": '[id="em"]', "tag": "input", "type": "email", "label": "Email",
         "ariaLabel": "", "placeholder": "", "name": "email", "id": "em", "title": "",
         "nearbyText": "", "required": True, "options": [], "value": ""},
    ]
    out = _merge_radio_groups(fields)
    groups = [f for f in out if f["type"] == "radio_group"]
    assert len(groups) == 1
    g = groups[0]
    assert g["label"] == "Which shift do you prefer?"
    assert [o["text"] for o in g["options"]] == ["Day", "Night"]
    assert [o["value"] for o in g["options"]] == ['[id="r1"]', '[id="r2"]']
    assert any(f["type"] == "email" for f in out)  # non-radios pass through


def test_option_text_falls_back_to_value_attr():
    fields = [_radio("q1", '[id="a"]', value="Yes"), _radio("q1", '[id="b"]', value="No")]
    g = _merge_radio_groups(fields)[0]
    assert [o["text"] for o in g["options"]] == ["Yes", "No"]


def test_single_unnamed_radio_passes_through():
    fields = [_radio("", '[id="solo"]', label="I agree")]
    out = _merge_radio_groups(fields)
    assert out[0]["type"] == "radio"


def test_checkbox_group_merged_too():
    fields = [
        {**_radio("days", '[id="c1"]', label="Mon"), "type": "checkbox"},
        {**_radio("days", '[id="c2"]', label="Tue"), "type": "checkbox"},
    ]
    g = _merge_radio_groups(fields)[0]
    assert g["type"] == "checkbox_group"
    assert [o["text"] for o in g["options"]] == ["Mon", "Tue"]


def test_single_checkbox_not_grouped():
    fields = [{**_radio("tos", '[id="t"]', label="I accept the terms"), "type": "checkbox"}]
    assert _merge_radio_groups(fields)[0]["type"] == "checkbox"


def test_group_keeps_dom_position():
    fields = [
        {"selector": '[id="fn"]', "tag": "input", "type": "text", "label": "First name",
         "ariaLabel": "", "placeholder": "", "name": "fn", "id": "fn", "title": "",
         "nearbyText": "", "required": True, "options": [], "value": ""},
        _radio("shift", '[id="r1"]', label="Day", nearby="Shift?"),
        _radio("shift", '[id="r2"]', label="Night", nearby="Shift?"),
        {"selector": '[id="em"]', "tag": "input", "type": "email", "label": "Email",
         "ariaLabel": "", "placeholder": "", "name": "email", "id": "em", "title": "",
         "nearbyText": "", "required": True, "options": [], "value": ""},
    ]
    out = _merge_radio_groups(fields)
    assert [f["type"] for f in out] == ["text", "radio_group", "email"]


def test_empty_option_text_gets_placeholder():
    fields = [_radio("q", '[id="a"]'), _radio("q", '[id="b"]')]
    g = _merge_radio_groups(fields)[0]
    assert g["options"][0]["text"] == "[option 1]"
    assert g["options"][1]["text"] == "[option 2]"


def test_none_of_the_above_not_picked_as_no():
    from backend.applier.analyzer import _pick_option
    options = [{"value": "[id='a']", "text": "None of the above"},
               {"value": "[id='b']", "text": "No, I do not require sponsorship"}]
    opt = _pick_option(options, "No", "_sponsorship")
    assert opt is not None and opt["value"] == "[id='b']"


# --- A1: selector building (id-less radio/checkbox groups must get DISTINCT selectors) ---

def test_selector_id_wins_over_name_for_text_inputs():
    assert _field_selector("the-id", "n", "", "text", "", 0) == '[id="the-id"]'


def test_selector_radio_name_value_beats_id():
    # Live failures forced this policy for radio/checkbox:
    #  - Workable regenerates random ids ('elQuzcvxczyuwMep') on every React
    #    re-render (the résumé upload triggers one) -> id selectors go stale;
    #  - Ashby gives every option of a group the SAME id -> id always hit
    #    option #1.
    # name+value (or name+nth) is unique within the group and survives.
    assert _field_selector("elQuzcvxczyuwMep", "QA_11864455", "", "radio", "true", 0) \
        == '[name="QA_11864455"][value="true"]'
    assert _field_selector("h0ox3YaL1xfICB5E", "217238", "", "checkbox", "", 2) \
        == '[name="217238"] >> nth=2'
    assert _field_selector("70b75ebf-0000-0000-a8c0-67af9910ece7_693",
                           "70b75ebf-0000-0000-a8c0-67af9910ece7_693", "",
                           "radio", "", 3) \
        == '[name="70b75ebf-0000-0000-a8c0-67af9910ece7_693"] >> nth=3'
    # nameless radio still falls back to its id
    assert _field_selector("customPronounsOption", "", "", "checkbox", "Custom", 0) \
        == '[id="customPronounsOption"]'


def test_selector_id_with_quote_falls_through_to_name():
    assert _field_selector('a"b', "nm", "", "text", "", 0) == '[name="nm"]'


def test_selector_same_name_radios_with_values_are_distinct():
    s1 = _field_selector("", "sponsor", "", "radio", "Yes", 0)
    s2 = _field_selector("", "sponsor", "", "radio", "No", 0)
    assert s1 == '[name="sponsor"][value="Yes"]'
    assert s2 == '[name="sponsor"][value="No"]'
    assert s1 != s2


def test_selector_valueless_same_name_radios_get_nth():
    s1 = _field_selector("", "shift", "", "radio", "", 0)
    s2 = _field_selector("", "shift", "", "radio", "", 1)
    assert s1 == '[name="shift"] >> nth=0'
    assert s2 == '[name="shift"] >> nth=1'
    assert s1 != s2


def test_selector_checkbox_value_with_quote_falls_back_to_nth():
    assert _field_selector("", "q", "", "checkbox", 'say "hi"', 2) == '[name="q"] >> nth=2'


def test_selector_text_input_name_stays_unqualified():
    assert _field_selector("", "email", "", "text", "", 0) == '[name="email"]'


def test_selector_aria_label_fallback():
    assert _field_selector("", "", "Phone", "text", "", 0) == '[aria-label="Phone"]'


def test_selector_nothing_usable_returns_empty():
    assert _field_selector("", "", "", "text", "", 0) == ""


# --- A8: Workable-style radios with DISTINCT names per option group by nearbyText ---

def test_distinct_name_radios_grouped_by_nearby_text():
    fields = [
        _radio("217238", '[id="a"]', label="Yes", nearby="Do you require visa sponsorship?"),
        _radio("217239", '[id="b"]', label="No", nearby="Do you require visa sponsorship?"),
    ]
    out = _merge_radio_groups(fields)
    assert len(out) == 1
    g = out[0]
    assert g["type"] == "radio_group"
    assert g["label"] == "Do you require visa sponsorship?"
    assert [o["text"] for o in g["options"]] == ["Yes", "No"]
    assert [o["value"] for o in g["options"]] == ['[id="a"]', '[id="b"]']


def test_distinct_name_checkboxes_not_grouped_by_nearby_text():
    # same-nearby checkboxes are commonly independent consents -> never merged
    fields = [
        {**_radio("c1", '[id="a"]', label="Email me", nearby="Communication preferences"),
         "type": "checkbox"},
        {**_radio("c2", '[id="b"]', label="Text me", nearby="Communication preferences"),
         "type": "checkbox"},
    ]
    out = _merge_radio_groups(fields)
    assert [f["type"] for f in out] == ["checkbox", "checkbox"]


# --- Workable: distinct-name options inside ONE explicit ARIA group container ---

def test_distinct_name_checkboxes_in_aria_group_merged():
    # Workable "Open to relocate": a Yes/No pair rendered as two CHECKBOXES with
    # distinct names (217238/217239) inside one div[role=group] — one question.
    fields = [
        {**_radio("217238", '[name="217238"] >> nth=0', nearby="Open to relocate"),
         "type": "checkbox", "groupKey": "YIPcxOTP3Yf6CI13_label", "optionLabel": "Yes"},
        {**_radio("217239", '[name="217239"] >> nth=0', nearby="Open to relocate"),
         "type": "checkbox", "groupKey": "YIPcxOTP3Yf6CI13_label", "optionLabel": "No"},
    ]
    out = _merge_radio_groups(fields)
    assert len(out) == 1
    g = out[0]
    assert g["type"] == "checkbox_group"
    assert g["label"] == "Open to relocate"
    assert [o["text"] for o in g["options"]] == ["Yes", "No"]
    assert [o["value"] for o in g["options"]] == \
        ['[name="217238"] >> nth=0', '[name="217239"] >> nth=0']


def test_different_aria_groups_not_cross_merged():
    fields = [
        {**_radio("1", '[id="a"]', nearby="Q1"), "type": "checkbox", "groupKey": "g1"},
        {**_radio("2", '[id="b"]', nearby="Q2"), "type": "checkbox", "groupKey": "g2"},
    ]
    out = _merge_radio_groups(fields)
    assert [f["type"] for f in out] == ["checkbox", "checkbox"]


def test_option_label_beats_internal_value_in_group_options():
    # Workable radio values are internal ids ('217240') — the resolved option
    # label ('Yes') must win as the option text
    fields = [
        {**_radio("CA_21646", '[id="r1"]', nearby="Willing to negotiate?", value="217240"),
         "optionLabel": "Yes"},
        {**_radio("CA_21646", '[id="r2"]', nearby="Willing to negotiate?", value="217241"),
         "optionLabel": "No"},
    ]
    g = _merge_radio_groups(fields)[0]
    assert [o["text"] for o in g["options"]] == ["Yes", "No"]


def test_distinct_name_radios_without_nearby_text_not_grouped():
    fields = [_radio("n1", '[id="a"]', label="Yes"), _radio("n2", '[id="b"]', label="No")]
    out = _merge_radio_groups(fields)
    assert [f["type"] for f in out] == ["radio", "radio"]


def test_nearby_grouping_keeps_position_and_other_fields():
    fields = [
        {"selector": '[id="fn"]', "tag": "input", "type": "text", "label": "First name",
         "ariaLabel": "", "placeholder": "", "name": "fn", "id": "fn", "title": "",
         "nearbyText": "", "required": True, "options": [], "value": ""},
        _radio("q1a", '[id="a"]', label="Yes", nearby="Are you over 18?"),
        _radio("q1b", '[id="b"]', label="No", nearby="Are you over 18?"),
        {"selector": '[id="em"]', "tag": "input", "type": "email", "label": "Email",
         "ariaLabel": "", "placeholder": "", "name": "email", "id": "em", "title": "",
         "nearbyText": "", "required": True, "options": [], "value": ""},
    ]
    out = _merge_radio_groups(fields)
    assert [f["type"] for f in out] == ["text", "radio_group", "email"]


def test_same_name_pass_still_wins_over_nearby_pass():
    # same-name radios merge in pass 1; pass 2 must not re-group or duplicate them
    fields = [
        _radio("shift", '[id="r1"]', label="Day", nearby="Which shift do you prefer?"),
        _radio("shift", '[id="r2"]', label="Night", nearby="Which shift do you prefer?"),
    ]
    out = _merge_radio_groups(fields)
    assert len(out) == 1 and out[0]["type"] == "radio_group"
    assert len(out[0]["options"]) == 2


# --- __UPLOAD__ never lands on a radio group; A7 fallback only suppressed by a
#     genuinely planned file upload (live Workable: CV radio groups inherited the
#     group's radio selector -> 30s set_input_files timeouts, resume never attached) ---

PROFILE = {"full_name": "Jordan Sample", "resume_path": "/tmp/resume.pdf"}


def _field(sel, ftype="text", tag="input", label="", nearby="", name="",
           required=True, options=None):
    return {"selector": sel, "tag": tag, "type": ftype, "label": label,
            "ariaLabel": "", "placeholder": "", "name": name, "id": "",
            "title": "", "nearbyText": nearby, "required": required,
            "options": options or [], "value": ""}


def _cv_group():
    return _field('[name="QA_1"][value="217240"]', ftype="radio_group",
                  label="Do you have an updated CV?",
                  nearby="Do you have an updated CV?", name="QA_1",
                  options=[{"value": '[name="QA_1"][value="217240"]', "text": "Yes"},
                           {"value": '[name="QA_1"][value="217241"]', "text": "No"}])


def _analyze(monkeypatch, raw_fields, profile):
    """Run analyze_page over pre-merged fields — no browser, page stubbed out."""
    async def fake_detect(page):
        return "application_form"

    async def fake_extract(page):
        return raw_fields

    async def fake_submit(page):
        return None

    monkeypatch.setattr(analyzer, "detect_page_type", fake_detect)
    monkeypatch.setattr(analyzer, "extract_form_fields", fake_extract)
    monkeypatch.setattr(analyzer, "find_submit_button", fake_submit)
    return asyncio.run(analyzer.analyze_page(None, profile, "", {}, {}))


def test_cv_radio_group_planned_as_choice_not_upload(monkeypatch):
    result = _analyze(monkeypatch, [_cv_group()], PROFILE)
    assert not any(f["action"] == "upload" for f in result["fields"])
    rg = [u for u in result["unknown_questions"] if u["type"] == "radio_group"]
    assert len(rg) == 1
    assert rg[0]["options"] == ["Yes", "No"]  # choice engine gets the options
    assert rg[0]["option_selectors"] == [o["value"] for o in _cv_group()["options"]]


def test_file_resume_input_planned_as_upload(monkeypatch):
    f = _field('[id="resume"]', ftype="file", label="Resume/CV", name="resume")
    result = _analyze(monkeypatch, [f], PROFILE)
    ups = [x for x in result["fields"] if x["action"] == "upload"]
    assert len(ups) == 1
    assert ups[0]["selector"] == '[id="resume"]'
    assert ups[0]["value"] == "/tmp/resume.pdf"
    assert ups[0]["matched"] == "resume"


def test_a7_fallback_not_suppressed_by_cv_radio_group(monkeypatch):
    # live failure: the CV radio group "matched" resume -> A7 stayed silent ->
    # the real (opaque-named) file input never got the résumé
    raw = [_cv_group(),
           _field('[id="input_files_input_Xj2"]', ftype="file", name="ZzMqm7mZ")]
    result = _analyze(monkeypatch, raw, PROFILE)
    ups = [x for x in result["fields"] if x["action"] == "upload"]
    assert len(ups) == 1
    assert ups[0]["selector"] == '[id="input_files_input_Xj2"]'
    assert ups[0]["matched"] == "resume"
    # A7 also removes the attached input from the unknowns
    assert all(u["selector"] != '[id="input_files_input_Xj2"]'
               for u in result["unknown_questions"])


def test_resume_text_input_not_uploaded(monkeypatch):
    # 'Link to your resume' rendered as a TEXT input: set_input_files on it
    # hangs — must surface for the human instead
    f = _field('[name="resume_link"]', ftype="text", label="Link to your resume",
               name="resume_link")
    result = _analyze(monkeypatch, [f], {"resume_path": "/tmp/resume.pdf"})
    assert not any(x["action"] == "upload" for x in result["fields"])
    assert any("resume" in u["question_text"].lower()
               for u in result["unknown_questions"])
