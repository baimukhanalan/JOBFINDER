"""A combined 'First and Last Name' label must resolve to the FULL name in both the drafter's
identity rules and the analyzer's field patterns (first match wins, so ORDER matters): the
last-name rule used to grab its 'Last Name' tail and an application went out as just the surname."""
import re

from backend.applier import analyzer
from backend.tools import catalog_drafts as cd


def _first_id_key(label: str):
    for rx, key in cd._ID_TEXT:
        if rx.search(label):
            return key
    return None


def _first_field_key(label: str):
    for pat, key, _action in analyzer.FIELD_PATTERNS:
        if re.search(pat, label):
            return key
    return None


def test_drafter_combined_name_is_full_name():
    for label in ("First and Last Name", "First & Last Name", "First and last name*", "Full Name", "Full name"):
        assert _first_id_key(label) == "full_name", label


def test_drafter_first_and_last_still_split():
    assert _first_id_key("First Name") == "first_name"
    assert _first_id_key("Last Name") == "last_name"
    assert _first_id_key("Surname") == "last_name"


def test_analyzer_combined_name_is_full_name():
    for label in ("First and Last Name", "First & Last Name", "first_and_last_name", "Full Name", "Name"):
        assert _first_field_key(label) == "full_name", label


def test_analyzer_first_and_last_still_split():
    assert _first_field_key("First Name") == "_first_name"
    assert _first_field_key("Last Name") == "_last_name"
    assert _first_field_key("Company name") != "full_name"
