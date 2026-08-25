"""Bright Data proxy provisioning for the apply engine.

Bright Data's rotating proxies use ONE gateway (`brd.superproxy.io:33335`) plus a
zone; the egress IP is chosen per *session*, where the session id is a token you
put in the proxy username. So "generate N rotating proxies" = mint N usernames
that share the zone's gateway/password but each carry a distinct
`-session-<random>` suffix. Every session pins a fresh residential/datacenter IP,
and all of them are valid as long as the zone stays funded — there is NO per-IP
allocation step for rotating zones.

This tool mints those session-proxies and (re)populates the apply pool
(`backend/data/proxies.json`) via `proxy_pool.replace_pool`, so all the existing
rotation / co-pilot / bulk machinery keeps working unchanged. Run it DAILY (cron)
to refresh the pool with new egress sessions and drop yesterday's.

Username layout (Bright Data):
    brd-customer-<customer>-zone-<zone>[-country-<cc>]-session-<random>

Config (backend/.env, all gitignored — creds):
    BRIGHTDATA_API_TOKEN, BRIGHTDATA_CUSTOMER, BRIGHTDATA_ZONE,
    BRIGHTDATA_ZONE_PASSWORD, BRIGHTDATA_GATEWAY, BRIGHTDATA_COUNTRY,
    BRIGHTDATA_POOL_SIZE

CLI:
    python -m backend.tools.brightdata_proxies --verify           # one live probe
    python -m backend.tools.brightdata_proxies --refresh [--count N] [--validate K]
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys

from backend.config import settings
from backend.tools import proxy_pool

_GATEWAY_DEFAULT = "brd.superproxy.io:33335"


def _cfg() -> dict:
    gw = (settings.brightdata_gateway or _GATEWAY_DEFAULT).strip()
    host, _, port = gw.partition(":")
    try:
        port = int(port or 33335)
    except ValueError:
        port = 33335
    return {
        "token": settings.brightdata_api_token,
        "customer": settings.brightdata_customer.strip(),
        "zone": settings.brightdata_zone.strip(),
        "password": settings.brightdata_zone_password,
        "host": host.strip(),
        "port": port,
        "country": (settings.brightdata_country or "").strip().lower(),
    }


def _missing(cfg: dict) -> list[str]:
    return [k for k in ("customer", "zone", "password") if not cfg.get(k)]


def _username(cfg: dict, session: str) -> str:
    parts = [f"brd-customer-{cfg['customer']}", f"zone-{cfg['zone']}"]
    if cfg["country"]:
        parts.append(f"country-{cfg['country']}")
    parts.append(f"session-{session}")
    return "-".join(parts)


def make_sessions(n: int, cfg: dict | None = None) -> list[dict]:
    """Mint `n` fresh session-proxies in proxy_pool's stored dict shape."""
    cfg = cfg or _cfg()
    server = f"http://{cfg['host']}:{cfg['port']}"
    out = []
    for _ in range(max(0, n)):
        sid = secrets.token_hex(8)
        out.append({
            "scheme": "http",
            "host": cfg["host"],
            "port": cfg["port"],
            "server": server,
            "username": _username(cfg, sid),
            "password": cfg["password"],
            "ip": None,
        })
    return out


def verify_zone(cfg: dict | None = None) -> tuple[bool, str | None]:
    """One live request through a fresh session — confirms the zone is funded and
    the gateway routes. Returns (ok, egress_ip | error_string)."""
    import httpx
    cfg = cfg or _cfg()
    p = make_sessions(1, cfg)[0]
    url = f"http://{p['username']}:{p['password']}@{p['host']}:{p['port']}"
    try:
        with httpx.Client(proxy=url, timeout=httpx.Timeout(25.0, connect=10.0)) as c:
            r = c.get("http://api.ipify.org?format=json")
            r.raise_for_status()
            try:
                return True, r.json().get("ip")
            except Exception:
                return True, (r.text or "").strip() or None
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:180]}"


def refresh(count: int | None = None, validate: int = 15) -> dict:
    """Verify the zone is live, then REPLACE the whole pool with fresh sessions.
    A best-effort sample of `validate` sessions is checked (records real egress IPs
    + proves rotation) — the rest are valid by construction. Never wipes the pool if
    the zone probe fails (so a drained balance / broken zone leaves yesterday's pool
    intact rather than emptying it)."""
    cfg = _cfg()
    miss = _missing(cfg)
    if miss:
        raise SystemExit(f"Bright Data config missing: {miss} — set them in backend/.env")

    ok, info = verify_zone(cfg)
    if not ok:
        raise SystemExit(
            f"Bright Data zone '{cfg['zone']}' probe FAILED ({info}) — pool left unchanged. "
            "Check balance / zone name / password.")

    n = count or settings.brightdata_pool_size or 200
    sessions = make_sessions(n, cfg)

    sample_ips: list[str] = []
    if validate > 0:
        import asyncio
        subset = sessions[:min(validate, len(sessions))]
        results = asyncio.run(proxy_pool._validate_all(subset, concurrency=10))
        by_user = {r.get("username"): r for r in results}
        for s in sessions:
            r = by_user.get(s["username"])
            if r and r.get("ok") and r.get("ip"):
                s["ip"] = r["ip"]
                sample_ips.append(r["ip"])

    res = proxy_pool.replace_pool(sessions)
    return {
        "zone": cfg["zone"],
        "country": cfg["country"] or "any",
        "gateway": f"{cfg['host']}:{cfg['port']}",
        "generated": len(sessions),
        "pool_count": res["count"],
        "probe_ip": info,
        "sample_validated": len(sample_ips),
        "sample_ips": sorted(set(sample_ips)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bright Data proxy pool provisioner")
    ap.add_argument("--verify", action="store_true",
                    help="one live probe through the zone; print egress IP")
    ap.add_argument("--refresh", action="store_true",
                    help="replace the pool with fresh session-proxies")
    ap.add_argument("--count", type=int, default=None,
                    help="how many sessions to mint (default BRIGHTDATA_POOL_SIZE)")
    ap.add_argument("--validate", type=int, default=15,
                    help="validate this many sample sessions (0 = skip)")
    args = ap.parse_args(argv)

    if args.verify:
        ok, info = verify_zone()
        print(json.dumps({"ok": ok, "egress_ip" if ok else "error": info}))
        return 0 if ok else 1
    if args.refresh:
        print(json.dumps(refresh(count=args.count, validate=args.validate), ensure_ascii=False))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
