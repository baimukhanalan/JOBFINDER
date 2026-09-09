"""«Мобильный прокси» — the owner's phone (its mobile-data IP) as a RESIDENTIAL egress for the
co-pilot, reached over the Tailscale tailnet (100.x.y.z) instead of the chisel loopback slots
(backend/tools/residential_proxy/, which need a Mac/Windows binary the phone can't run).

Why: Ashby rejects every submit from the Contabo datacenter IP as spam (5/5 on 2026-09-09,
«flagged as possible spam»); a mobile-carrier IP is the cleanest residential egress there is.

How it plugs in: `proxy_pool.residential_slots()` appends the live endpoints listed here, so
everything that already PREFERS residential (`next_proxy()` → the campaign cron's `_do_fill`,
single fills, the bulk lane under PARA_RESIDENTIAL=1) routes through the phone the moment its
SOCKS server answers on the tailnet — and falls back to the pool the moment it doesn't.

Settings: backend/data/mobile_proxy.json (gitignored):
    {"enabled": true, "server": "socks5://100.x.y.z:1080", "username": "", "password": "",
     "note": "iPhone, iSH microsocks"}
`server` may also be "http://user:pass@host:port" (Chromium authenticates http proxies; it can NOT
authenticate socks5 — a socks5 endpoint must be auth-less, which is safe inside the tailnet).

CLI:  PYTHONPATH=. python3 -m backend.tools.mobile_proxy --check     # alive · egress IP · ASN
      PYTHONPATH=. python3 -m backend.tools.mobile_proxy --set socks5://100.x.y.z:1080 [--off|--on]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

_PATH = Path(__file__).resolve().parent.parent / "data" / "mobile_proxy.json"
_LOCK = threading.Lock()
_ECHO_URL = "http://api.ipify.org?format=json"
_CACHE: dict = {"ts": 0.0, "status": None}
_CACHE_TTL = 60.0


# ---- settings ------------------------------------------------------------------------------------
def load() -> dict:
    try:
        d = json.loads(_PATH.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(d: dict) -> None:
    _PATH.parent.mkdir(exist_ok=True)
    tmp = _PATH.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    os.replace(tmp, _PATH)
    _CACHE["ts"] = 0.0          # a settings change invalidates the cached probe


def update(*, enabled: bool | None = None, server: str | None = None, username: str | None = None,
           password: str | None = None, note: str | None = None) -> dict:
    with _LOCK:
        d = load()
        if enabled is not None:
            d["enabled"] = bool(enabled)
        if server is not None:
            d["server"] = normalize_server(server)
        if username is not None:
            d["username"] = username.strip()
        if password is not None:
            d["password"] = password
        if note is not None:
            d["note"] = note.strip()
        save(d)
        return d


def normalize_server(s: str) -> str:
    """'100.1.2.3:1080' -> 'socks5://100.1.2.3:1080'; keeps an explicit scheme."""
    s = (s or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "socks5://" + s
    return s


def _parse(server: str) -> tuple[str, str, int, str | None, str | None]:
    u = urlsplit(server)
    scheme = (u.scheme or "socks5").lower()
    return scheme, u.hostname or "", int(u.port or (1080 if scheme.startswith("socks") else 8080)), u.username, u.password


def proxy_dict() -> dict | None:
    """The configured endpoint as the {server, username, password} shape the co-pilot expects,
    or None when disabled / unset. Credentials in the URL win over the separate fields."""
    d = load()
    if not d.get("enabled") or not d.get("server"):
        return None
    scheme, host, port, u_user, u_pass = _parse(d["server"])
    user = u_user or (d.get("username") or None)
    pw = u_pass or (d.get("password") or None)
    return {"server": f"{scheme}://{host}:{port}", "username": user, "password": pw}


# ---- probes --------------------------------------------------------------------------------------
def tcp_alive(server: str, timeout: float = 3.0) -> bool:
    try:
        _s, host, port, _u, _p = _parse(server)
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except Exception:
        return False


def _echo_through(proxy: dict, timeout: float = 8.0) -> str | None:
    """Fetch the IP-echo THROUGH the proxy (httpx; socks5 via the socksio extra). None on failure."""
    import httpx
    srv = proxy["server"]
    if proxy.get("username"):
        scheme, rest = srv.split("://", 1)
        srv = f"{scheme}://{proxy['username']}:{proxy.get('password') or ''}@{rest}"
    try:
        r = httpx.get(_ECHO_URL, proxy=srv, timeout=timeout)
        if r.status_code == 200:
            return (r.json() or {}).get("ip") or None
    except Exception:
        return None
    return None


def status(force: bool = False, echo=None) -> dict:
    """{configured, enabled, server, alive, egress, checked_at, note} — cached 60s (the probe
    makes a real request through the phone). `echo` is injectable for tests."""
    now = time.time()
    if not force and _CACHE["status"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return dict(_CACHE["status"])
    d = load()
    st = {"configured": bool(d.get("server")), "enabled": bool(d.get("enabled")),
          "server": d.get("server") or "", "note": d.get("note") or "", "alive": False,
          "egress": None, "checked_at": now}
    px = proxy_dict()
    if px:
        st["alive"] = tcp_alive(px["server"])
        if st["alive"]:
            st["egress"] = (echo or _echo_through)(px)
            st["alive"] = bool(st["egress"])          # a SOCKS that answers TCP but can't route = down
    _CACHE.update(ts=now, status=dict(st))
    return st


def live_servers(timeout: float = 1.5) -> list[str]:
    """The enabled endpoint(s) that answer TCP right now — what proxy_pool.residential_slots()
    appends to the loopback chisel slots (cheap; no echo fetch on this hot path)."""
    px = proxy_dict()
    if not px:
        return []
    return [px["server"]] if tcp_alive(px["server"], timeout=timeout) else []


def egress_ip() -> str | None:
    return status().get("egress")


def asn_of(ip: str, timeout: float = 5.0) -> str:
    """Best-effort 'AS… <org>' for an egress IP (ipinfo.io, no key) — tells a mobile/residential
    carrier from a datacenter at a glance. '' on failure."""
    import httpx
    try:
        r = httpx.get(f"https://ipinfo.io/{ip}/json", timeout=timeout)
        if r.status_code == 200:
            j = r.json()
            return f"{j.get('org', '')} · {j.get('city', '')}, {j.get('country', '')}".strip(" ·,")
    except Exception:
        pass
    return ""


# ---- CLI -----------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Мобильный прокси (телефон через Tailscale) — статус/настройка")
    ap.add_argument("--check", action="store_true", help="probe: alive, egress IP, ASN (forces a fresh probe)")
    ap.add_argument("--set", metavar="SERVER", help="set the endpoint, e.g. socks5://100.x.y.z:1080")
    ap.add_argument("--on", action="store_true")
    ap.add_argument("--off", action="store_true")
    ap.add_argument("--note", default=None)
    a = ap.parse_args()
    if a.set or a.on or a.off or a.note is not None:
        d = update(server=a.set, enabled=(True if a.on else (False if a.off else None)), note=a.note)
        print(json.dumps({"server": d.get("server"), "enabled": d.get("enabled"), "note": d.get("note")},
                         ensure_ascii=False))
    if a.check or not (a.set or a.on or a.off or a.note is not None):
        st = status(force=True)
        if st.get("egress"):
            st["asn"] = asn_of(st["egress"])
        print(json.dumps(st, ensure_ascii=False))


if __name__ == "__main__":
    main()
