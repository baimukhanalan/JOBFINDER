"""Unit tests for the catalog_forms render-wait / retry robustness (no network).

Covers the fix for the silent partial/empty-scrape bug: the field-count stability
poll (`_poll_stable`), the "identity-only is suspicious" guard (`_looks_partial`),
and the empty/partial retry orchestration (`_retry_loads`). All three take injectable
read/sleep/clock so the logic is exercised without a browser.
"""
from backend.tools import catalog_forms as cf


# ---- _looks_partial -----------------------------------------------------------
def test_looks_partial_empty_is_not_partial():
    # empty == "not scraped", handled by the empty-retry path, not the partial guard
    assert cf._looks_partial([]) is False


def test_looks_partial_identity_only_is_partial():
    fields = [{"type": "text", "label": "Name"}, {"type": "email", "label": "Email"},
              {"type": "tel", "label": "Phone"}, {"type": "file", "label": "Resume"},
              {"type": "text", "label": "LinkedIn"}]
    assert cf._looks_partial(fields) is True


def test_looks_partial_with_screener_is_full():
    for screener in ("choice", "select", "textarea", "multi_select", "radio_group"):
        fields = [{"type": "text", "label": "Name"},
                  {"type": screener, "label": "A real question"}]
        assert cf._looks_partial(fields) is False, screener


# ---- _poll_stable -------------------------------------------------------------
def _fake_clock():
    """Clock that only advances when sleep() is called (like wall time between polls)."""
    t = {"now": 0.0}
    return (lambda: t["now"]), (lambda s: t.__setitem__("now", t["now"] + s))


def _reader(seq):
    """read() yielding seq values, then repeating the last one forever."""
    box = {"i": 0}

    def read():
        i = box["i"]
        box["i"] = i + 1
        return seq[i] if i < len(seq) else seq[-1]
    return read


def test_poll_returns_when_count_grows_then_stabilizes():
    clock, sleep = _fake_clock()
    # renders progressively: 0 -> 5 -> 13 -> 13 (two equal nonzero reads => stable)
    n = cf._poll_stable(_reader([0, 5, 13, 13]), cap_s=12.0, interval_s=0.5,
                        sleep=sleep, clock=clock)
    assert n == 13
    assert clock() <= 2.0   # returned early, well under the cap


def test_poll_returns_zero_for_genuinely_empty_form_at_cap():
    clock, sleep = _fake_clock()
    n = cf._poll_stable(_reader([0]), cap_s=3.0, interval_s=0.5, sleep=sleep, clock=clock)
    assert n == 0
    assert clock() >= 3.0   # polled the whole cap before giving up


def test_poll_does_not_stop_on_two_equal_zero_reads():
    # a transient 0,0 must NOT be treated as "stable" (0 is falsy) — keep polling
    clock, sleep = _fake_clock()
    n = cf._poll_stable(_reader([0, 0, 7, 7]), cap_s=12.0, interval_s=0.5,
                        sleep=sleep, clock=clock)
    assert n == 7


# ---- _retry_loads -------------------------------------------------------------
def _scripted(loads):
    box = {"i": 0}
    calls = {"n": 0}

    def load():
        calls["n"] += 1
        i = box["i"]
        box["i"] = i + 1
        item = loads[i] if i < len(loads) else loads[-1]
        if isinstance(item, Exception):
            raise item
        return item
    return load, calls


_ID = [{"type": "text", "label": "Name"}, {"type": "email", "label": "Email"}]
_FULL = _ID + [{"type": "choice", "label": "Authorized to work?", "options": ["Yes", "No"]}]


def test_retry_accepts_first_full_result_without_retrying():
    load, calls = _scripted([_FULL])
    got = cf._retry_loads(load, max_loads=3, sleep=lambda s: None)
    assert got == _FULL
    assert calls["n"] == 1


def test_retry_recovers_full_after_identity_only_first_load():
    # the 7175-workable case: first load identity-only, reload yields the screeners
    load, calls = _scripted([_ID, _FULL])
    got = cf._retry_loads(load, max_loads=3, sleep=lambda s: None)
    assert got == _FULL
    assert calls["n"] == 2


def test_retry_identity_only_gets_exactly_one_extra_load():
    # legitimately identity-only: don't hammer — one extra load, then keep it
    load, calls = _scripted([_ID, _ID, _ID])
    got = cf._retry_loads(load, max_loads=3, sleep=lambda s: None)
    assert got == _ID
    assert calls["n"] == 2   # NOT 3


def test_retry_keeps_fuller_identity_only_result():
    smaller = [{"type": "text", "label": "Name"}]
    load, calls = _scripted([smaller, _ID])
    got = cf._retry_loads(load, max_loads=3, sleep=lambda s: None)
    assert got == _ID        # the fuller of the two identity-only reads


def test_retry_empty_retries_up_to_max_then_returns_empty():
    load, calls = _scripted([[], [], []])
    got = cf._retry_loads(load, max_loads=3, sleep=lambda s: None)
    assert got == []
    assert calls["n"] == 3


def test_retry_recovers_after_empty_then_full():
    load, calls = _scripted([[], _FULL])
    got = cf._retry_loads(load, max_loads=3, sleep=lambda s: None)
    assert got == _FULL
    assert calls["n"] == 2


def test_retry_treats_exception_as_empty_and_retries():
    load, calls = _scripted([RuntimeError("nav failed"), _FULL])
    got = cf._retry_loads(load, max_loads=3, sleep=lambda s: None)
    assert got == _FULL
    assert calls["n"] == 2


def test_retry_backoff_is_exponential_not_tight_loop():
    delays = []
    load, _ = _scripted([[], [], []])
    cf._retry_loads(load, max_loads=3, sleep=delays.append, backoff_base=1.5)
    # backoff between the 3 attempts: 1.5**0, 1.5**1 (last attempt has no trailing sleep)
    assert delays == [1.0, 1.5]
