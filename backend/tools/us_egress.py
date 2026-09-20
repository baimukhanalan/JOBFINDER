"""US-geolocated egress resolver for the apply lanes.

Resolves a Playwright proxy dict that egresses from a **US** IP, in priority order,
each tier GATED and (for the flaky free tier) VALIDATED. Built for the US BPO lanes
(Concentrix/CVS/Cigna/Humana/Amazon-corporate/Workday) whose reCAPTCHA-Enterprise /
AWS-WAF risk score wants a US-resident IP that MATCHES the persona — the owner's
connected phones are KAZAKHSTAN residential (a geo-mismatch, see proxy_pool notes),
so they are NEVER returned here.

`us_proxy()` priority chain (each falls through to the next on miss/error, never raises):
  1. EXPLICIT override — `US_PROXY` env. A proxy URL → that exact proxy. A value of
     `direct`/`none`/`off`/`0`/`false`/`""` (SET but empty/keyword) → force DIRECT (None).
     UNSET → fall through.
  2. FREE US source (PRIMARY) — Webshare free tier (gate on `WEBSHARE_API_KEY`) +
     a proxyscrape free public list. Each candidate is VALIDATED live (alive AND
     geolocates to the US via a short probe through the proxy) before it is returned;
     the validated pick is cached to `data/us_egress_cache.json` with a TTL so we don't
     re-probe every call. Free public proxies are flaky → validate, skip the dead, try
     a few.
  3. BRIGHT DATA (FALLBACK) — a fresh Bright Data session COUNTRY-PINNED to the US
     (`-country-us` in the username, forced regardless of `BRIGHTDATA_COUNTRY`). Gated
     on the BD env already used elsewhere (`brightdata_proxies._missing` == []).
     Datacenter zone (`alibaba_dc`, no KYC) or residential (`alibaba_res`) — whichever
     `BRIGHTDATA_ZONE` is set to. Valid by construction (a paid rotating gateway), so
     it is NOT re-probed.
  4. DIRECT (last resort) — None (the server's own datacenter IP).

Returned dict is Playwright's proxy shape: `{"server": ..., ["username", "password"]}`.

A lane OPTS IN with `lane_us_egress("<LANE>_US", "<LANE>_PROXY")`: `<LANE>_US=1` turns on
the US resolver (falls back through the chain to DIRECT); `<LANE>_PROXY=<url>` is a hard
per-lane override; nothing set → DIRECT. No existing lane's default changes.

Cache file `data/us_egress_cache.json` is gitignored (holds validated proxy creds).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from backend.tools import proxy_pool

logger = logging.getLogger(__name__)

_CACHE = Path(__file__).resolve().parents[1] / "data" / "us_egress_cache.json"

# A short probe budget — free public proxies are slow; don't hang a lane on validation.
_PROBE_TIMEOUT = httpx.Timeout(6.0, connect=5.0)
_FETCH_TIMEOUT = httpx.Timeout(10.0, connect=6.0)
_MAX_CANDIDATES = 8
_DIRECT_KEYWORDS = ("direct", "none", "off", "0", "false")


# ---- shape helpers ---------------------------------------------------------
def _to_pw(p: dict) -> dict:
    """proxy_pool.parse_proxies dict → Playwright proxy dict {server[, username, password]}."""
    out = {"server": p["server"]}
    if p.get("username"):
        out["username"] = p["username"]
        out["password"] = p.get("password") or ""
    return out


def _pw_to_httpx(cand: dict) -> str:
    """Playwright proxy dict → an httpx proxy URL (creds folded back into the URL)."""
    server = cand["server"]
    if cand.get("username"):
        scheme, _, rest = server.partition("://")
        return (f"{scheme}://{quote(cand['username'], safe='')}:"
                f"{quote(cand.get('password') or '', safe='')}@{rest}")
    return server


# ---- validation cache ------------------------------------------------------
def _cache_ttl() -> int:
    try:
        return int((os.getenv("US_EGRESS_CACHE_TTL") or "1800").strip() or "1800")
    except (TypeError, ValueError):
        return 1800


def _cache_read() -> dict | None:
    """The last validated US proxy if still within TTL, else None. Never raises."""
    try:
        d = json.loads(_CACHE.read_text(encoding="utf-8"))
        if time.time() - float(d.get("ts", 0)) < _cache_ttl():
            p = d.get("proxy")
            if isinstance(p, dict) and p.get("server"):
                return p
    except Exception:
        pass
    return None


def _cache_write(cand: dict) -> None:
    """Persist the validated pick (atomic). Never raises."""
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"proxy": cand, "ts": time.time()}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(_CACHE)
    except Exception:
        pass


# ---- free source candidates ------------------------------------------------
def _webshare_candidates() -> list[dict]:
    """Webshare free-tier US proxies (needs WEBSHARE_API_KEY). Never raises → []."""
    key = (os.getenv("WEBSHARE_API_KEY") or "").strip()
    if not key:
        return []
    try:
        url = ("https://proxy.webshare.io/api/v2/proxy/list/"
               "?mode=direct&country_code__in=US&valid=true&page_size=25")
        with httpx.Client(timeout=_FETCH_TIMEOUT) as c:
            r = c.get(url, headers={"Authorization": f"Token {key}"})
            r.raise_for_status()
            data = r.json()
        out: list[dict] = []
        for row in data.get("results", []):
            if (row.get("country_code") or "").upper() != "US":
                continue
            if row.get("valid") is False:
                continue
            addr, port = row.get("proxy_address"), row.get("port")
            if not addr or not port:
                continue
            d = {"server": f"http://{addr}:{port}"}
            if row.get("username"):
                d["username"] = row["username"]
                d["password"] = row.get("password") or ""
            out.append(d)
        return out
    except Exception as e:
        logger.debug("webshare fetch failed: %s", e)
        return []


def _proxyscrape_candidates() -> list[dict]:
    """proxyscrape free public US HTTP proxies. Geo hint only — still validated. → []."""
    try:
        url = ("https://api.proxyscrape.com/v4/free-proxy-list/get"
               "?request=display_proxies&proxy_format=protocolipport&format=text"
               "&country=us&protocol=http")
        with httpx.Client(timeout=_FETCH_TIMEOUT) as c:
            r = c.get(url)
            r.raise_for_status()
            text = r.text or ""
        out: list[dict] = []
        for line in text.splitlines():
            parsed = proxy_pool.parse_proxies(line.strip())
            if parsed:
                out.append(_to_pw(parsed[0]))
        return out
    except Exception as e:
        logger.debug("proxyscrape fetch failed: %s", e)
        return []


def _fetch_free_candidates(limit: int = _MAX_CANDIDATES) -> list[dict]:
    """Combined free candidates (Webshare first — has auth + is more reliable). → []."""
    out: list[dict] = []
    out.extend(_webshare_candidates())
    out.extend(_proxyscrape_candidates())
    return out[:limit]


# ---- geo validation --------------------------------------------------------
def _egress_country(cand: dict) -> str | None:
    """The 2-letter country code the candidate egresses from (probe THROUGH it), or None
    if dead/unreachable. Never raises."""
    try:
        with httpx.Client(proxy=_pw_to_httpx(cand), timeout=_PROBE_TIMEOUT) as c:
            r = c.get("http://ip-api.com/json")
            r.raise_for_status()
            j = r.json()
        cc = (j.get("countryCode") or j.get("country_code") or "").upper()
        return cc or None
    except Exception as e:
        logger.debug("geo probe failed for %s: %s", cand.get("server"), e)
        return None


def _geo_validate(cand: dict) -> bool:
    """True iff the candidate is alive AND geolocates to the US."""
    return _egress_country(cand) == "US"


def _free_us_proxy() -> dict | None:
    """A validated free US proxy (cache-first), or None if none validate. Never raises."""
    cached = _cache_read()
    if cached:
        return cached
    try:
        for cand in _fetch_free_candidates():
            if _geo_validate(cand):
                _cache_write(cand)
                return cand
    except Exception as e:
        logger.debug("free US source failed: %s", e)
    return None


# ---- Bright Data fallback --------------------------------------------------
def _bd_us_proxy() -> dict | None:
    """A fresh Bright Data session COUNTRY-PINNED to the US, as a Playwright dict, or None
    when BD isn't configured. Forces `country=us` regardless of BRIGHTDATA_COUNTRY so the
    egress lands in the US even if the env is set to another country. Valid by construction
    (paid rotating gateway) → not re-probed. Never raises."""
    try:
        from backend.tools import brightdata_proxies as bd
        cfg = bd._cfg()
        if bd._missing(cfg):
            return None
        cfg = {**cfg, "country": "us"}          # FORCE US pin (adds -country-us to the username)
        s = bd.make_sessions(1, cfg)[0]
        return {"server": s["server"], "username": s["username"], "password": s["password"]}
    except Exception as e:
        logger.debug("bright data US proxy failed: %s", e)
        return None


# ---- public API ------------------------------------------------------------
def us_proxy() -> dict | None:
    """Resolve a US-geolocated egress as a Playwright proxy dict, or None = DIRECT.

    Priority: explicit `US_PROXY` override → validated FREE US source → Bright Data
    (US-pinned) → DIRECT. See the module docstring. Never raises; the owner's KZ phones
    are NEVER returned (this chain does not consult `residential_slots()`)."""
    raw = os.getenv("US_PROXY")
    if raw is not None:                          # SET (possibly empty) — explicit intent
        v = raw.strip()
        if v == "" or v.lower() in _DIRECT_KEYWORDS:
            return None                          # forced DIRECT
        parsed = proxy_pool.parse_proxies(v)
        if parsed:
            return _to_pw(parsed[0])
        logger.warning("US_PROXY=%r is unparseable — falling through to the free source", v)

    free = _free_us_proxy()
    if free:
        return free
    bd = _bd_us_proxy()
    if bd:
        return bd
    return None


def lane_us_egress(us_env: str, proxy_env: str = "", name: str = "") -> dict | None:
    """Per-lane US-egress resolver. DIRECT is the DEFAULT; a lane opts in via env.

    Precedence high→low:
      - `<proxy_env>` SET → a hard per-lane override: a proxy URL → that exact proxy;
        `direct`/`none`/`off`/`0`/`false`/`""` → force DIRECT (None). Terminal (never
        falls through), matching `proxy_pool.lane_egress`.
      - `<us_env>` truthy (`1`/`true`/`yes`/`on`) → the US resolver `us_proxy()`
        (validated FREE US → Bright Data US → DIRECT; also honours the global US_PROXY).
      - nothing set → DIRECT (None).

    `name` is reserved for future per-lane pinning (unused today). Never raises."""
    if proxy_env:
        pov = os.getenv(proxy_env)
        if pov is not None:
            v = pov.strip()
            if v == "" or v.lower() in _DIRECT_KEYWORDS:
                return None
            parsed = proxy_pool.parse_proxies(v)
            return _to_pw(parsed[0]) if parsed else None
    if (os.getenv(us_env) or "").strip().lower() in ("1", "true", "yes", "on"):
        return us_proxy()
    return None
