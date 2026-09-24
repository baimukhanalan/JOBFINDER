"""Tests for the Teleperformance Virtual Hiring Event capture lane.

Pure (no DB / no network) for the matcher + the body extractors + the Zoom-id
parse; the DB-backed aggregation is exercised live against real mail in the report,
not here. Fixtures are trimmed copies of the two real invite variants (inline-URL
"TODAY!" blast and the anchor-text "You're Invited" blast).
"""
from __future__ import annotations

import json

from backend.tools import hiring_events as he

# ---- fixtures: the two real invite shapes (tokens scrubbed) ----------------------

# Variant A — the "TODAY!" blast: the Zoom link is INLINE in the plain body
# (💻 Zoom:<url>, no space), unsubscribe URL on its own line after "please go to:".
_PLAIN_A = (
    "Hi there,\n \n"
    "Great news! Teleperformance is hiring Remote Customer Service Representatives, "
    "and we’d love to meet you!\n \n"
    "\U0001f3af Join our Virtual Hiring Event and interview with our team!\n \n"
    "\U0001f4c5 Hiring Event: Monday–Friday\n"
    "⏰ Time: 9:30 AM – 5:00 PM ET\n"
    "\U0001f4bb Zoom:https://tracking.icims.com/f/a/ZOOM_A~~/AAIB5hA~/zoomtokenA\n \n"
    "\U0001f680 Why join?\n"
    "- Remote Customer Service opportunity\n"
    "⏰ Interview spots can fill quickly, so join early to secure your spot.\n \n"
    "This message was sent to madison.harlow5917@takhet.com. If you don't want to "
    "receive these emails from this company in the future, please go to:\n"
    "https://tracking.icims.com/f/a/UNSUB_A~~/AAIB5hA~/unsubtokenA \n"
)
_HTML_A = (
    '<html><body><p>\U0001f4bb Zoom:'
    '<a href="https://tracking.icims.com/f/a/ZOOM_A~~/AAIB5hA~/zoomtokenA">'
    'https://tracking.icims.com/f/a/ZOOM_A~~/AAIB5hA~/zoomtokenA</a></p>'
    '<p>please go to: <a href="https://tracking.icims.com/f/a/UNSUB_A~~/AAIB5hA~/unsubtokenA">'
    'unsubscribe</a></p></body></html>'
)

# Variant B — "You're Invited": plain has only anchor TEXT ("Join the Hiring Event
# Now"), the real Zoom link lives only in the HTML anchor.
_PLAIN_B = (
    "Hi there,\n \n"
    "Great news! Teleperformance is hiring Remote Customer Service Representatives, "
    "and we’d love to meet you!\n \n"
    "\U0001f4c5 Hiring Event: Monday–Friday\n"
    "⏰ Time: 9:30 AM – 5:00 PM ET\n"
    "\U0001f4bb Zoom: Join the Hiring Event Now\n \n"
    "This message was sent to samuel.nash3785@takhet.com. If you don't want to "
    "receive these emails from this company in the future, please go to:\n"
    "https://tracking.icims.com/f/a/UNSUB_B~~/AAIB5hA~/unsubtokenB \n"
)
_HTML_B = (
    '<html><body><p>\U0001f4bb Zoom: '
    '<a href="https://tracking.icims.com/f/a/ZOOM_B~~/AAIB5hA~/zoomtokenB">'
    'Join the Hiring Event Now</a></p>'
    '<p><a href="https://tracking.icims.com/f/a/UNSUB_B~~/AAIB5hA~/unsubtokenB">'
    'https://teleperformance.icims.com/icims2</a></p></body></html>'
)


# ---- matcher --------------------------------------------------------------------
def test_is_hiring_event_true():
    assert he.is_hiring_event(
        "\U0001f3af Join our Virtual Hiring Event and interview with our team, TODAY!",
        "teleperformance+email+jibmk-ceee1344bd@talent.icims.com")
    assert he.is_hiring_event(
        "\U0001f389 You're Invited! Virtual Hiring Event – Remote CSR",
        "Loraine Roluna <teleperformance+email+jincl@talent.icims.com>")


