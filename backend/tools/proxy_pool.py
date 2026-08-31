"""Proxy pool for the apply engine: parse an uploaded proxy list, validate each
(dropping the dead ones immediately), persist the survivors, and hand them out
round-robin so every application goes out from a different egress IP.

Accepted line formats (one proxy per line, blank/`#` lines ignored):

    host:port                       no auth, http
    host:port:user:pass             http, colon form (common provider export)
    user:pass@host:port             http
    scheme://host:port
    scheme://user:pass@host:port    scheme ∈ http, https, socks5, socks5h, socks4

VALIDATION: http/https proxies are checked by fetching an IP-echo endpoint
THROUGH the proxy (records the real egress IP). socks5 is only TCP-probed —
httpx has no socks transport installed here, and the Playwright browser can't
authenticate socks5 anyway (so socks5-with-auth won't route a real submit).

Pool file: backend/data/proxies.json (gitignored — proxy credentials).
Cursor persists so round-robin survives process restarts.
"""
import asyncio
import json
import logging
import socket
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_STORE = Path(__file__).resolve().parents[1] / "data" / "proxies.json"
_LOCK = threading.Lock()

# The owner's home machines, each reverse-tunnelled to the server (chisel over 443/WSS) as a
# RESIDENTIAL egress on a loopback SLOT in 127.0.0.1:8120..8129 (one per connected machine; see
# backend/tools/residential_proxy/). Each slot is a no-auth SOCKS5 proxy (chisel reverse socks).
# We TCP-probe the whole range (cached) and, when any slot is up, PREFER residential over the
# datacenter Bright Data pool so blocked ATSes (Teleperformance/iCIMS, Kelly/Akamai, reCAPTCHA-
# Greenhouse) see a home IP — round-robining across ALL live slots so several laptops share load and
# a dropped one stops being used. No auth (reachable only via the tunnels; Playwright accepts a
# no-auth loopback SOCKS5). When zero slots are up, next_proxy() falls back to the Bright Data pool.
_RES_HOST = "127.0.0.1"
_RES_BASE, _RES_COUNT = 8120, 10
_res_cache = {"ts": 0.0, "slots": []}
_res_cursor = 0


def residential_slots() -> list[str]:
    """The live residential slot proxy servers (one per connected machine). Cached ~8s so
    next_proxy() can consult it per fill without probing 10 ports each time."""
    now = time.time()
    if now - _res_cache["ts"] < 8:
        return _res_cache["slots"]
    live = []
    for port in range(_RES_BASE, _RES_BASE + _RES_COUNT):
        try:
            socket.create_connection((_RES_HOST, port), timeout=0.4).close()
            live.append(f"socks5://{_RES_HOST}:{port}")
        except Exception:
            pass
    _res_cache.update(ts=now, slots=live)
    return live


def residential_up() -> bool:
    """True if at least one residential slot (connected machine) is available."""
    return bool(residential_slots())


def residential_proxy() -> dict | None:
    """The NEXT live residential slot as a {server, username, password} proxy dict (round-robin
    across connected machines), or None if none are up. No auth — loopback tunnel only."""
    global _res_cursor
    slots = residential_slots()
    if not slots:
        return None
    with _LOCK:
        srv = slots[_res_cursor % len(slots)]
        _res_cursor += 1
    return {"server": srv, "username": None, "password": None}
# Plain http (not https) so an http proxy simply forwards it — https CONNECT is
# occasionally blocked and would false-flag a working proxy as dead.
_ECHO_URL = "http://api.ipify.org?format=json"
_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")


def _norm_one(line: str) -> dict | None:
    """Parse a single line into {scheme, host, port, server[, username, password]}.
    Returns None when it can't be read as host+port."""
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None
    scheme = "http"
    if "://" in s:
        head, s = s.split("://", 1)
        if head.strip().lower() in _SCHEMES:
            scheme = head.strip().lower()
    user = pw = host = port = None
    if "@" in s:                        # user:pass@host:port
        cred, hostpart = s.rsplit("@", 1)
        if ":" in cred:
            user, pw = cred.split(":", 1)
        else:
            user = cred
        hp = hostpart.split(":")
        if len(hp) >= 2:
            host, port = hp[0], hp[1]
    else:
        parts = s.split(":")
        if len(parts) == 4:             # host:port:user:pass
            host, port, user, pw = parts
        elif len(parts) >= 2:           # host:port (ignore any trailing junk)
            host, port = parts[0], parts[1]
    if not host or not port:
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if not (0 < port < 65536):
        return None
    out = {"scheme": scheme, "host": host, "port": port,
           "server": f"{scheme}://{host}:{port}"}
    if user:
        out["username"] = user
        out["password"] = pw or ""
    return out


