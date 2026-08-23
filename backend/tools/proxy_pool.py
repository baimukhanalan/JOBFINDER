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
import threading
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_STORE = Path(__file__).resolve().parents[1] / "data" / "proxies.json"
_LOCK = threading.Lock()
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
            "ips": [{"server": p.get("server"), "ip": p.get("ip")} for p in ps]}


def clear() -> dict:
    with _LOCK:
        _save({"proxies": [], "cursor": 0})
    return {"count": 0}


def next_proxy() -> dict | None:
    """Round-robin the pool (advances + persists the cursor). Returns
    {server, username, password} or None when the pool is empty."""
    with _LOCK:
        data = _load()
        ps = data.get("proxies") or []
        if not ps:
            return None
        i = int(data.get("cursor", 0)) % len(ps)
        data["cursor"] = (i + 1) % len(ps)
        _save(data)
        p = ps[i]
    return {"server": p.get("server"), "username": p.get("username"),
            "password": p.get("password")}