def test_is_hiring_event_rejects_non_event():
    # right sender, wrong subject (a plain application confirmation)
    assert not he.is_hiring_event(
        "Thank you for applying to Teleperformance",
        "teleperformance@talent.icims.com")
    # right subject wording, but not a Teleperformance/icims sender
    assert not he.is_hiring_event(
        "You're invited to our Virtual Hiring Event",
        "recruiting@some-other-company.com")


# ---- schedule + role extraction -------------------------------------------------
def test_extract_schedule():
    for plain in (_PLAIN_A, _PLAIN_B):
        date_text, time_text = he.extract_schedule(plain)
        assert date_text == "Monday–Friday"
        assert time_text == "9:30 AM – 5:00 PM ET"


def test_extract_role_from_body():
    assert "Remote Customer Service" in he.extract_role(
        "\U0001f3af Join our Virtual Hiring Event ... TODAY!", _PLAIN_A)


def test_extract_role_from_subject_tail():
    assert he.extract_role(
        "\U0001f389 You're Invited! Virtual Hiring Event – Remote CSR", "") == "Remote CSR"


# ---- Zoom / join link extraction ------------------------------------------------
def test_extract_join_inline_variant():
    join, unsub = he.extract_join(_PLAIN_A, _HTML_A)
    assert join == "https://tracking.icims.com/f/a/ZOOM_A~~/AAIB5hA~/zoomtokenA"
    assert unsub == "https://tracking.icims.com/f/a/UNSUB_A~~/AAIB5hA~/unsubtokenA"
    assert join != unsub


def test_extract_join_anchor_text_variant():
    # the real Zoom link is ONLY in the HTML anchor whose text is "Join the Hiring Event Now"
    join, unsub = he.extract_join(_PLAIN_B, _HTML_B)
    assert join == "https://tracking.icims.com/f/a/ZOOM_B~~/AAIB5hA~/zoomtokenB"
    assert unsub == "https://tracking.icims.com/f/a/UNSUB_B~~/AAIB5hA~/unsubtokenB"
    # must never hand back the unsubscribe link as the join link
    assert join != unsub


def test_extract_join_never_returns_unsub_when_only_unsub_present():
    # a degenerate mail with ONLY the unsubscribe icims link must not surface it as a join link
    plain = "please go to:\nhttps://tracking.icims.com/f/a/ONLYUNSUB~~/AAIB5hA~/t"
    join, unsub = he.extract_join(plain, "")
    assert unsub == "https://tracking.icims.com/f/a/ONLYUNSUB~~/AAIB5hA~/t"
    assert join is None


# ---- zoom meeting id -------------------------------------------------------------
def test_zoom_meeting_id():
    assert he.zoom_meeting_id(
        "https://us06web.zoom.us/j/7436255779?pwd=abc") == "7436255779"
    assert he.zoom_meeting_id("https://tracking.icims.com/f/a/x~~/y/z") is None
    assert he.zoom_meeting_id(None) is None


# ---- candidate résumé + detail (state / ФИО / approximate age) -------------------
def test_estimate_age_from_education_graduation_year():
    # earliest education year → age ≈ now − grad + 22
    r = {"education": [{"degree": "BS", "year": "2010"}]}
    assert he.estimate_age(r, now_year=2026) == 2026 - 2010 + 22  # 38


def test_estimate_age_from_experience_when_no_education():
    r = {"experience": [{"title": "CSR", "dates": "2015 - 2019"}]}
    assert he.estimate_age(r, now_year=2026) == 2026 - 2015 + 22  # 33


def test_estimate_age_uses_earliest_anchor_across_edu_and_exp():
    r = {"education": [{"year": "2018"}], "experience": [{"dates": "2012-2018"}]}
    assert he.estimate_age(r, now_year=2026) == 2026 - 2012 + 22  # earliest=2012 → 36


