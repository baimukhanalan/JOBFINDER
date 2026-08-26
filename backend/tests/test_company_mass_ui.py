from fastapi.testclient import TestClient

from backend import dashboard_app
from backend.tools import company_mass_ui, mailcrm_ui


def _snapshot(**overrides):
    data = {
        "available": True,
        "message": "Основной контур не затронут.",
        "profiles": [{"id": "alan", "name": "Alan B"}],
        "companies": {"total": 1200},
        "enrichment": {"domains": 410, "careers": 280, "ats": 190,
                       "domain_attempted": 940, "web_attempted": 450},
        "jobs": {"active": 84},
        "applications": {"total": 3, "by_state": {
            "awaiting_approval": 2, "auto_submitted": 1}},
        "rows": [{
            "id": 7, "state": "awaiting_approval", "company_name": "A&B <Corp>",
            "title": "Remote Support <script>", "source": "greenhouse",
            "location_raw": "Remote — US", "fit_score": 91,
            "apply_url": "https://example.test/apply?a=1&b=2",
        }],
    }
    data.update(overrides)
    return data


def test_mass_hiring_page_uses_shared_shell_and_only_new_routes():
    html = company_mass_ui.render_page(
        _snapshot(), {"state": "idle", "total": 0, "done": 0},
        selected_profile="alan")
    assert "JobFinder — Массовый найм" in html
    assert "Массовый найм" in html
    assert "Обогащение Company Master" in html
    assert "410" in html and "280" in html and "190" in html
    assert "940" in html and "450" in html
    assert "доменов проверено" in html and "сайтов проверено" in html
    assert 'href="/mass-hiring"' in html
    assert "/mass-hiring/sync" in html
    assert "/mass-hiring/build" in html
    assert "/mass-hiring/start" in html
    assert "/mass-hiring/stop" in html
    assert "/mass-hiring/status" in html
    for forbidden in ("/catalog/fill_all", "127.0.0.1:8102", "noVNC",
                      "synth_persona", "catalog_drafts"):
        assert forbidden not in html


def test_mass_hiring_page_escapes_rows_and_has_explicit_confirmation():
    html = company_mass_ui.render_page(
        _snapshot(), {"state": "idle"}, selected_profile="alan")
    assert "A&amp;B &lt;Corp&gt;" in html
    assert "Remote Support &lt;script&gt;" in html
    assert "Подтвердить массовую подачу" in html
    assert "Для подтверждения введите" in html
    assert 'id="mh-confirm-text"' in html
    assert "SEND $" in html
    assert "Подтвердить и запустить" in html


def test_unavailable_or_no_real_profile_disables_mutating_controls():
    html = company_mass_ui.render_page(_snapshot(
        available=False, profiles=[], message="База недоступна", rows=[]),
        {"state": "idle"})
    assert "Нет готового реального профиля" in html
    assert "База недоступна" in html
    assert 'id="mh-sync" type="button" disabled' in html
    assert 'id="mh-build" type="button" disabled' in html
    assert 'id="mh-start" type="button" disabled' in html


def test_shared_navigation_marks_mass_hiring_active_desktop_and_mobile():
    html = mailcrm_ui._page("mass_hiring", "<p>body</p>")
    assert html.count('href="/mass-hiring"') == 2
    assert html.count('class="active" href="/mass-hiring"') == 2
    assert "Масс-найм" in html


def test_mass_hiring_route_renders_snapshot(monkeypatch):
    monkeypatch.setattr(dashboard_app, "_company_mass_snapshot",
                        lambda profile="": {**_snapshot(), "selected_profile": "alan"})
    client = TestClient(dashboard_app.app)
    response = client.get("/mass-hiring")
    assert response.status_code == 200
    assert "Массовый найм" in response.text
    assert "1 200" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_mass_hiring_status_is_separate_from_legacy_bulk_state():
    client = TestClient(dashboard_app.app)
    old = dict(dashboard_app._COMPANY_MASS_RUN)
    try:
        dashboard_app._COMPANY_MASS_RUN.clear()
        dashboard_app._COMPANY_MASS_RUN.update(
            {"state": "running", "total": 4, "done": 1, "submitted": 1})
        response = client.get("/mass-hiring/status")
        assert response.status_code == 200
        assert response.json()["total"] == 4
        assert "run_id" not in response.json()
    finally:
        dashboard_app._COMPANY_MASS_RUN.clear()
        dashboard_app._COMPANY_MASS_RUN.update(old)


def test_start_route_uses_count_bound_batch_authorization(monkeypatch):
    from backend.tools import company_applier, company_apply_db

    calls = {}
    monkeypatch.setattr(company_applier, "load_candidate", lambda profile: (object(), {}))
    monkeypatch.setattr(company_apply_db, "list_applications", lambda **kwargs: [
        {"id": 11, "revalidation_hash": "h11"},
        {"id": 12, "revalidation_hash": "h12"},
    ])

    def authorize(profile, ids, actor, confirmation, hashes):
        calls.update(profile=profile, ids=ids, actor=actor,
                     confirmation=confirmation, hashes=hashes)
        return {"batch_id": "batch-1", "application_ids": list(ids)}

    monkeypatch.setattr(company_apply_db, "authorize_batch", authorize)

    class Thread:
        def __init__(self, *, target, args, daemon):
            calls.update(target=target, thread_args=args, daemon=daemon)

        def start(self):
            calls["started"] = True

    monkeypatch.setattr(dashboard_app.threading, "Thread", Thread)
    dashboard_app._COMPANY_MASS_RUN["state"] = "idle"
    response = TestClient(dashboard_app.app).post("/mass-hiring/start", data={
        "profile": "alan", "count": "2", "min_fit": "40",
        "confirmation": "SEND 2"})
    assert response.status_code == 200
    assert response.json() == {"started": True, "total": 2, "batch_id": "batch-1"}
    assert calls["confirmation"] == "SEND 2"
    assert calls["ids"] == [11, 12]
    assert calls["hashes"] == {11: "h11", 12: "h12"}
    assert calls["thread_args"][-1] == "batch-1"
    assert calls["started"] is True
    dashboard_app._COMPANY_MASS_RUN["state"] = "idle"