def parse_proxies(text: str) -> list[dict]:
    """Parse a pasted list, de-duplicating on (server, username)."""
    seen: set = set()
    out: list[dict] = []
    for line in (text or "").splitlines():
        p = _norm_one(line)
        if not p:
            continue
        key = (p["server"], p.get("username") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _httpx_proxy_url(p: dict) -> str:
    auth = f"{p['username']}:{p.get('password', '')}@" if p.get("username") else ""
    return f"{p['scheme']}://{auth}{p['host']}:{p['port']}"


async def _tcp_probe(host: str, port: int, timeout: float) -> tuple[bool, str | None]:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port),
                                      timeout=min(timeout, 8.0))
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


async def _validate_one(p: dict, timeout: float = 10.0) -> dict:
    """Return p plus ok / ip / latency_ms / err / checked."""
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    if p["scheme"].startswith("socks"):
        ok, err = await _tcp_probe(p["host"], p["port"], timeout)
        return {**p, "ok": ok, "ip": None,
                "latency_ms": int((loop.time() - t0) * 1000) if ok else None,
                "err": err, "checked": "tcp"}
    try:
        async with httpx.AsyncClient(proxy=_httpx_proxy_url(p),
                                     timeout=httpx.Timeout(timeout, connect=8.0)) as c:
            r = await c.get(_ECHO_URL)
            r.raise_for_status()
            try:
                ip = r.json().get("ip")
            except Exception:
                ip = (r.text or "").strip() or None
        return {**p, "ok": True, "ip": ip,
                "latency_ms": int((loop.time() - t0) * 1000), "err": None,
                "checked": "http"}
    except Exception as e:
        return {**p, "ok": False, "ip": None, "latency_ms": None,
                "err": f"{type(e).__name__}: {str(e)[:120]}", "checked": "http"}


async def _validate_all(proxies: list[dict], concurrency: int = 20) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)

    async def _guard(p):
        async with sem:
            return await _validate_one(p)

    return await asyncio.gather(*[_guard(p) for p in proxies])


# ---- persistence -----------------------------------------------------------
def _load() -> dict:
    try:
        d = json.loads(_STORE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("proxies", [])
            d.setdefault("cursor", 0)
            return d
    except Exception:
        pass
    return {"proxies": [], "cursor": 0}


def _save(data: dict) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_STORE)


def _clean(p: dict) -> dict:
    """Stored form: keep what's needed to USE the proxy (creds) + its egress IP."""
    return {k: p[k] for k in ("scheme", "host", "port", "server",
                              "username", "password", "ip") if k in p}


# ---- public API ------------------------------------------------------------
def upload(text: str) -> dict:
    """Parse + validate the pasted list, MERGE the valid ones into the pool (dead
    ones dropped), and return a summary. Never leaks passwords in the response."""
    parsed = parse_proxies(text)
    if not parsed:
        return {"received": 0, "kept": 0, "dropped": 0, "count": 0, "ips": [], "results": []}
    results = asyncio.run(_validate_all(parsed))
    valid = [r for r in results if r.get("ok")]
    with _LOCK:
        data = _load()
        pool = data.get("proxies") or []
        seen = {(p.get("server"), p.get("username")) for p in pool}
        for r in valid:
            key = (r.get("server"), r.get("username"))
            if key not in seen:
                pool.append(_clean(r))
                seen.add(key)
        data["proxies"] = pool
        _save(data)
        count = len(pool)
    return {
        "received": len(parsed),
        "kept": len(valid),
        "dropped": len(parsed) - len(valid),
        "count": count,
        "ips": [r.get("ip") or r["server"] for r in valid],
        "results": [{"server": r["server"], "ok": r["ok"], "ip": r.get("ip"),
                     "checked": r.get("checked"), "err": r.get("err")} for r in results],
    }


def summary() -> dict:
    """Pool overview for the UI — host:port + egress IP only, NO credentials."""
    with _LOCK:
        data = _load()
    ps = data.get("proxies") or []
    return {"count": len(ps), "cursor": int(data.get("cursor", 0)),
            "last_check": data.get("last_check"),
            "ips": [{"server": p.get("server"), "ip": p.get("ip")} for p in ps]}