def test_estimate_age_none_when_no_year_or_implausible():
    assert he.estimate_age({}, now_year=2026) is None
    assert he.estimate_age({"education": [{"year": "n/a"}]}, now_year=2026) is None
    # a stray year before the 1950 floor is ignored → no plausible anchor → None
    assert he.estimate_age({"education": [{"year": "1900"}]}, now_year=2026) is None
    # an anchor that would imply age > 75 is rejected (guess band 18–75)
    assert he.estimate_age({"education": [{"year": "1955"}]}, now_year=2026) is None


def test_candidate_detail_reads_state_name_age_and_omits_missing():
    persona = {"profile": {"full_name": "Jane Doe", "state": "Ohio",
                           "resume": {"education": [{"year": "2016"}]}}}
    det = he.candidate_detail(persona)
    assert det["full_name"] == "Jane Doe"
    assert det["state"] == "Ohio"
    assert det["age"] == he.estimate_age(persona["profile"]["resume"])
    # nameless / unresolved → the fallback name, no state, no age
    det2 = he.candidate_detail(None, fallback_name="Fallback Name")
    assert det2 == {"full_name": "Fallback Name", "state": "", "age": None}


def test_prefill_resolution_resume_path_and_filename(tmp_path):
    # a fake prefill tree; id_resolver=None-returning forces the deterministic demo-id guess
    # (`jane.doe1@…` → `demo_jane_doe1`), so the test never touches the real registry/disk.
    root = tmp_path
    d = root / "demo_jane_doe1" / "mh_5"
    d.mkdir(parents=True)
    persona = {"profile": {"full_name": "Jane Doe", "state": "Texas",
                           "resume": {"personal_info": {"name": "Jane Doe"},
                                      "education": [{"degree": "BS", "year": "2016"}],
                                      "experience": [{"title": "CSR",
                                                      "dates": "2018-Present"}]}}}
    (d / "persona.json").write_text(json.dumps(persona), encoding="utf-8")
    (d / "resume.pdf").write_bytes(b"%PDF-1.4 test resume")
    noid = lambda _e: None  # noqa: E731 — force the localpart-guess branch

    got = he.prefill_dir_for("jane.doe1@takhet.com", root=str(root), id_resolver=noid)
    assert got == str(d)
    assert he.resume_pdf_path("jane.doe1@takhet.com", root=str(root),
                              id_resolver=noid) == str(d / "resume.pdf")
    loaded = he.load_persona("jane.doe1@takhet.com", root=str(root), id_resolver=noid)
    assert he.candidate_detail(loaded)["state"] == "Texas"
    # filename is the candidate's name (persona passed → no disk lookup)
    assert he.resume_filename("jane.doe1@takhet.com", loaded) == "Jane Doe - resume.pdf"
    # an unknown persona resolves to nothing (button hidden / 404)
    assert he.prefill_dir_for("nobody.here9@takhet.com", root=str(root),
                              id_resolver=noid) is None
    assert he.resume_pdf_path("nobody.here9@takhet.com", root=str(root),
                              id_resolver=noid) is None


def test_resume_filename_falls_back_to_localpart():
    assert he.resume_filename("someone.new42@takhet.com", persona={}) == \
        "someone.new42 - resume.pdf"


# ---- per-candidate join link (each persona's OWN link, not just the group room) ---
def test_candidate_join_same_room_as_group_not_flagged():
    # a persona resolved to the SAME Zoom room as the group → own link, NOT «differs».
    inv = {"tracking_url": "https://tracking.icims.com/f/a/A~~/x/tokA",
           "join_url": "https://us06web.zoom.us/j/7436255779?pwd=abc",
           "meeting_id": "7436255779", "resolved": True}
    cj = he.candidate_join(inv, group_meeting_id="7436255779")
    assert cj["tracking_url"] == "https://tracking.icims.com/f/a/A~~/x/tokA"
    assert cj["join_url"] == "https://us06web.zoom.us/j/7436255779?pwd=abc"
    assert cj["meeting_id"] == "7436255779"
    assert cj["resolved"] is True
    assert cj["differs"] is False


