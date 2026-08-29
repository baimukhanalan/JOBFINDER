"""Unit tests for the large-employer E-Verify reference source (no network).

Covers the ported HTML parser, the segmentation heuristic, the graceful-degradation contract of
the live fetch (via httpx.MockTransport), and the stale-gated cache. All offline.
"""
import json

import httpx

from backend.tools import everify_employers as ev


_ROW_HTML = '''<table><tbody><tr class="evm-tr">
  <td class="evm-tdnm"><div class="evm-enm">Example Holdings, Inc.</div>
  <div class="evm-dba">DBA: Example Brand</div></td>
  <td><span class="evm-sbadge evm-sb-open">Active</span></td>
  <td class="evm-tdsz">10,000 and over</td>
  <td><span class="evm-stag">CA</span><span class="evm-stag">TX</span>
  <span class="evm-stag evm-stag-more">+3</span></td>
  <td class="evm-tddt">Jan 2, 2020</td>
  <td class="r"><span class="evm-sites">1,234</span></td>
</tr></table>'''


# ---- parser --------------------------------------------------------------------
def test_parser_extracts_identity_workforce_sites_and_states():
    rows = ev.parse_everify_employer_page(
        _ROW_HTML, source_url="https://source.test/page", observed_at="2026-08-30T00:00:00Z")
    assert len(rows) == 1
    r = rows[0]
    assert r["legal_name"] == "Example Holdings, Inc."
    assert r["trade_name"] == "Example Brand"
    assert r["employee_size"] == "10000+"
    assert r["employee_count_min"] == 10000
    assert r["hiring_sites"] == 1234
    assert r["states"] == ["CA", "TX"]
    assert r["additional_state_count"] == 3
    assert r["source"] == "everify_large_employer"
    assert r["country"] == "US"


def test_parser_drops_rows_below_10k_workforce():
    html = _ROW_HTML.replace("10,000 and over", "500 to 999")
    assert ev.parse_everify_employer_page(
        html, source_url="x", observed_at="t") == []


def test_parser_strips_leading_asterisk_marker():
    html = _ROW_HTML.replace(
        '<div class="evm-enm">Example Holdings, Inc.</div>',
        '<div class="evm-enm">* Jones Lang LaSalle</div>').replace(
        '<div class="evm-dba">DBA: Example Brand</div>', '')
    rows = ev.parse_everify_employer_page(html, source_url="x", observed_at="t")
    assert rows[0]["legal_name"] == "Jones Lang LaSalle"
    assert rows[0]["trade_name"] == "Jones Lang LaSalle"


def test_parser_ignores_non_employer_html():
    assert ev.parse_everify_employer_page("<html><body>no table</body></html>",
                                          source_url="x", observed_at="t") == []


# ---- segmentation --------------------------------------------------------------
def test_classify_staffing_lane_without_risk():
    segment, risks = ev.classify_employer(
        {"brand_name": "Acme Staffing", "industry": "workforce solutions"})
    assert segment == "staffing"
    assert risks == []


def test_classify_flags_shell_fund_and_aggregate():
    segment, risks = ev.classify_employer(
        {"brand_name": "Example Payroll Shared Services Investment Fund Trust",
         "industry": "financial services"})
    assert segment == "general"
    assert "shell_or_shared_services" in risks
    assert "fund_or_trust" in risks


def test_classify_specialized_verticals():
    assert ev.classify_employer({"brand_name": "City of Austin"})[0] == "government"
    assert ev.classify_employer({"brand_name": "Duke University"})[0] == "education"
    assert ev.classify_employer({"brand_name": "Mercy Hospital"})[0] == "healthcare"
    assert ev.classify_employer({"brand_name": "Red Cross Foundation"})[0] == "nonprofit"


def test_classify_falls_back_to_general():
    assert ev.classify_employer({"brand_name": "Dollar Tree, Inc"})[0] == "general"


# ---- live fetch (mocked, deduped, ranked) --------------------------------------
def _page(names_sites):
    body = "".join(
        f'<tr class="evm-tr"><td class="evm-tdnm"><div class="evm-enm">{n}</div></td>'
        f'<td class="evm-tdsz">10,000 and over</td>'
        f'<td><span class="evm-stag">CA</span></td>'
        f'<td class="evm-tddt">Jan 1, 2020</td>'
        f'<td><span class="evm-sites">{s:,}</span></td></tr>'
        for n, s in names_sites)
    return f"<table><tbody>{body}</tbody></table>"


