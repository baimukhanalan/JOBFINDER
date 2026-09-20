"""Every inline <script> the dashboard pages ship must PARSE. The page scripts are hand-built
Python string concatenations, and one dropped `}catch{}` (commit 68a6280, 2026-08-25) made the
whole /unfinished <script> a SyntaxError for two weeks — every button on that tab was dead and
nothing noticed. This test renders the «Вакансии» pages with stubbed data and runs `node --check`
on each script block. Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_inline_js_syntax.py -q
"""
import re
import shutil
import subprocess

import pytest

_SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _check_scripts(html: str, tmp_path, label: str):
    blocks = [m.group("body") for m in _SCRIPT_RE.finditer(html) if "src=" not in m.group("attrs")]
    assert blocks, f"{label}: no inline scripts found"
    for i, body in enumerate(blocks):
        f = tmp_path / f"{label}_{i}.js"
        f.write_text(body)
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{label} script #{i} does not parse:\n{r.stderr[-800:]}"
    return len(blocks)


def test_shell_js_parses(tmp_path):
    from backend.tools import mailcrm_ui as m
    html = m._page("unfinished", "<div>x</div>")
    assert _check_scripts(html, tmp_path, "shell") >= 2


def test_unfinished_page_scripts_parse(tmp_path, monkeypatch):
    from backend.tools import bulk_log, catalog_db
    from backend import dashboard_app as d
    monkeypatch.setattr(bulk_log, "unfinished", lambda: [
        {"jobid": 1, "company": "Acme", "title": "T", "ts": "2026-09-09T10:00:00", "error": "x"}])
    monkeypatch.setattr(catalog_db, "jobs_by_ids", lambda ids: {})
    monkeypatch.setattr(d, "_drain_partition", lambda items: (list(items), []))
    html = d.unfinished_index().body.decode()
    assert "unfRerunAll" in html and "fab-compose" in html
    _check_scripts(html, tmp_path, "unfinished")


def test_today_page_scripts_parse(tmp_path, monkeypatch):
    # the «Сегодня» dashboard adds no page-specific inline JS, but render it with a stubbed
    # blob (no DB/log I/O) so any future inline script it grows is checked, and confirm the
    # shell scripts still parse under it.
    from backend.tools import today_dash, today_ui
    blob = {
        "generated_at": 1789916244, "took_ms": 5, "day_label": "20.09.2026",
        "submissions": {"total_confirmed": 141, "total_attempts": 286,
                        "lanes": [{"key": "tp", "label": "Teleperformance",
                                   "attempts": 115, "confirmed": 101}]},
        "assessments": {
            "invites": {"total_msgs": 32, "total_personas": 26,
                        "by_source": [{"label": "Maximus", "msgs": 24, "personas": 18}],
                        "by_role": [{"label": "Поддержка клиентов", "personas": 16}]},
            "solved": {"total": 4, "by_source": [{"label": "TTEC", "n": 4}],
                       "by_role": [{"label": "Прочее", "n": 4}],
                       "cards": [{"email": "a.b1@takhet.com", "source_key": "ttec",
                                  "source": "TTEC", "role": "Прочее",
                                  "salary_label": "~$80k–$130k", "salary_estimated": True}]},
        },
        "interviews": {"total": 0, "cards": []},
        "offers": {"total": 1, "cards": [{"email": "a.b1@takhet.com", "company": "TTEC",
                                          "source": "TTEC", "role": "Прочее",
                                          "salary_label": "~$80k–$130k", "salary_estimated": True}]},
        "candidates": [],
    }
    monkeypatch.setattr(today_dash, "get_today", lambda force=False: blob)
    html = today_ui.render_page()
    assert "Сегодня" in html and "Teleperformance" in html
    _check_scripts(html, tmp_path, "today")


def test_catalog_page_scripts_parse(tmp_path, monkeypatch):
    from backend.tools import catalog_ui as ui
    # stub the DB reads so the render is pure (a card + the sheets + the whole _CAT_JS)
    job = {"id": 1, "ats": "greenhouse", "company": "Acme", "company_key": "acme", "title": "T",
           "location": "Remote", "department": "", "workplace": "remote", "is_remote": True,
           "url": "https://x", "questions": [], "q_count": 0, "regions": ["US"]}
    monkeypatch.setattr(ui.catalog_db, "list_jobs", lambda **k: [job])
    monkeypatch.setattr(ui.catalog_db, "companies", lambda *a, **k: [])
    monkeypatch.setattr(ui, "_counts_cached", lambda *a, **k: {"total": 1, "remote": 1,
                        "with_questions": 0, "by_region": {"US": 1}, "untagged": 0})
    html = ui.render_page()
    assert "catSelBar" in html and "cat-name" not in html
    _check_scripts(html, tmp_path, "catalog")


def test_hiring_events_page_scripts_parse(tmp_path, monkeypatch):
    from backend.tools import hiring_events as he
    # stub the grouped-events scan → one room + one invite, and force the persona resolver
    # to miss so the render is pure (no disk scan of the real prefill tree).
    monkeypatch.setattr(he, "prefill_dir_for", lambda *a, **k: None)
    monkeypatch.setattr(he, "grouped_events", lambda **k: [{
        "key": "m1", "meeting_id": "7436255779",
        "join_url": "https://us06web.zoom.us/j/7436255779",
        "role": "Remote CSR", "date_text": "Mon-Fri", "time_text": "9-5 ET",
        "latest_ts": 0, "invites": [
            {"mailbox": "jane.doe1@takhet.com", "candidate": "Jane Doe",
             "date_ts": 0, "path_hash": "abc123",
             "tracking_url": "https://tracking.icims.com/f/a/A~~/x/tokA",
             "join_url": "https://us06web.zoom.us/j/7436255779",
             "meeting_id": "7436255779", "resolved": True}]}])
    html = he.render_page()
    assert "he-exp-btn" in html and 'id="he-d-abc123"' in html
    # the per-candidate «Ссылка» opens THAT persona's own Zoom room (same as the group here)
    assert 'class="he-inv-link"' in html
    assert 'href="https://us06web.zoom.us/j/7436255779" target="_blank"' in html
    assert "др. комната" not in html  # same room → not flagged
    _check_scripts(html, tmp_path, "hiring")


def test_mass_hiring_page_scripts_parse(tmp_path, monkeypatch):
    from backend.tools import mass_hiring_ui as ui
    monkeypatch.setattr(ui.mass_hiring, "stats", lambda: {"active": 1, "companies": 1, "last_collected": 0})
    monkeypatch.setattr(ui.mass_hiring, "companies", lambda limit=200: [])
    html = ui.render_page()
    assert "mhOpenRun" in html and "_MH_SCROLL" not in html
    _check_scripts(html, tmp_path, "masshiring")