def test_candidate_join_different_room_is_flagged():
    # a persona whose UNIQUE invite resolves to a DIFFERENT room than the group's → flagged.
    inv = {"tracking_url": "https://tracking.icims.com/f/a/B~~/x/tokB",
           "join_url": "https://us06web.zoom.us/j/9990001111",
           "meeting_id": "9990001111", "resolved": True}
    cj = he.candidate_join(inv, group_meeting_id="7436255779")
    assert cj["meeting_id"] == "9990001111"
    assert cj["differs"] is True
    # the own link opens the candidate's OWN room, not the group's
    assert cj["join_url"] == "https://us06web.zoom.us/j/9990001111"


def test_candidate_join_unresolved_falls_back_to_tracking_never_differs():
    # resolution failed: join_url falls back to the tracking link (still opens Zoom on
    # click), no meeting_id, and an UNRESOLVED candidate is NEVER flagged as differing.
    inv = {"tracking_url": "https://tracking.icims.com/f/a/C~~/x/tokC",
           "join_url": "https://tracking.icims.com/f/a/C~~/x/tokC",
           "meeting_id": None, "resolved": False}
    cj = he.candidate_join(inv, group_meeting_id="7436255779")
    assert cj["join_url"] == "https://tracking.icims.com/f/a/C~~/x/tokC"
    assert cj["meeting_id"] is None
    assert cj["resolved"] is False
    assert cj["differs"] is False


def test_candidate_join_resolved_flag_derived_from_meeting_id():
    # even if an invite dict omits an explicit `resolved`, a real meeting_id means resolved.
    cj = he.candidate_join({"tracking_url": "https://tracking.icims.com/f/a/D~~/x/tokD",
                            "join_url": "https://us06web.zoom.us/j/12345",
                            "meeting_id": "12345"})
    assert cj["resolved"] is True
    # no group room to compare against → never differs
    assert cj["differs"] is False


# ---- OFFICIAL verification + urgency classification (pure) ------------------------

def _inv(**kw):
    """A minimal invite dict for the classifier (pre-stamped verify/deadline fields)."""
    base = {"mailbox": "a@takhet.com", "tracking_url": "https://tracking.icims.com/x",
            "date_ts": 1_700_000_000, "deadline_ts": None, "deadline_days": None,
            "deadline_estimated": True, "meeting_id": None, "resolved": False}
    base.update(kw)
    return base


def test_classify_old_invite_with_live_link_stays_attendable():
    """Expiry is LINK-based, NOT age-based (owner 2026-09-24): a weeks-old invite whose Zoom room
    still resolves is STILL «можно зайти» — only a definitively-dead link (verify='gone') expires."""
    import time as _t
    old_but_live = _inv(verify="zoom", meeting_id="1", date_ts=int(_t.time()) - 40 * 86400)
    assert he.classify_event(old_but_live, now=_t.time()) == "attendable"
    dead_link = _inv(verify="gone", date_ts=int(_t.time()) - 1 * 86400)
    assert he.classify_event(dead_link, now=_t.time()) == "expired"


def test_classify_attendable_when_zoom_resolved_and_no_past_deadline():
    assert he.classify_event(_inv(verify="zoom", meeting_id="7436255779")) == "attendable"


def test_classify_expired_on_explicit_past_deadline():
    # explicit (not estimated) past deadline → unrecoverable, even if the room still resolves
    assert he.classify_event(_inv(verify="zoom", meeting_id="1",
                                  deadline_days=-3, deadline_estimated=False)) == "expired"


def test_classify_estimated_deadline_never_expires():
    # an ESTIMATED past deadline must NOT expire (mirrors the «Собес» is_expired rule)
    assert he.classify_event(_inv(verify="zoom", meeting_id="1",
                                  deadline_days=-9, deadline_estimated=True)) == "attendable"


