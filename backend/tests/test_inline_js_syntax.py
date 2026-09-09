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


def test_mass_hiring_page_scripts_parse(tmp_path, monkeypatch):
    from backend.tools import mass_hiring_ui as ui
    monkeypatch.setattr(ui.mass_hiring, "stats", lambda: {"active": 1, "companies": 1, "last_collected": 0})
    monkeypatch.setattr(ui.mass_hiring, "companies", lambda limit=200: [])
    html = ui.render_page()
    assert "mhOpenRun" in html and "_MH_SCROLL" not in html
    _check_scripts(html, tmp_path, "masshiring")
