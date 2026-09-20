"""Offline tests for the OBS lifecycle in the Sutherland/Mac assessment supervisor (no SSH, no DB, no Mac).

`_mac_ssh` (the one real SSH call site) is monkeypatched with a recording fake, and `_fresh` is stubbed,
so the gating (`_ensure_obs` before a drive · `_stop_obs` only on auto-stop, guarded while a drive runs)
is exercised without touching the shared Mac or the CRM DB."""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import assessment_supervisor as s  # noqa: E402


def _cp(stdout: str = "", rc: int = 0):
    """A stand-in for subprocess.CompletedProcess (the code only reads .stdout)."""
    return types.SimpleNamespace(stdout=stdout, returncode=rc, stderr="")


# --------------------------------------------------------------------------- env-gating (no MAC_SSH)

def test_ensure_obs_noop_without_mac_ssh(monkeypatch):
    monkeypatch.delenv("MAC_SSH", raising=False)
    called = []
    monkeypatch.setattr(s, "_mac_ssh", lambda *a, **k: called.append(a) or _cp())
    msg = s._ensure_obs()
    assert "not auto-managed" in msg
    assert called == []          # never touches SSH when MAC_SSH is unset


def test_stop_obs_noop_without_mac_ssh(monkeypatch):
    monkeypatch.delenv("MAC_SSH", raising=False)
    called = []
    monkeypatch.setattr(s, "_mac_ssh", lambda *a, **k: called.append(a) or _cp())
    msg = s._stop_obs()
    assert "not auto-managed" in msg
    assert called == []


# --------------------------------------------------------------------------- _ensure_obs behavior

def test_ensure_obs_already_running_is_noop(monkeypatch):
    """OBS up already → LEAVE it (never restart a feed a live drive may be using; no `open` issued)."""
    monkeypatch.setenv("MAC_SSH", "macalan")
    cmds = []

    def fake(target, cmd, timeout):
        cmds.append(cmd)
        return _cp("up\n")     # pgrep says up

    monkeypatch.setattr(s, "_mac_ssh", fake)
    msg = s._ensure_obs()
    assert "already running" in msg
    assert all("open -a OBS" not in c for c in cmds)   # NO restart


def test_ensure_obs_cold_start_issues_virtualcam(monkeypatch):
    """OBS down → start it headlessly WITH the virtual cam, poll until up."""
    monkeypatch.setenv("MAC_SSH", "macalan")
    monkeypatch.setattr(s.time, "sleep", lambda *a: None)
    cmds = []
    state = {"pgrep_calls": 0}

    def fake(target, cmd, timeout):
        cmds.append(cmd)
        if "pgrep -x OBS" in cmd:
            state["pgrep_calls"] += 1
            return _cp("down\n") if state["pgrep_calls"] == 1 else _cp("up\n")
        if "system_profiler" in cmd:
            return _cp("Camera:\n    OBS Virtual Camera:\n      Model ID: obs-mac-virtualcam\n")
        return _cp("")   # the `open -a OBS ...` start command

    monkeypatch.setattr(s, "_mac_ssh", fake)
    msg = s._ensure_obs()
    assert any("open -a OBS --args --startvirtualcam --minimize-to-tray" == c for c in cmds)
    assert "OBS started" in msg
    assert "Virtual Camera detected" in msg   # system_profiler saw it


def test_ensure_obs_unreachable_mac(monkeypatch):
    monkeypatch.setenv("MAC_SSH", "macalan")
    monkeypatch.setattr(s, "_mac_ssh", lambda *a, **k: None)   # SSH failed
    msg = s._ensure_obs()
    assert "unreachable" in msg


# --------------------------------------------------------------------------- _stop_obs behavior

def test_stop_obs_guarded_while_drive_running(monkeypatch):
    """NEVER kill OBS while a shl_sutherland drive is using the live proctored camera."""
    monkeypatch.setenv("MAC_SSH", "macalan")
    monkeypatch.setattr(s, "_drive_running_locally", lambda: True)
    cmds = []
    monkeypatch.setattr(s, "_mac_ssh", lambda t, c, to: cmds.append(c) or _cp())
    msg = s._stop_obs()
    assert "left UP" in msg
    assert cmds == []           # no pkill issued