def test_classify_expired_when_link_definitively_gone():
    assert he.classify_event(_inv(verify="gone")) == "expired"


def test_classify_unverified_on_resolve_error_kept_not_dropped():
    # could not confirm (error/other) AND not explicitly past → unverified, NOT expired
    assert he.classify_event(_inv(verify="error")) == "unverified"
    assert he.classify_event(_inv(verify="other", resolved=True)) == "unverified"


def test_classify_recurring_window_stays_attendable():
    # a recurring «Mon–Fri» window has no explicit deadline → attendable when the room resolves
    assert he.classify_event(_inv(verify="zoom", meeting_id="1",
                                  deadline_ts=None, deadline_days=None)) == "attendable"


def test_verify_events_offline_marks_zoom_by_meeting_id_else_error():
    invs = [_inv(meeting_id="1", tracking_url="t1"), _inv(meeting_id=None, tracking_url="t2")]
    he.verify_events(invs, live=False)
    assert invs[0]["verify"] == "zoom"
    assert invs[1]["verify"] == "error"   # offline can't confirm → unverified, never dropped


def test_urgency_key_orders_attendable_before_expired_and_soonest_deadline_first():
    live_soon = _inv(verify="zoom", meeting_id="1", deadline_days=1, deadline_estimated=False)
    live_far = _inv(verify="zoom", meeting_id="2", deadline_days=9, deadline_estimated=False)
    dead = _inv(verify="gone", deadline_days=-2, deadline_estimated=False)
    ordered = sorted([dead, live_far, live_soon], key=he._urgency_key)
    assert ordered[0] is live_soon and ordered[1] is live_far and ordered[2] is dead


def test_partition_groups_splits_all_dead_room_out_and_keeps_mixed_live():
    now = 1_700_100_000.0
    live_grp = {"key": "L", "latest_ts": 1_700_050_000, "invites": [
        _inv(mailbox="l1@x", verify="zoom", meeting_id="1"),
        _inv(mailbox="l2@x", verify="gone", deadline_days=-5, deadline_estimated=False)]}
    dead_grp = {"key": "D", "latest_ts": 1_700_000_000, "invites": [
        _inv(mailbox="d1@x", verify="gone", deadline_days=-9, deadline_estimated=False)]}
    attend, expired = he.partition_groups([dead_grp, live_grp], now)
    assert [g["key"] for g in attend] == ["L"]      # mixed room stays attendable
    assert [g["key"] for g in expired] == ["D"]     # all-dead room moves to «истёкшие»
    # within the live room, the attendable candidate sorts above the dead one
    assert attend[0]["invites"][0]["mailbox"] == "l1@x"
    assert attend[0]["invites"][0]["_status"] == "attendable"
    assert attend[0]["invites"][1]["_status"] == "expired"
    assert attend[0]["_live_n"] == 1


# ---- auto-distribution + window helpers (2026-09-23) ------------------------------
import datetime as _dt


def test_parse_event_window_recurring():
    w = he.parse_event_window("Monday–Friday", "9:30 AM – 5:00 PM ET")
    assert w["dows"] == {0, 1, 2, 3, 4}
    assert w["start_min"] == 9 * 60 + 30 and w["end_min"] == 17 * 60
    assert w["tz"] == "America/New_York"


def test_parse_event_window_defaults():
    w = he.parse_event_window("", "")
    assert w["dows"] == {0, 1, 2, 3, 4}          # TP default Mon-Fri
    assert w["start_min"] == 0 and w["end_min"] == 1440


def test_next_occurrence_is_future():
    w = he.parse_event_window("Monday-Friday", "9:30 AM - 5:00 PM ET")
    now = _dt.datetime(2026, 9, 23, 12, 0, tzinfo=_dt.timezone.utc)
    occ = he.next_occurrence_utc(w, now)
    assert occ is not None and occ[1] > now


