"""Self-healing watcher — the PURE core: the failure-mode classifier and the cooldown / circuit
breaker (+ alert throttle). Network-free, no subprocess, no gather(). Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_health_heal.py -q
"""
from backend.tools import health_heal as H


def _row(name, status, detail=""):
    return {"name": name, "status": status, "detail": detail}


def _snap(*sections):
    return {"sections": [{"key": k, "rows": rows} for k, rows in sections], "overall": "down"}


# --------------------------------------------------------------------------- classifier: pm2
def test_pm2_jobfinder_down_restarts():
    a = H.classify_row("pm2", _row("jobfinder-alan-dash", "down", "errored · аптайм 2 мин"))
    assert a["kind"] == "heal" and a["mode"] == "pm2_restart"
    assert a["command"] == ["pm2", "restart", "jobfinder-alan-dash"]
    assert a["target"] == "pm2:jobfinder-alan-dash"


def test_pm2_crash_loop_still_a_restart_action():
    a = H.classify_row("pm2", _row("jobfinder-alan-copilot", "down", "3 перезапуска за час (падает?) · ..."))
    assert a["mode"] == "pm2_restart"          # the breaker (not the classifier) governs the storm


def test_pm2_cwd_mismatch_is_alert_not_restart():
    a = H.classify_row("pm2", _row("jobfinder-alan-dash", "down", "cwd /home/projects/JOBFINDER ≠ живой чекаут"))
    assert a["kind"] == "alert" and a["mode"] == "pm2_manual" and a["command"] is None


def test_pm2_process_removed_is_alert():
    a = H.classify_row("pm2", _row("jobfinder-mail-indexer", "down", "не найден в pm2 (процесс удалён?)"))
    assert a["kind"] == "alert" and a["command"] is None


def test_pm2_jobfinder_ok_or_warn_is_noop():
    assert H.classify_row("pm2", _row("jobfinder-alan-dash", "ok", "online")) is None
    assert H.classify_row("pm2", _row("jobfinder-alan-dash", "warn", "перезапуск ...")) is None


def test_pm2_layer_failure_is_alert_only():
    a = H.classify_row("pm2", _row("pm2", "down", "pm2 jlist не отвечает"))
    assert a["kind"] == "alert" and a["target"] == "pm2:layer"


# --------------------------------------------------------------------------- classifier: LLM
def test_llm_unreachable_restarts_llm_server():
    a = H.classify_row("deps", _row("Локальная модель", "down", "не отвечает: ConnectError"))
    assert a["kind"] == "heal" and a["mode"] == "llm_restart"
    assert a["command"] == ["pm2", "restart", "llm-server"]


def test_llm_http_error_is_alert_not_restart():
    # reachable but 500 (expired owner token) — a restart won't fix it and must NOT loop
    a = H.classify_row("deps", _row("Локальная модель", "down", "127.0.0.1:8080 · HTTP 500"))
    assert a["kind"] == "alert" and a["mode"] == "llm_token" and a["command"] is None


def test_llm_warn_is_noop():
    assert H.classify_row("deps", _row("Локальная модель", "warn", "модель не в списке")) is None


# --------------------------------------------------------------------------- classifier: egress / mac / chrome
def test_dead_egress_slot_syncs():
    a = H.classify_row("deps", _row("Exit-node мост (телефоны)", "warn",
                                    "2 слотов, но ни один демон не жив (телефон отвалился?)"))
    assert a["kind"] == "heal" and a["mode"] == "egress_sync"
    assert a["command"][:3] == [H.sys.executable, "-m", "backend.tools.tailscale_egress"]
    assert "--sync" in a["command"]


def test_healthy_egress_is_noop():
    assert H.classify_row("deps", _row("Exit-node мост (телефоны)", "ok", "1 слот активно")) is None
    assert H.classify_row("deps", _row("Exit-node мост (телефоны)", "info", "выключен · 0 слотов")) is None


def test_mac_tunnel_down_syncs_with_same_command_as_egress():
    a = H.classify_row("assessments", _row("Лэйн Mac/Sutherland", "down",
                                           "лэйн запущен, но Mac НЕ отвечает (спит/офлайн)"))
    assert a["kind"] == "heal" and a["mode"] == "mac_tunnel"
    # SAME command as the dead-slot sync → the executor de-dupes them to one run per tick
    egress = H.classify_row("deps", _row("Exit-node мост (телефоны)", "warn", "1 слотов, но ни один демон не жив"))
    assert a["command"] == egress["command"]


def test_mac_lane_warn_is_noop():
    assert H.classify_row("assessments", _row("Лэйн Mac/Sutherland", "warn", "холостой прогон")) is None


def test_too_many_chromium_reaps():
    a = H.classify_row("system", _row("Chromium (браузеры JobFinder)", "warn", "наших 12 · всего 20"))
    assert a["kind"] == "heal" and a["mode"] == "chrome_reap"
    assert a["command"][1:3] == ["-m", "backend.tools.chrome_reaper"]
    assert "--min-age" in a["command"]