def test_stop_obs_kills_when_idle(monkeypatch):
    monkeypatch.setenv("MAC_SSH", "macalan")
    monkeypatch.setattr(s, "_drive_running_locally", lambda: False)
    cmds = []
    monkeypatch.setattr(s, "_mac_ssh", lambda t, c, to: cmds.append(c) or _cp())
    msg = s._stop_obs()
    assert "stopped" in msg
    # SIGKILL (-9) so a plain quit doesn't hang on OBS's "still active" confirm dialog
    assert any("pkill -9 -x OBS" in c for c in cmds)
    # pmset release is best-effort in the same one-liner (silently ignored if sudo needs a password)
    assert any("pmset disablesleep 0" in c for c in cmds)


# --------------------------------------------------------------------------- run() wiring / gating

def _stub_run_env(monkeypatch):
    """Neutralize the side-effecting helpers so run() can be exercised without a Mac/DB."""
    monkeypatch.setattr(s, "_load_attempts", lambda: {})
    monkeypatch.setattr(s, "_save_attempts", lambda d: None)
    monkeypatch.setattr(s, "_tunnel_up", lambda: True)
    monkeypatch.setattr(s, "_tunnel_down", lambda: None)
    monkeypatch.setattr(s, "_mac_online", lambda: True)
    monkeypatch.setattr(s, "_keep_mac_awake", lambda: "awake-stub")


def test_run_idle_stops_obs_and_tunnel(monkeypatch):
    _stub_run_env(monkeypatch)
    monkeypatch.setattr(s, "_fresh", lambda: [])
    calls = []
    monkeypatch.setattr(s, "_stop_obs", lambda: calls.append("stop") or "stopped")
    monkeypatch.setattr(s, "_tunnel_down", lambda: calls.append("tunnel_down"))
    monkeypatch.setattr(s, "_ensure_obs", lambda: calls.append("ensure") or "up")
    s.run(dry=False, max_jobs=8)
    assert "stop" in calls and "tunnel_down" in calls
    assert "ensure" not in calls          # never brings OBS up when there's no work


def test_run_idle_dry_run_touches_nothing(monkeypatch):
    _stub_run_env(monkeypatch)
    monkeypatch.setattr(s, "_fresh", lambda: [])
    calls = []
    monkeypatch.setattr(s, "_stop_obs", lambda: calls.append("stop") or "stopped")
    monkeypatch.setattr(s, "_tunnel_down", lambda: calls.append("tunnel_down"))
    s.run(dry=True, max_jobs=8)
    assert calls == []                    # dry-run guards the teardown behind `not dry`


def test_run_dry_run_with_work_does_not_start_rig(monkeypatch):
    _stub_run_env(monkeypatch)
    monkeypatch.setattr(s, "_fresh", lambda: [("mbx", "http://x")])
    calls = []
    monkeypatch.setattr(s, "_ensure_obs", lambda: calls.append("ensure") or "up")
    monkeypatch.setattr(s, "_tunnel_up", lambda: calls.append("tunnel_up") or True)
    s.run(dry=True, max_jobs=8)
    assert calls == []                    # dry-run reports intent, brings up nothing


def test_run_drive_ensures_obs_before_and_stops_after(monkeypatch):
    _stub_run_env(monkeypatch)
    order = []
    # _fresh: non-empty at the top, empty at the teardown re-check → queue "drains"
    seq = iter([[("mbx", "http://x")], []])
    monkeypatch.setattr(s, "_fresh", lambda: next(seq))
    monkeypatch.setattr(s, "_ensure_obs", lambda: order.append("ensure") or "up")
    monkeypatch.setattr(s, "_stop_obs", lambda: order.append("stop") or "stopped")
    monkeypatch.setattr(s, "_tunnel_down", lambda: order.append("tunnel_down"))
    # a partial drive (banked items, not completed) → no mailcrm import, just logs/retries
    monkeypatch.setattr(s, "_drive", lambda mbx, url: (False, 10, False, False, False))
    s.run(dry=False, max_jobs=8)
    assert order == ["ensure", "stop", "tunnel_down"]   # up before the loop, down after it drains