def test_can_attend_empty_avail_unrestricted_but_set_avail_is_respected():
    w = he.parse_event_window("Wednesday", "9:30 AM - 5:00 PM ET")
    now = _dt.datetime(2026, 9, 23, 0, 0, tzinfo=_dt.timezone.utc)
    s, e = he.next_occurrence_utc(w, now)
    assert he._can_attend([], "UTC", s, e) is True                       # no schedule → unrestricted
    wd = he.slots_weekday(s) if hasattr(he, "slots_weekday") else s.astimezone(_dt.timezone.utc).weekday()
    off = [{"dow": wd, "start_min": 0, "end_min": 300, "enabled": True}]  # only 00:00-05:00 UTC
    assert he._can_attend(off, "UTC", s, e) is False
    onw = [{"dow": wd, "start_min": 12 * 60, "end_min": 23 * 60, "enabled": True}]
    assert he._can_attend(onw, "UTC", s, e) is True


def test_auto_distribute_balances_and_idempotent(monkeypatch):
    from backend.interviews import db as ivdb
    invites = [{"mailbox": f"c{i}@x", "candidate": f"C{i}", "date_text": "Monday-Friday",
                "time_text": "9:30 AM - 5:00 PM ET", "_status": "attendable",
                "join_url": "z", "meeting_id": "m"} for i in range(3)]
    groups = [{"invites": invites, "join_url": "z", "meeting_id": "m",
               "date_text": "Monday-Friday", "time_text": "9:30 AM - 5:00 PM ET", "latest_ts": 0}]
    monkeypatch.setattr(he, "grouped_events", lambda **k: groups)
    monkeypatch.setattr(he, "verify_events", lambda invs, **k: invs)
    monkeypatch.setattr(he, "partition_groups", lambda gs, now=None: (gs, []))
    store: dict = {}
    monkeypatch.setattr(ivdb, "event_claims_for", lambda mbs: {m: v for m, v in store.items() if m in mbs})
    monkeypatch.setattr(ivdb, "list_responsibles", lambda active_only=True: [{"id": 1, "tz": "UTC"}, {"id": 2, "tz": "UTC"}])
    monkeypatch.setattr(ivdb, "get_availability", lambda rid: [])
    monkeypatch.setattr(ivdb, "set_event_claim",
                        lambda mb, rid, join_ts=None, joined=None: store.setdefault(mb, []).append({"responsible_id": rid}))
    r1 = he.auto_distribute()
    assert r1["assigned"] == 3
    counts: dict = {}
    for lst in store.values():
        for c in lst:
            counts[c["responsible_id"]] = counts.get(c["responsible_id"], 0) + 1
    assert set(counts) == {1, 2} and max(counts.values()) <= 2       # load-balanced across both
    r2 = he.auto_distribute()
    assert r2["assigned"] == 0 and r2["skipped_claimed"] == 3        # idempotent — no churn


def test_auto_distribute_never_assigns_expired(monkeypatch):
    from backend.interviews import db as ivdb
    inv = {"mailbox": "d@x", "candidate": "D", "date_text": "Monday-Friday",
           "time_text": "9:30 AM - 5:00 PM ET", "_status": "expired"}
    monkeypatch.setattr(he, "grouped_events", lambda **k: [{"invites": [inv], "latest_ts": 0}])
    monkeypatch.setattr(he, "verify_events", lambda invs, **k: invs)
    monkeypatch.setattr(he, "partition_groups", lambda gs, now=None: (gs, []))
    monkeypatch.setattr(ivdb, "event_claims_for", lambda mbs: {})
    monkeypatch.setattr(ivdb, "list_responsibles", lambda active_only=True: [{"id": 1, "tz": "UTC"}])
    monkeypatch.setattr(ivdb, "get_availability", lambda rid: [])
    called = []
    monkeypatch.setattr(ivdb, "set_event_claim", lambda *a, **k: called.append(a))
    r = he.auto_distribute()
    assert r["assigned"] == 0 and called == []                      # expired room never assigned
