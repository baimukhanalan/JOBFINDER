"""Offline tests for the no-browser AMCAT invite state-checker (no network, no DB).

Async calls are driven via asyncio.run() inside plain sync tests (matching this repo's convention —
see test_harvest_shl_gates.py — rather than pulling in pytest-asyncio)."""
import asyncio
import base64
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import assessment_state_check as asc  # noqa: E402


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _make_url(payload: dict | None = None) -> str:
    payload = payload or {"login": {"uniqueID": "12345", "region": "US"}}
    token = "eyJhbGciOiJFUzI1NiJ9." + _b64url(payload) + ".sig"
    return f"https://amcatglobal.aspiringminds.com/?autoLoginVersion=3&token={token}"


def _api_response(status_code: int, body: dict) -> httpx.Response:
    return httpx.Response(status_code, json=body)


def _probe(url: str, handler) -> dict:
    async def _run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await asc.check_amcat_invite(client, url)
    return asyncio.run(_run())


def test_decode_jwt_payload_roundtrip():
    payload = {"login": {"uniqueID": "99099470", "region": "US"}}
    token = "eyJhbGciOiJFUzI1NiJ9." + _b64url(payload) + ".sig"
    assert asc.decode_jwt_payload(token) == payload


def test_decode_jwt_payload_never_raises_on_garbage():
    assert asc.decode_jwt_payload("not-a-jwt") == {}
    assert asc.decode_jwt_payload("") == {}
    assert asc.decode_jwt_payload("a.b") == {}


def test_extract_token():
    url = _make_url()
    tok = asc.extract_token(url)
    assert tok and tok.startswith("eyJ")
    assert asc.extract_token("https://example.com/no-token-here") is None


@pytest.mark.parametrize("message,expected", [
    ("Error Code LEX100: Your test login credentials have expired. Please contact your test administrator.",
     "lex100_expired"),
    ("This assessment has either been completed or submitted. No further action is needed on this "
     "assessment. Message code TC100.", "completed_report"),
    ("We could not detect your camera. Camera is mandatory for this assessment.", "camera_walled"),
    ("Some brand new message we've never seen", "unknown"),
])
def test_classify_amcat_message_terminal_states(message, expected):
    assert asc.classify_amcat_message(message, login_success=False) == expected


def test_classify_amcat_message_fresh_wins_over_message_text():
    # loginSuccess=True always means fresh, regardless of message content.
    assert asc.classify_amcat_message("anything", login_success=True) == "fresh_answerable"


def test_check_amcat_invite_lex100():
    url = _make_url()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == asc.AMCAT_LOGIN_API
        return _api_response(400, {
            "status": "error", "code": 400,
            "message": "Error Code LEX100: Your test login credentials have expired. Please contact your test administrator.",
            "data": {"loginSuccess": False, "userDetails": {"fullName": "Spencer Chase", "hasLoginExpired": 1}},
        })

    res = _probe(url, handler)
    assert res["state"] == "lex100_expired"
    assert res["login_success"] is False
    assert res["full_name"] == "Spencer Chase"


def test_check_amcat_invite_completed_report():
    url = _make_url()

    def handler(request: httpx.Request) -> httpx.Response:
        return _api_response(400, {
            "status": "error", "code": 1009,
            "message": "This assessment has either been completed or submitted. No further action is "
                       "needed on this assessment. Message code TC100.",
            "data": {"loginSuccess": False, "errorCode": 1009,
                     "userDetails": {"fullName": "Charlotte Kingston", "numUsedTest": 0}},
        })

    res = _probe(url, handler)
    assert res["state"] == "completed_report"


def test_check_amcat_invite_fresh():
    url = _make_url()

    def handler(request: httpx.Request) -> httpx.Response:
        return _api_response(200, {
            "status": "success", "code": 200, "message": "",
            "data": {"loginSuccess": True, "userDetails": {"fullName": "New Candidate"}},
        })

    res = _probe(url, handler)
    assert res["state"] == "fresh_answerable"


def test_check_amcat_invite_no_token():
    async def _run():
        async with httpx.AsyncClient() as client:
            return await asc.check_amcat_invite(client, "https://amcatglobal.aspiringminds.com/?no=token")
    res = asyncio.run(_run())
    assert res["state"] == "no_token"


def test_check_amcat_invite_non_json_never_raises():
    url = _make_url()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>gateway error</html>")

    res = _probe(url, handler)
    assert res["state"] == "probe_error"


def test_check_amcat_invite_network_error_never_raises():
    url = _make_url()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    res = _probe(url, handler)
    assert res["state"] == "probe_error"
    assert "error" in res


def test_summarize_counts():
    results = [{"state": "lex100_expired"}, {"state": "lex100_expired"}, {"state": "completed_report"}]
    assert asc.summarize(results) == {"lex100_expired": 2, "completed_report": 1}


def test_checkers_registry_has_amcat():
    assert "amcat" in asc.CHECKERS
    assert callable(asc.CHECKERS["amcat"])
