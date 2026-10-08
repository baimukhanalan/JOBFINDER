"""Pure slot-picker + provider detection for the recruiter auto-booking (no network/browser)."""
import datetime as dt

from backend.interviews import booking_bot as bb

TZ = dt.timezone(dt.timedelta(hours=2))          # Europe/Berlin-ish


def _d(day, h, m=0):
    return dt.datetime(2026, 10, day, h, m, tzinfo=TZ)


def test_pick_nearest_2000_in_window_on_earliest_day():
    offered = [_d(10, 20, 0), _d(9, 19, 0), _d(9, 21, 0), _d(9, 20, 0), _d(9, 9, 0)]
    pick = bb.pick_slot(offered)
    assert pick == _d(9, 20, 0)                  # earliest day (9th), exactly 20:00 beats the 10th's 20:00


def test_pick_respects_window_edges():
    offered = [_d(9, 18, 0), _d(9, 18, 30), _d(9, 21, 0), _d(9, 22, 0)]
    pick = bb.pick_slot(offered)
    # 18:00 and 22:00 are OUTSIDE 18:30–21:30; of the in-window {18:30,21:00}, 21:00 is nearer 20:00
    assert pick == _d(9, 21, 0)


def test_no_in_window_slot_returns_none():
    assert bb.pick_slot([_d(9, 9, 0), _d(9, 12, 0), _d(9, 23, 0)]) is None
    assert bb.pick_slot([]) is None


def test_custom_prefer():
    offered = [_d(9, 19, 0), _d(9, 20, 30)]
    assert bb.pick_slot(offered, prefer="19:00") == _d(9, 19, 0)


def test_detect_provider():
    assert bb.detect_provider("https://calendly.com/acme/30min") == "calendly"
    assert bb.detect_provider("https://acme.modernloop.io/x") == "modernloop"
    assert bb.detect_provider("https://app.goodtime.io/x") == "goodtime"
    assert bb.detect_provider("https://example.com/book") == "unknown"


def test_book_slot_no_url():
    r = bb.book_slot("", candidate={"name": "A", "email": "a@takhet.com"})
    assert r["booked"] is False and r["reason"] == "no_url"


def test_unsupported_provider_never_books(monkeypatch):
    # a non-Calendly provider returns provider_unsupported WITHOUT launching a browser
    called = {"n": 0}
    import backend.applier.browser as br
    monkeypatch.setattr(br, "BrowserManager", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    r = bb.book_slot("https://acme.modernloop.io/x", candidate={"name": "A", "email": "a@takhet.com"})
    assert r["booked"] is False and r["reason"] == "provider_unsupported" and called["n"] == 0