def test_fetch_dedupes_and_ranks_by_hiring_sites():
    pages = {
        1: _page([("Alpha Corp", 5000), ("Beta LLC", 4000)]),
        2: _page([("Beta LLC", 4000), ("Gamma Inc", 3000)]),  # Beta repeats -> deduped
        3: "<table></table>",  # empty -> stop
    }

    def handler(request):
        page = int(dict(request.url.params).get("page", 1))
        return httpx.Response(200, request=request, text=pages.get(page, "<table></table>"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = ev.large_everify_employers(limit=100, client=client)
    names = [r["name"] for r in rows]
    assert names == ["Alpha Corp", "Beta LLC", "Gamma Inc"]  # ranked desc, deduped
    assert rows[0]["hiring_sites"] == 5000


def test_fetch_never_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(ev.time, "sleep", lambda *_: None)  # skip the real backoff sleeps

    def handler(request):
        return httpx.Response(503, request=request, text="")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        # 503 retries exhaust -> returns [] (no raise)
        assert ev.fetch_large_everify_employers(limit=10, min_interval=0, client=client) == []


def test_fetch_never_raises_on_transport_exception(monkeypatch):
    monkeypatch.setattr(ev.time, "sleep", lambda *_: None)

    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert ev.fetch_large_everify_employers(limit=10, min_interval=0, client=client) == []


def test_fetch_respects_limit():
    def handler(request):
        return httpx.Response(200, request=request,
                              text=_page([(f"Co {i}", 100 - i) for i in range(50)]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = ev.fetch_large_everify_employers(limit=5, min_interval=0, client=client)
    assert len(rows) == 5


# ---- cache ---------------------------------------------------------------------
def test_cache_roundtrip_and_missing_is_empty(tmp_path):
    path = tmp_path / "everify.json"
    assert ev.load_cached(path=path) == {}  # missing -> {}
    payload = {"fetched_at": 1, "count": 1,
               "employers": [{"name": "X", "segment": "general", "hiring_sites": 9,
                              "states": ["CA"], "additional_state_count": 0}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    got = ev.load_cached(path=path)
    assert got["employers"][0]["name"] == "X"


def test_cache_bad_json_is_empty(tmp_path):
    path = tmp_path / "everify.json"
    path.write_text("{ not json", encoding="utf-8")
    assert ev.load_cached(path=path) == {}


def test_maybe_refresh_skips_when_fresh(tmp_path, monkeypatch):
    import time as _t
    path = tmp_path / "everify.json"
    path.write_text(json.dumps({"fetched_at": int(_t.time()), "count": 2,
                                "employers": [{"name": "A"}, {"name": "B"}]}), encoding="utf-8")

    def _boom(*a, **k):  # must NOT be called when cache is fresh
        raise AssertionError("network fetch attempted on a fresh cache")

    monkeypatch.setattr(ev, "large_everify_employers", _boom)
    out = ev.maybe_refresh_cache(path=path, max_age=99999)
    assert out == {"refreshed": False, "count": 2}


def test_maybe_refresh_when_stale(tmp_path, monkeypatch):
    path = tmp_path / "everify.json"
    path.write_text(json.dumps({"fetched_at": 1, "count": 0, "employers": []}),
                    encoding="utf-8")
    monkeypatch.setattr(ev, "large_everify_employers",
                        lambda **k: [{"name": "Fresh Co", "legal_name": "Fresh Co",
                                      "segment": "general", "risk_flags": [],
                                      "hiring_sites": 42, "states": ["CA"],
                                      "additional_state_count": 0}])
    out = ev.maybe_refresh_cache(path=path, max_age=1)
    assert out == {"refreshed": True, "count": 1}
    assert ev.load_cached(path=path)["employers"][0]["name"] == "Fresh Co"


def test_refresh_keeps_old_cache_on_empty_fetch(tmp_path, monkeypatch):
    path = tmp_path / "everify.json"
    good = {"fetched_at": 5, "count": 1, "employers": [{"name": "Keep Me"}]}
    path.write_text(json.dumps(good), encoding="utf-8")
    monkeypatch.setattr(ev, "large_everify_employers", lambda **k: [])
    kept = ev.refresh_cache(path=path)
    assert kept["employers"][0]["name"] == "Keep Me"  # not wiped