def revalidate_batch(batch: int = 150, max_fails: int = 3,
                     concurrency: int = 20) -> dict:
    """Self-heal the pool: re-check the next `batch` proxies round-robin (its OWN
    `recheck_cursor`) and drop the dead ones. A proxy that PASSES has its fail-streak
    reset (and its egress IP refreshed); one that FAILS has the streak incremented and
    is REMOVED only after `max_fails` CONSECUTIVE failures — residential proxies flap,
    so a single timeout must never evict a good one. Stamps `last_check`. Network
    validation runs OUTSIDE the lock. Returns {checked, removed, remaining}."""
    with _LOCK:
        data = _load()
        pool = list(data.get("proxies") or [])
        rc = int(data.get("recheck_cursor", 0))
    n = len(pool)
    if n == 0:
        with _LOCK:
            data = _load()
            data["last_check"] = time.time()
            _save(data)
        return {"checked": 0, "removed": 0, "remaining": 0}
    rc %= n
    take = min(batch, n)
    subset = [pool[(rc + k) % n] for k in range(take)]
    results = asyncio.run(_validate_all(subset, concurrency=concurrency))
    res_by_key = {(r.get("server"), r.get("username")): r for r in results}
    removed = 0
    with _LOCK:
        data = _load()
        new_pool = []
        for p in (data.get("proxies") or []):
            r = res_by_key.get((p.get("server"), p.get("username")))
            if r is None:                       # not in this batch — untouched
                new_pool.append(p)
            elif r.get("ok"):
                q = {**p, "fails": 0}
                if r.get("ip"):
                    q["ip"] = r["ip"]
                new_pool.append(q)
            else:
                fails = int(p.get("fails", 0)) + 1
                if fails >= max_fails:
                    removed += 1                # evicted
                else:
                    new_pool.append({**p, "fails": fails})
        data["proxies"] = new_pool
        m = len(new_pool)
        data["recheck_cursor"] = (rc + take) % m if m else 0
        data["cursor"] = int(data.get("cursor", 0)) % m if m else 0
        data["last_check"] = time.time()
        _save(data)
    return {"checked": len(subset), "removed": removed, "remaining": m}


def clear() -> dict:
    with _LOCK:
        _save({"proxies": [], "cursor": 0})
    return {"count": 0}


def replace_pool(proxies: list[dict]) -> dict:
    """Atomically REPLACE the whole pool with a fresh set (daily refresh: delete the
    old proxies, write the new ones). Resets both cursors and the fail streaks (a
    fresh entry starts at fails=0 — `_clean` drops `fails`). Used by the Bright Data
    daily provisioner so the pool is rebuilt from fresh egress sessions each day."""
    with _LOCK:
        data = _load()
        data["proxies"] = [_clean(p) for p in proxies]
        data["cursor"] = 0
        data["recheck_cursor"] = 0
        data["last_check"] = time.time()
        _save(data)
    return {"count": len(data["proxies"])}


def next_proxy() -> dict | None:
    """Round-robin the pool (advances + persists the cursor). Returns
    {server, username, password} or None when the pool is empty.

    When the owner's laptop residential tunnel is connected, PREFER it (so a fill goes out from a
    real home IP for ATSes that block the datacenter). This is the owner's on/off switch — connect
    the laptop and single-fills route residential; disconnect and it falls back to the pool below."""
    res = residential_proxy()
    if res:
        return res
    with _LOCK:
        data = _load()
        ps = data.get("proxies") or []
        if not ps:
            return None
        # Prefer the healthiest tier: cycle only among the lowest-`fails` proxies so a
        # mostly-dead pool doesn't keep handing out known-bad egresses (a dead proxy costs
        # a page-load failure/timeout per job). Falls through to the full pool if the whole
        # pool is uniformly degraded. The revalidator resets `fails` to 0 on a live check,
        # so this tier converges to the actually-live proxies over time.
        min_fails = min(int(p.get("fails", 0)) for p in ps)
        pool = [p for p in ps if int(p.get("fails", 0)) <= min_fails] or ps
        i = int(data.get("cursor", 0)) % len(pool)
        data["cursor"] = (i + 1) % len(pool)
        _save(data)
        p = pool[i]
    return {"server": p.get("server"), "username": p.get("username"),
            "password": p.get("password")}
