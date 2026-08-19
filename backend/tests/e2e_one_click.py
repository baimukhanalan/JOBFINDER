"""E2E: the one-click apply chain in a REAL Chromium with the unpacked extension.

Chain under test:
    dashboard Apply link (#aa=profile:jid) -> content script auto-run
    -> /job_pack fetch -> form auto-fill -> /resume_file auto-attach
    -> review panel -> submit-detection -> /mark_ext -> status.json flip.

Standalone script (NOT pytest-collected). Run from the repo root:
    PYTHONPATH=. python3 backend/tests/e2e_one_click.py [--live]

Requirements: the dashboard running on 127.0.0.1:8089 (PM2 jobfinder-dash),
Xvfb on :99 (PM2 jobfinder-display), playwright + chromium installed.

NEVER submits a real application: the local run drives a static fixture page
(backend/tests/fixtures/workable_form.html, captured from the same posting as
the boldr-robotics-support-associate run dir); the optional --live smoke only
loads a real Greenhouse posting, fills it, screenshots and closes — it clicks
nothing on that page.
"""
import argparse
import http.server
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
PROFILE = "michael"
SRC_JID = "boldr-robotics-support-associate"
E2E_JID = "e2etest"  # lowercase alnum only — survives the server's _safe_id
PREFILL = ROOT / "uploads" / "prefill" / PROFILE
FIXTURES = ROOT / "backend" / "tests" / "fixtures"
EXT_DIR = ROOT / "extension"
DASH = "http://127.0.0.1:8089"
TOKEN = (ROOT / "backend" / ".assist_token").read_text().strip()
SHOT_LOCAL = ROOT / "uploads" / "e2e_one_click.png"
SHOT_LIVE = ROOT / "uploads" / "e2e_live_greenhouse.png"
LIVE_URL = "https://job-boards.greenhouse.io/automatticcareers/jobs/6216089"

NEEDS_STYLE_SEL = '[style*="122, 92, 18"], [style*="#7a5c12"]'  # #7a5c12 = rgb(122,92,18)

JS_COUNT_FILLED = """() => {
  const skip = new Set(['hidden','submit','button','reset','file','image','checkbox','radio']);
  let n = 0;
  document.querySelectorAll('input, textarea').forEach(el => {
    if (skip.has((el.type || '').toLowerCase())) return;
    if (el.value && el.value.trim()) n++;
  });
  return n;
}"""

JS_FILE_INPUTS = """() => [...document.querySelectorAll("input[type='file']")]
  .map(el => [...el.files].map(f => f.name))"""

CHECKS = []  # (run, name, ok, detail)


