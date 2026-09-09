"""«Мобильные прокси (телефоны)» — a POOL of phones (mostly iPhone, Android fallback) on the
project's Tailscale tailnet, each running a SOCKS server on a shared port, used as RESIDENTIAL /
mobile-carrier egress for the co-pilot instead of the Contabo datacenter IP.

Why: Ashby rejects every submit from the datacenter IP as spam (5/5 on 2026-09-09, «flagged as
possible spam»); a mobile-carrier IP is the cleanest residential egress. One phone was a start —
the owner will bring MANY, so this discovers every online tailnet peer automatically and
round-robins across the live ones (a different phone per application).

How it plugs in: `proxy_pool.residential_slots()` appends `live_servers()` (every phone answering
right now) to the loopback chisel slots, so everything that already PREFERS residential
(`next_proxy()` → the campaign cron's `_do_fill`, single fills, the bulk lane under
PARA_RESIDENTIAL=1) round-robins across the phones and falls back to the pool the moment none answer.

Settings: backend/data/mobile_proxy.json (gitignored)::

    {"enabled": true,
     "port": 1080,                                  # SOCKS port every phone listens on
     "discover": {"enabled": true, "match": ""},    # auto-find tailnet peers; ""=all, else a
                                                     #   substring of the hostname (e.g. "jf-")
     "manual": [{"server": "socks5://100.x.y.z:1080", "note": "iPhone Dana"}],
     "username": "", "password": "",                # only for http proxies (Chromium can't auth socks5)
     "cursor": 0}

An OLD single-`server` file (the first cut, commit 2efd140) is migrated to `manual:[…]` on load.

Dedicated tailnet: the server currently sits on a personal tailnet. Point it at a project-only
tailnet with a reusable auth key (NOT stored here):
    PYTHONPATH=. python3 -m backend.tools.mobile_proxy --join-tailnet <KEY> --yes

CLI:  --check          probe every endpoint: source · alive · egress IP · carrier/ASN
      --discover       what `tailscale status` yields right now
      --add socks5://100.x.y.z:1080 [--note "…"] / --rm socks5://… / --on / --off
      --tailnet-status current tailnet + peers
      --join-tailnet <KEY> [--yes]   switch this node to a project tailnet (owner-run)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

_PATH = Path(__file__).resolve().parent.parent / "data" / "mobile_proxy.json"
_LOCK = threading.Lock()
_ECHO_URL = "http://api.ipify.org?format=json"
# full status (with an egress fetch per phone) is expensive -> cache 60s
_CACHE: dict = {"ts": 0.0, "status": None}
_CACHE_TTL = 60.0
# the hot path (proxy_pool asks for live servers per fill) is TCP-only + a short cache
_LIVE_CACHE: dict = {"ts": 0.0, "servers": []}
_LIVE_TTL = 15.0
# discovery runs a subprocess -> its own short cache
_DISC_CACHE: dict = {"ts": 0.0, "endpoints": [], "tailnet": ""}
_DISC_TTL = 30.0
_DEFAULT_PORT = 1080


# ---- settings ------------------------------------------------------------------------------------
def _migrate(d: dict) -> dict:
    """Bring any older shape up to the pool schema. The first cut (2efd140) stored a single
    top-level `server`/`note`; fold it into `manual`."""
    if not isinstance(d, dict):
        return _blank()
    if "manual" not in d:
        manual = []
        old = (d.pop("server", "") or "").strip()
        if old:
            manual.append({"server": normalize_server(old), "note": (d.pop("note", "") or "").strip()})
        d["manual"] = manual
    d.setdefault("enabled", False)
    d.setdefault("port", _DEFAULT_PORT)
    disc = d.get("discover")
    if not isinstance(disc, dict):
        d["discover"] = {"enabled": True, "match": ""}
    else:
        disc.setdefault("enabled", True)
        disc.setdefault("match", "")
    d.setdefault("username", "")
    d.setdefault("password", "")
    d.setdefault("cursor", 0)
    # sanitise manual entries
    clean = []
    for m in d.get("manual") or []:
        if isinstance(m, str):
            m = {"server": m, "note": ""}
        srv = normalize_server((m or {}).get("server") or "")
        if srv:
            clean.append({"server": srv, "note": ((m or {}).get("note") or "").strip()})
    d["manual"] = clean
    return d


def _blank() -> dict:
    return {"enabled": False, "port": _DEFAULT_PORT, "discover": {"enabled": True, "match": ""},
            "manual": [], "username": "", "password": "", "cursor": 0}


def load() -> dict:
    try:
        d = json.loads(_PATH.read_text())
    except Exception:
        return _blank()
    return _migrate(d if isinstance(d, dict) else {})


def save(d: dict) -> None:
    _PATH.parent.mkdir(exist_ok=True)
    tmp = _PATH.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    os.replace(tmp, _PATH)
    _invalidate()


def _invalidate() -> None:
    _CACHE["ts"] = 0.0
    _LIVE_CACHE["ts"] = 0.0


def normalize_server(s: str) -> str:
    """'100.1.2.3:1080' -> 'socks5://100.1.2.3:1080'; keeps an explicit scheme; drops embedded
    creds into the URL untouched (proxy_dict re-splits them)."""
    s = (s or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "socks5://" + s
    return s


def _parse(server: str):
    u = urlsplit(server)
    scheme = (u.scheme or "socks5").lower()
    port = int(u.port or (1080 if scheme.startswith("socks") else 8080))
    return scheme, u.hostname or "", port, u.username, u.password


def _hostport(server: str) -> str:
    _s, h, p, _u, _pw = _parse(server)
    return f"{h}:{p}"


def update(*, enabled: bool | None = None, server: str | None = None, note: str | None = None,
           add: str | None = None, remove: str | None = None, port: int | None = None,
           discover_enabled: bool | None = None, match: str | None = None,
           username: str | None = None, password: str | None = None) -> dict:
    """Mutate the settings. `server`/`add` append a manual endpoint (deduped by host:port);
    `remove` drops one; `enabled` is the master on/off. Kept backward-compatible with the old
    `update(enabled=, server=, note=)` signature the dashboard route uses."""
    with _LOCK:
        d = load()
        if enabled is not None:
            d["enabled"] = bool(enabled)
        if port is not None:
            try:
                d["port"] = max(1, min(int(port), 65535))
            except (TypeError, ValueError):
                pass
        if discover_enabled is not None:
            d["discover"]["enabled"] = bool(discover_enabled)
        if match is not None:
            d["discover"]["match"] = match.strip()
        if username is not None:
            d["username"] = username.strip()
        if password is not None:
            d["password"] = password
        for srv in (server, add):
            if srv and srv.strip():
                ns = normalize_server(srv)
                hp = _hostport(ns)
                if not any(_hostport(m["server"]) == hp for m in d["manual"]):
                    d["manual"].append({"server": ns, "note": (note or "").strip()})
        if remove and remove.strip():
            hp = _hostport(normalize_server(remove))
            d["manual"] = [m for m in d["manual"] if _hostport(m["server"]) != hp]
        save(d)
        return d


# ---- discovery (tailscale peers) -----------------------------------------------------------------
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _tailscale_status_json(timeout: float = 4.0) -> dict:
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                             text=True, timeout=timeout)
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout)
    except Exception:
        pass
    return {}


def tailnet_name() -> str:
    st = _tailscale_status_json()
    ct = (st.get("CurrentTailnet") or {}).get("Name")
    return ct or st.get("MagicDNSSuffix") or ""


def peers() -> list[dict]:
    """Every peer (not Self): {name, ip, online, os}. [] on any failure."""
    st = _tailscale_status_json()
    out = []
    for _k, p in (st.get("Peer") or {}).items():
        ip = next((a for a in (p.get("TailscaleIPs") or []) if _IPV4.match(a)), "")
        out.append({"name": (p.get("HostName") or p.get("DNSName") or "").split(".")[0],
                    "dns": (p.get("DNSName") or ""), "ip": ip,
                    "online": bool(p.get("Online")), "os": p.get("OS") or ""})
    return out


def discover_endpoints(force: bool = False) -> list[str]:
    """socks5://<ip>:<port> for every ONLINE tailnet peer matching discover.match. Cached 30s
    (a subprocess), guarded (return [] on any failure)."""
    d = load()
    disc = d.get("discover") or {}
    if not disc.get("enabled", True):
        return []
    now = time.time()
    if not force and now - _DISC_CACHE["ts"] < _DISC_TTL:
        return list(_DISC_CACHE["endpoints"])
    port = int(d.get("port") or _DEFAULT_PORT)
    match = (disc.get("match") or "").strip().lower()
    eps = []
    for p in peers():
        if not p["online"] or not p["ip"]:
            continue
        hay = (p["name"] + " " + p["dns"]).lower()
        if match and match not in hay:
            continue
        eps.append(f"socks5://{p['ip']}:{port}")
    _DISC_CACHE.update(ts=now, endpoints=eps, tailnet=tailnet_name())
    return list(eps)


# ---- the endpoint pool ---------------------------------------------------------------------------
def manual_servers() -> list[str]:
    return [m["server"] for m in load().get("manual") or []]


def all_endpoints(force_discover: bool = False) -> list[dict]:
    """Manual ∪ discovered, deduped by host:port (manual wins the label). Each: {server, source}."""
    out, seen = [], set()
    for m in load().get("manual") or []:
        hp = _hostport(m["server"])
        if hp not in seen:
            seen.add(hp)
            out.append({"server": m["server"], "source": "manual", "note": m.get("note") or ""})
    for srv in discover_endpoints(force=force_discover):
        hp = _hostport(srv)
        if hp not in seen:
            seen.add(hp)
            out.append({"server": srv, "source": "discovered", "note": ""})
    return out


def _creds():
    d = load()
    return (d.get("username") or None), (d.get("password") or None)


def tcp_alive(server: str, timeout: float = 3.0) -> bool:
    try:
        _s, host, port, _u, _p = _parse(server)
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except Exception:
        return False


def _proxy_url(server: str) -> str:
    """server + the shared http creds folded into the URL (socks5 stays auth-less)."""
    user, pw = _creds()
    scheme, host, port, u_user, u_pass = _parse(server)
    user, pw = (u_user or user), (u_pass or pw)
    if user and scheme.startswith("http"):
        return f"{scheme}://{user}:{pw or ''}@{host}:{port}"
    return f"{scheme}://{host}:{port}"


def _echo_through(server: str, timeout: float = 8.0, echo=None):
    """Egress IP seen THROUGH `server` (or None). `echo` injectable for tests."""
    if echo is not None:
        return echo(server)
    import httpx
    try:
        r = httpx.get(_ECHO_URL, proxy=_proxy_url(server), timeout=timeout)
        if r.status_code == 200:
            return (r.json() or {}).get("ip") or None
    except Exception:
        return None
    return None


def live_servers(timeout: float = 1.2) -> list[str]:
    """The hot path (proxy_pool asks per fill): every enabled endpoint answering TCP right now,
    probed concurrently, cached ~15s. Cheap — NO egress fetch here."""
    d = load()
    if not d.get("enabled"):
        return []
    now = time.time()
    if now - _LIVE_CACHE["ts"] < _LIVE_TTL:
        return list(_LIVE_CACHE["servers"])
    servers = [e["server"] for e in all_endpoints()]
    live = []
    if servers:
        with ThreadPoolExecutor(max_workers=min(8, len(servers))) as ex:
            for srv, ok in zip(servers, ex.map(lambda s: tcp_alive(s, timeout=timeout), servers)):
                if ok:
                    live.append(srv)
    _LIVE_CACHE.update(ts=now, servers=live)
    return list(live)


def live_endpoints(force: bool = False, echo=None) -> list[dict]:
    """Full probe of every endpoint (TCP + an egress fetch through it), concurrent, for the UI /
    health / --check. Each: {server, source, alive, egress}."""
    eps = all_endpoints(force_discover=force)

    def probe(e):
        alive = tcp_alive(e["server"])
        egress = _echo_through(e["server"], echo=echo) if alive else None
        return {**e, "alive": bool(egress), "egress": egress}

    if not eps:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(eps))) as ex:
        return list(ex.map(probe, eps))


def next_mobile() -> dict | None:
    """Round-robin the LIVE endpoints (persist the cursor). {server, username, password} or None.
    The apply path normally rotates through proxy_pool.residential_proxy() (which merges these
    slots); this is the direct accessor + what the CLI/tests use."""
    live = live_servers()
    if not live:
        return None
    with _LOCK:
        d = load()
        i = int(d.get("cursor", 0)) % len(live)
        d["cursor"] = (i + 1) % len(live)
        save(d)
    srv = live[i]
    user, pw = _creds()
    _s, _h, _p, u_user, u_pass = _parse(srv)
    return {"server": srv, "username": (u_user or (user if _s.startswith("http") else None)),
            "password": (u_pass or (pw if _s.startswith("http") else None))}


def status(force: bool = False, echo=None) -> dict:
    """Aggregate for the UI/health, cached 60s: {configured, enabled, n_configured, n_online,
    endpoints:[…], egress_samples:[…], tailnet, checked_at}."""
    now = time.time()
    if not force and _CACHE["status"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return json.loads(json.dumps(_CACHE["status"]))
    d = load()
    eps = live_endpoints(force=force, echo=echo) if d.get("enabled") else \
        [{**e, "alive": False, "egress": None} for e in all_endpoints(force_discover=force)]
    online = [e for e in eps if e.get("alive")]
    st = {"configured": bool(eps), "enabled": bool(d.get("enabled")),
          "n_configured": len(eps), "n_online": len(online),
          "endpoints": eps, "egress_samples": [e["egress"] for e in online if e.get("egress")][:5],
          "discover_on": bool((d.get("discover") or {}).get("enabled", True)),
          "match": (d.get("discover") or {}).get("match", ""), "port": int(d.get("port") or _DEFAULT_PORT),
          "tailnet": _DISC_CACHE.get("tailnet") or tailnet_name(), "checked_at": now}
    _CACHE.update(ts=now, status=json.loads(json.dumps(st)))
    return st


def egress_ip() -> str | None:
    s = status().get("egress_samples") or []
    return s[0] if s else None


def asn_of(ip: str, timeout: float = 5.0) -> str:
    """Best-effort 'org · city, country' for an egress IP (ipinfo.io, no key) — tells a mobile
    carrier from a datacenter. '' on failure."""
    import httpx
    try:
        r = httpx.get(f"https://ipinfo.io/{ip}/json", timeout=timeout)
        if r.status_code == 200:
            j = r.json()
            return f"{j.get('org', '')} · {j.get('city', '')}, {j.get('country', '')}".strip(" ·,")
    except Exception:
        pass
    return ""


# ---- dedicated-tailnet join (owner-run; no key stored) -------------------------------------------
def logout_argv() -> list[str]:
    return ["sudo", "tailscale", "logout"]


def join_argv(authkey: str, hostname: str = "jobfinder-server") -> list[str]:
    """The exact `tailscale up` argv to move THIS node onto a project tailnet with a reusable auth
    key. Pure — building it never stores or logs the key (the CLI masks it when echoing)."""
    return ["sudo", "tailscale", "up", f"--authkey={authkey}", f"--hostname={hostname}",
            "--accept-routes=false", "--advertise-exit-node=false", "--reset", "--ssh=false"]


def _mask_key(argv: list[str]) -> list[str]:
    return [(a.split("=", 1)[0] + "=***" if a.startswith("--authkey=") else a) for a in argv]


# ---- CLI -----------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Мобильные прокси (телефоны через Tailscale) — пул/статус")
    ap.add_argument("--check", action="store_true", help="probe every endpoint: alive · egress · carrier")
    ap.add_argument("--discover", action="store_true", help="what `tailscale status` yields now")
    ap.add_argument("--add", metavar="SERVER", help="add a manual endpoint, e.g. socks5://100.x.y.z:1080")
    ap.add_argument("--set", metavar="SERVER", help="alias of --add (back-compat)")
    ap.add_argument("--rm", metavar="SERVER", help="remove a manual endpoint")
    ap.add_argument("--note", default=None)
    ap.add_argument("--match", default=None, help="discovery hostname filter (''=all)")
    ap.add_argument("--port", type=int, default=None, help="shared SOCKS port (default 1080)")
    ap.add_argument("--on", action="store_true")
    ap.add_argument("--off", action="store_true")
    ap.add_argument("--tailnet-status", action="store_true", help="current tailnet + peers")
    ap.add_argument("--join-tailnet", metavar="AUTHKEY", help="switch this node to a project tailnet")
    ap.add_argument("--yes", action="store_true", help="actually run --join-tailnet (else dry-run)")
    a = ap.parse_args()

    mutated = False
    if a.add or a.set or a.rm or a.on or a.off or a.note is not None or a.match is not None or a.port is not None:
        d = update(add=a.add or a.set, remove=a.rm, note=a.note, match=a.match, port=a.port,
                   enabled=(True if a.on else (False if a.off else None)))
        mutated = True
        print(json.dumps({"enabled": d["enabled"], "port": d["port"],
                          "discover": d["discover"], "manual": d["manual"]}, ensure_ascii=False))

    if a.tailnet_status:
        print(json.dumps({"tailnet": tailnet_name(), "peers": peers()}, ensure_ascii=False, indent=1))
        return

    if a.join_tailnet:
        argv = join_argv(a.join_tailnet)
        print("Would run:")
        print("  " + " ".join(logout_argv()))
        print("  " + " ".join(_mask_key(argv)))
        print("NOTE: this DROPS the current tailnet membership (this node is project-only, fine).")
        if not a.yes:
            print("Dry-run. Re-run with --yes to execute.")
            return
        try:
            subprocess.run(logout_argv(), check=False, timeout=30)
            r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
            # NEVER echo the key: mask stdout/stderr defensively too
            def scrub(s):
                return re.sub(r"(--authkey=)\S+", r"\1***", s or "")
            print(scrub(r.stdout))
            if r.returncode != 0:
                print("ERR:", scrub(r.stderr))
        except Exception as exc:
            print("join failed:", re.sub(r"(authkey=?)\S+", r"\1***", str(exc)))
        return

    if a.discover:
        print(json.dumps({"tailnet": tailnet_name(),
                          "discovered": discover_endpoints(force=True),
                          "peers": peers()}, ensure_ascii=False, indent=1))
        return

    if a.check or not mutated:
        eps = live_endpoints(force=True)
        for e in eps:
            asn = asn_of(e["egress"]) if e.get("egress") else ""
            print(f"[{e['source']:>10}] {e['server']:<28} "
                  f"{'ONLINE' if e['alive'] else 'offline':>7} "
                  f"{e.get('egress') or '-':<16} {asn}")
        online = sum(1 for e in eps if e["alive"])
        print(f"-- {online}/{len(eps)} phones online · tailnet {tailnet_name() or '?'}")


if __name__ == "__main__":
    main()