# --------------------------------------------------------------------------- classify() whole snapshot
def test_unknown_down_row_becomes_generic_alert():
    snap = _snap(("data", [_row("Postgres jobfinder_crm", "down", "недоступна: OperationalError")]))
    acts = H.classify(snap)
    assert len(acts) == 1 and acts[0]["kind"] == "alert" and acts[0]["mode"] == "alert"
    assert acts[0]["target"] == "down:data:Postgres jobfinder_crm"


def test_known_mode_not_double_alerted():
    # a down LLM row is a known mode → exactly ONE action (its own), never also a generic "unknown down"
    snap = _snap(("deps", [_row("Локальная модель", "down", "не отвечает: ConnectError")]))
    acts = H.classify(snap)
    assert len(acts) == 1 and acts[0]["mode"] == "llm_restart"


def test_incidents_section_never_actionable():
    snap = _snap(("incidents", [_row("Postgres lock", "down", "info row")]))
    assert H.classify(snap) == []


def test_heals_ordered_before_alerts():
    snap = _snap(
        ("pm2", [_row("jobfinder-alan-dash", "down", "errored")]),
        ("data", [_row("Maildir takhet.com", "down", "нет доставок")]),
    )
    acts = H.classify(snap)
    assert acts[0]["kind"] == "heal" and acts[-1]["kind"] == "alert"


# --------------------------------------------------------------------------- cooldown + circuit breaker
def test_fresh_target_is_healable():
    lg = H.HealLedger()
    d, _ = lg.decision("pm2:x", now=1000)
    assert d == "heal"


def test_cooldown_blocks_a_second_heal_soon():
    lg = H.HealLedger()
    lg.record("pm2:x", now=1000)
    d, _ = lg.decision("pm2:x", now=1000 + 120, cooldown=300, max_heals=3, window=3600)
    assert d == "cooldown"
    d2, _ = lg.decision("pm2:x", now=1000 + 400, cooldown=300, max_heals=3, window=3600)
    assert d2 == "heal"


def test_breaker_trips_after_max_heals_in_window():
    lg = H.HealLedger()
    for t in (0, 400, 800):                      # 3 heals within a 3600s window
        lg.record("pm2:x", now=t)
    d, why = lg.decision("pm2:x", now=1200, cooldown=300, max_heals=3, window=3600)
    assert d == "breaker" and "предохранитель" in why


def test_breaker_resets_once_attempts_age_out():
    lg = H.HealLedger()
    for t in (0, 400, 800):
        lg.record("pm2:x", now=t)
    # all three attempts are now older than the window → breaker closes, healable again
    d, _ = lg.decision("pm2:x", now=800 + 3601, cooldown=300, max_heals=3, window=3600)
    assert d == "heal"


def test_breaker_checked_before_cooldown():
    lg = H.HealLedger()
    for t in (1000, 1300, 1600):
        lg.record("pm2:x", now=t)
    # only 50s since the last attempt (< cooldown) AND at the cap → breaker wins, not cooldown
    d, _ = lg.decision("pm2:x", now=1650, cooldown=300, max_heals=3, window=3600)
    assert d == "breaker"


def test_targets_are_independent():
    lg = H.HealLedger()
    for t in (0, 400, 800):
        lg.record("pm2:a", now=t)
    assert lg.decision("pm2:a", now=1000, max_heals=3, window=3600)[0] == "breaker"
    assert lg.decision("pm2:b", now=1000, max_heals=3, window=3600)[0] == "heal"


# --------------------------------------------------------------------------- alert throttle
def test_alert_throttle_dedupes_identical_state():
    lg = H.HealLedger()
    assert lg.should_alert("heal", "sigA", now=0, cooldown=14400) is True
    lg.mark_alert("heal", "sigA", now=0)
    assert lg.should_alert("heal", "sigA", now=100, cooldown=14400) is False    # same state, throttled
    assert lg.should_alert("heal", "sigB", now=100, cooldown=14400) is True     # state changed → send
    assert lg.should_alert("heal", "sigA", now=14401, cooldown=14400) is True   # cooldown elapsed → send


# --------------------------------------------------------------------------- dry-run executor (hermetic)
def test_dry_run_plans_but_changes_nothing(monkeypatch):
    saved = {"n": 0}
    monkeypatch.setattr(H, "_load_state", lambda: {})
    monkeypatch.setattr(H, "_save_state", lambda s: saved.__setitem__("n", saved["n"] + 1))
    monkeypatch.setattr(H, "_run_cmd", lambda c: (_ for _ in ()).throw(AssertionError("must not run in dry-run")))
    monkeypatch.setattr(H.health, "_tg", lambda m: (_ for _ in ()).throw(AssertionError("must not send in dry-run")))
    snap = _snap(
        ("pm2", [_row("jobfinder-alan-dash", "down", "errored")]),
        ("deps", [_row("Локальная модель", "down", "127.0.0.1:8080 · HTTP 500")]),
    )
    res = H.heal(dry_run=True, snapshot=snap)
    assert res["dry_run"] is True
    assert any("jobfinder-alan-dash" in f for f in res["fixed"])   # planned
    assert res["unresolved"] == ["llm:token"]                      # token issue surfaced, not restarted
    assert saved["n"] == 0                                         # no state write in dry-run