def check(run: str, name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((run, name, bool(ok), detail))
    print(f"  [{run}] {'PASS' if ok else 'FAIL'} — {name}" + (f" ({detail})" if detail else ""))
    return bool(ok)


def free_port() -> int:
    for p in range(8084, 8100):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("no free port in 8084-8099")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # keep the test output readable
        pass


def serve_fixtures(port: int) -> http.server.HTTPServer:
    handler = lambda *a, **k: _Quiet(*a, directory=str(FIXTURES), **k)  # noqa: E731
    httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def api_get(path: str):
    req = Request(DASH + path, headers={"X-Assist-Token": TOKEN})
    with urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def read_status() -> dict:
    p = PREFILL / "status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def drop_status_key(jid: str) -> None:
    """Remove one jid from status.json with the same tmp+os.replace atomic write
    the server uses (cross-process there is no shared lock — atomicity is the
    actual guarantee; nothing else races during the test teardown)."""
    p = PREFILL / "status.json"
    data = read_status()
    if jid not in data:
        return
    data.pop(jid, None)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def poll(fn, timeout: float, interval: float = 0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return fn()


def wire_console(page, sink: list, tag: str):
    page.on("console", lambda m: sink.append(f"[{tag}:{m.type}] {m.text}"))
    page.on("pageerror", lambda e: sink.append(f"[{tag}:pageerror] {e}"))


def find_live_jid() -> str | None:
    """Run dir under uploads/prefill/michael whose pack targets the live posting."""
    best, best_mtime = None, -1.0
    for d in PREFILL.iterdir():
        if not d.is_dir() or "automattic" not in d.name:
            continue
        try:
            rep = json.loads((d / "report.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        if "6216089" not in (rep.get("apply_url") or ""):
            continue
        mt = (d / "report.json").stat().st_mtime
        if mt > best_mtime:
            best, best_mtime = d.name, mt
    return best


def run_local(ctx, page, console: list) -> None:
    """Steps 4-6: fixture page — fill, attach, panel, fake submit, mark_ext."""
    pack = api_get(f"/job_pack?profile={PROFILE}&jid={E2E_JID}")
    review_empty = not pack.get("review")
    print(f"  pack: {len(pack.get('answers') or {})} answers, review_empty={review_empty}, "
          f"has_resume={pack.get('has_resume')}")

    port = free_port()
    httpd = serve_fixtures(port)
    try:
        page.goto(f"http://127.0.0.1:{port}/workable_form.html#aa={PROFILE}:{E2E_JID}",
                  wait_until="load")

        # (a) review panel
        panel = True
        try:
            page.wait_for_selector("#__aa_panel", timeout=30000)
        except Exception:
            panel = False
        check("local", "review panel #__aa_panel appeared", panel)
        page.wait_for_timeout(3000)  # let the async fill/attach passes settle

        # (b) auto-fill ran
        filled = page.evaluate(JS_COUNT_FILLED)
        check("local", ">=5 inputs/textareas filled", filled >= 5, f"filled={filled}")

        # (c) resume auto-attached (async — poll up to 15s)
        def attached():
            for names in page.evaluate(JS_FILE_INPUTS):
                if len(names) == 1 and names[0] == "resume.pdf":
                    return True
            return False
        ok_attach = poll(attached, 15)
        check("local", "file input holds resume.pdf (files.length == 1)", ok_attach,
              f"file inputs: {page.evaluate(JS_FILE_INPUTS)}")

        # (d) yellow "needs" marks vs the pack's review map
        needs_n = page.evaluate(f"() => document.querySelectorAll('{NEEDS_STYLE_SEL}').length")
        if review_empty:
            check("local", "pack.review empty — yellow marks not required", True,
                  f"observed {needs_n} yellow-marked element(s) anyway")
        else:
            check("local", "at least one yellow 'needs' outline (#7a5c12)", needs_n >= 1,
                  f"needs={needs_n}")

        # (e) screenshot
        page.screenshot(path=str(SHOT_LOCAL), full_page=True)
        check("local", "screenshot saved", SHOT_LOCAL.is_file(), str(SHOT_LOCAL))

        # (6) submit-detection WITHOUT a real submit: the page is a static local
        # fixture — clicking its submit button cannot reach any real ATS.
        btn = page.get_by_role("button", name=re.compile("submit", re.I)).first
        if btn.count():
            btn.click()
            try:  # native form GET-submit may reload the fixture; ride it out
                page.wait_for_load_state("load", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(1500)  # content script re-inject + watch resume
            page.evaluate(
                "document.body.insertAdjacentHTML('beforeend','<div>Thank you for applying</div>')")
            marked = poll(
                lambda: read_status().get(E2E_JID, {}).get("status") == "submitted", 10, 0.5)
            check("local", "submit detected -> mark_ext -> status.json == submitted", marked,
                  f"status.json[{E2E_JID}]={read_status().get(E2E_JID)}")
        else:
            check("local", "fixture submit button present", False, "no submit button found")
    finally:
        httpd.shutdown()


def run_live(ctx, console: list) -> None:
    """LIVE smoke on the real Greenhouse posting. Fill + screenshot ONLY —
    this function never clicks anything on the page."""
    jid = find_live_jid()
    if not check("live", "automattic run dir found", bool(jid), f"jid={jid}"):
        return
    page = ctx.new_page()
    wire_console(page, console, "live")
    try:
        page.goto(f"{LIVE_URL}#aa={PROFILE}:{jid}", wait_until="domcontentloaded",
                  timeout=60000)
        panel = True
        try:
            page.wait_for_selector("#__aa_panel", timeout=45000)
        except Exception:
            panel = False
        check("live", "review panel #__aa_panel appeared", panel)
        page.wait_for_timeout(8000)  # custom dropdowns / attach settle

        filled = page.evaluate(JS_COUNT_FILLED)
        check("live", ">=5 fields filled", filled >= 5, f"filled={filled}")

        file_inputs = page.evaluate(JS_FILE_INPUTS)
        if file_inputs:
            def attached():
                return any(n == ["resume.pdf"] for n in page.evaluate(JS_FILE_INPUTS))
            ok = poll(attached, 20)
            check("live", "resume.pdf attached to a file input", ok,
                  f"file inputs: {page.evaluate(JS_FILE_INPUTS)}")
        else:
            check("live", "no file input on the form — attach not applicable", True)

        page.screenshot(path=str(SHOT_LIVE), full_page=True)
        check("live", "screenshot saved", SHOT_LIVE.is_file(), str(SHOT_LIVE))
    finally:
        page.close()  # nothing was clicked; nothing was submitted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="after the local E2E passes, smoke the real Greenhouse posting (no clicks)")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    # 1) prep: a sacrificial copy of a real prefill pack under a test jid
    src, dst = PREFILL / SRC_JID, PREFILL / E2E_JID
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    assert (dst / "report.json").is_file() and (dst / "resume.pdf").is_file(), \
        "e2etest copy is missing report.json/resume.pdf"

    user_dir = tempfile.mkdtemp(prefix="aa-e2e-")
    console: list[str] = []
    exit_code = 1
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_dir, headless=False,
                args=[f"--disable-extensions-except={EXT_DIR}",
                      f"--load-extension={EXT_DIR}", "--no-sandbox", "--no-first-run"],
                env={**os.environ, "DISPLAY": ":99"},  # playwright won't forward DISPLAY itself
            )
            try:
                # 3) extension service worker + point it at the local dashboard
                sw = ctx.service_workers[0] if ctx.service_workers else \
                    ctx.wait_for_event("serviceworker", timeout=20000)
                sw.evaluate(f"() => chrome.storage.local.set({{aa_base: '{DASH}'}})")
                base = sw.evaluate("() => chrome.storage.local.get('aa_base')")
                check("setup", "extension SW loaded + aa_base override set",
                      base.get("aa_base") == DASH, f"aa_base={base.get('aa_base')}")

                page = ctx.new_page()
                wire_console(page, console, "local")
                run_local(ctx, page, console)

                local_ok = all(ok for run, _, ok, _ in CHECKS if run in ("setup", "local"))
                if args.live:
                    if local_ok:
                        run_live(ctx, console)
                    else:
                        print("  [live] skipped — local E2E failed")
            finally:
                ctx.close()
    finally:
        # 8) cleanup: test pack dir, its status.json entry, throwaway profile dir
        shutil.rmtree(dst, ignore_errors=True)
        drop_status_key(E2E_JID)
        shutil.rmtree(user_dir, ignore_errors=True)

    errors = [m for m in console if ":error]" in m or ":pageerror]" in m]
    if errors:
        print("\n--- console errors ---")
        for m in errors[:20]:
            print(" ", m[:300])

    print("\n=== SUMMARY ===")
    failed = 0
    for run, name, ok, detail in CHECKS:
        print(f"  [{run}] {'PASS' if ok else 'FAIL'} — {name}" + (f" ({detail})" if detail else ""))
        failed += not ok
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    exit_code = 0 if failed == 0 else 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
