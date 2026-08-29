"""Large US employer reference — a keyless mass-hiring SIGNAL for the Mass Hiring board.

Ported (self-contained) from the company-discovery experiment: a public E-Verify mirror lists
US employers with a 10,000+ workforce ranked by the number of active hiring sites — a strong
"this employer hires at volume" signal. Unlike the board's `fetch_*` sources this yields
EMPLOYERS, not job postings, so it NEVER feeds `mass_hiring_jobs`; it is a small reference /
candidate-employer list surfaced in the board UI and cached to `backend/data/everify_employers.json`.

Design constraints (deliberate, so it can't destabilise the live board):
  * pure `httpx`, no browser, no DB;
  * `fetch_large_everify_employers()` degrades to [] on any network/parse failure, never raises;
  * `classify_employer()` is a pure heuristic (segment + entity-risk flags), unit-tested no-network;
  * the UI reads ONLY the cached file (no network on page render);
  * `refresh_cache()` is stale-gated and called guarded at the end of `mass_hiring.collect()`.

CLI:
    python -m backend.tools.everify_employers --refresh [--limit N]   # rebuild the cache
    python -m backend.tools.everify_employers --show [--limit N]      # print the cached list
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import httpx

EVERIFY_MIRROR_URL = "https://h1btrack.com/e-verify/employers/"
# A real-browser UA — the mirror is fronted by a CDN that may reject an obvious bot UA.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_CACHE_PATH = Path(__file__).resolve().parents[1] / "data" / "everify_employers.json"
# The large-employer list is extremely stable (10k+ workforce firms), so a weekly refresh is
# plenty; the nightly `mass_hiring.collect()` hook only re-fetches once the cache passes this age.
_CACHE_MAX_AGE = 7 * 86400
# How many employers to hold in the cache (the UI panel shows only the top slice of this).
_DEFAULT_LIMIT = 500


# --------------------------------------------------------------------------- segmentation
def classify_employer(record: dict) -> tuple[str, list[str]]:
    """Deterministic employer lane + entity-risk flags. Pure — no network, no DB.

    Returns (segment, risk_flags). `segment` is one of staffing/government/education/
    healthcare/nonprofit/general; risk flags mark names that look like a shell/fund/affiliate
    rather than a real hiring operation.
    """
    name = str(record.get("brand_name") or record.get("trade_name")
               or record.get("legal_name") or "").strip()
    industry = str(record.get("industry") or "").strip()
    blob = f"{name} {industry}".casefold()
    if re.search(r"\b(staffing|recruit(?:ing|ment)?|personnel|talent solutions)\b", blob):
        segment = "staffing"
    elif re.search(r"\b(government|department|county|city of|state of|federal|municipal)\b", blob):
        segment = "government"
    elif re.search(r"\b(university|college|school district|higher education|education)\b", blob):
        segment = "education"
    elif re.search(r"\b(health|hospital|medical|clinic|pharma|biotech)\b", blob):
        segment = "healthcare"
    elif re.search(r"\b(nonprofit|non-profit|foundation|charit(?:y|able))\b", blob):
        segment = "nonprofit"
    else:
        segment = "general"

    risks: list[str] = []
    if re.search(r"\b(payroll|shared services|management company|management services)\b", blob):
        risks.append("shell_or_shared_services")
    if re.search(r"\b(fund|trust|investment vehicle)\b", blob):
        risks.append("fund_or_trust")
    if re.search(r"\b(subsidiary|division|operating company|operations|systems)\b", blob):
        risks.append("affiliate_or_division")
    if len(name) > 80 or len(name.split()) > 12:
        risks.append("aggregate_or_sentence_name")
    return segment, list(dict.fromkeys(risks))


# --------------------------------------------------------------------------- HTML parsing
class _EmployerTableParser(HTMLParser):
    """Parse the E-Verify mirror's employer table into raw field dicts."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self._row: dict | None = None
        self._field = ""
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        values = {str(key): str(value or "") for key, value in attrs}
        classes = set(values.get("class", "").split())
        if tag == "tr" and "evm-tr" in classes:
            self._row = {"states": []}
        if self._row is None:
            return
        field = ""
        if "evm-enm" in classes:
            field = "legal_name"
        elif "evm-dba" in classes:
            field = "dba"
        elif "evm-tdsz" in classes:
            field = "workforce"
        elif "evm-tddt" in classes:
            field = "enrolled"
        elif "evm-sites" in classes:
            field = "sites"
        elif "evm-stag" in classes:
            field = "state"
        if field:
            self._field = field
            self._text = []

    def handle_data(self, data) -> None:
        if self._field:
            self._text.append(str(data))

    def handle_endtag(self, tag) -> None:
        if self._row is None:
            return
        if self._field and tag in {"div", "td", "span"}:
            value = " ".join(" ".join(self._text).split())
            if self._field == "state":
                if re.fullmatch(r"[A-Z]{2}", value):
                    self._row["states"].append(value)
                elif value.startswith("+") and value[1:].isdigit():
                    self._row["additional_states"] = int(value[1:])
            elif value:
                self._row[self._field] = value
            self._field = ""
            self._text = []
        if tag == "tr":
            if self._row.get("legal_name"):
                self.rows.append(self._row)
            self._row = None


def parse_everify_employer_page(html: str, *, source_url: str,
                                observed_at: str) -> list[dict]:
    """Extract 10k+ workforce employers from one mirror page as flat, self-contained dicts.

    Only rows whose workforce band is exactly "10,000 and over" are kept. Each dict carries the
    employer identity, its hiring-site count (the ranking signal), the visible states, and a
    deterministic `segment` + `risk_flags` from `classify_employer`.
    """
    parser = _EmployerTableParser()
    parser.feed(html or "")
    records: list[dict] = []
    for row in parser.rows:
        # The mirror occasionally prefixes a name with a bare "*" marker — drop it for display.
        legal_name = re.sub(r"^[*\s]+", "", row.get("legal_name", "")).strip()
        dba = re.sub(r"^[*\s]+", "", re.sub(
            r"^DBA:\s*", "", row.get("dba", ""), flags=re.I)).strip()
        if row.get("workforce", "") != "10,000 and over":
            continue
        sites_text = row.get("sites", "0").replace(",", "")
        sites = int(sites_text) if sites_text.isdigit() else 0
        name = dba or legal_name
        identity = "|".join((legal_name.casefold(), dba.casefold(), row.get("enrolled", "")))
        external_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        segment, risk_flags = classify_employer(
            {"brand_name": name, "trade_name": name, "legal_name": legal_name})
        records.append({
            "source": "everify_large_employer",
            "source_external_id": external_id,
            "source_url": source_url,
            "source_observed_at": observed_at,
            "legal_name": legal_name,
            "trade_name": name,
            "brand_name": name,
            "country": "US",
            "employee_size": "10000+",
            "employee_count_min": 10000,
            "workforce_range": "10,000 and over",
            "hiring_sites": sites,
            "states": row.get("states") or [],
            "additional_state_count": row.get("additional_states", 0),
            "enrolled_at": row.get("enrolled") or None,
            "employer_status": "active",
            "segment": segment,
            "risk_flags": risk_flags,
            "source_caveat": "requires official identity and domain verification",
        })
    return records


# --------------------------------------------------------------------------- live fetch
def fetch_large_everify_employers(*, limit: int = _DEFAULT_LIMIT, min_interval: float = 0.75,
                                  client: httpx.Client | None = None) -> list[dict]:
    """Fetch active 10k+ workforce US employers ranked by participating hiring sites.

    Degrades gracefully: ANY network / HTTP / parse failure returns whatever was gathered so
    far (possibly []) — it never raises, so a caller (e.g. the collect hook) can't be broken by
    the mirror being down or changing shape.
    """
    if limit < 1:
        return []
    owned = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(30.0), headers={"User-Agent": _UA}, follow_redirects=True)
    output: list[dict] = []
    seen: set[str] = set()
    observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        for page in range(1, 66):
            response = None
            for attempt in range(6):
                try:
                    response = client.get(EVERIFY_MIRROR_URL, params={
                        "status": "Open", "size": "10,000 and over",
                        "sort": "sites_desc", "page": page,
                    })
                except httpx.HTTPError:
                    if attempt >= 5:
                        return output
                    time.sleep(min(2 ** attempt, 12))
                    continue
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
                if attempt < 5:
                    retry_after = response.headers.get("retry-after", "")
                    delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() \
                        else min(2 ** attempt, 12)
                    time.sleep(delay)
            if response is None or response.status_code >= 400:
                return output
            records = parse_everify_employer_page(
                response.text, source_url=str(response.url), observed_at=observed)
            if not records:
                break
            for record in records:
                key = re.sub(r"[^a-z0-9]+", " ", record["trade_name"].casefold()).strip()
                if key and key not in seen:
                    seen.add(key)
                    output.append(record)
                    if len(output) >= limit:
                        return output
            if page < 65 and min_interval:
                time.sleep(max(0.0, min_interval))
        return output
    except Exception:
        return output
    finally:
        if owned:
            client.close()


def large_everify_employers(*, limit: int = _DEFAULT_LIMIT,
                            client: httpx.Client | None = None) -> list[dict]:
    """Ranked large-employer reference list (name + segment + hiring-site signal).

    Thin public wrapper over `fetch_large_everify_employers` returning the compact shape the UI
    and future connectors consume. Never raises (returns [] on failure).
    """
    rows = fetch_large_everify_employers(limit=limit, client=client)
    out = []
    for r in rows:
        out.append({
            "name": r["trade_name"],
            "legal_name": r["legal_name"],
            "segment": r["segment"],
            "risk_flags": r["risk_flags"],
            "hiring_sites": r["hiring_sites"],
            "states": r["states"],
            "additional_state_count": r.get("additional_state_count", 0),
        })
    out.sort(key=lambda x: x["hiring_sites"], reverse=True)
    return out


# --------------------------------------------------------------------------- cache
def load_cached(*, path: Path | str = _CACHE_PATH) -> dict:
    """Read the cached employer list. Returns {} when absent/unreadable (never raises)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("employers"), list):
        return {}
    return data


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def refresh_cache(*, limit: int = _DEFAULT_LIMIT, path: Path | str = _CACHE_PATH,
                  client: httpx.Client | None = None) -> dict:
    """Fetch + write the cache. On an empty fetch the existing cache is LEFT INTACT (a transient
    mirror outage must not wipe a good list). Returns the (new or kept) cache dict; never raises.
    """
    try:
        employers = large_everify_employers(limit=limit, client=client)
    except Exception:
        employers = []
    if not employers:
        return load_cached(path=path)
    payload = {
        "fetched_at": int(time.time()),
        "source": "everify_large_employer",
        "count": len(employers),
        "employers": employers,
    }
    try:
        _atomic_write(Path(path), payload)
    except OSError:
        pass
    return payload


def maybe_refresh_cache(*, limit: int = _DEFAULT_LIMIT, max_age: int = _CACHE_MAX_AGE,
                        path: Path | str = _CACHE_PATH) -> dict:
    """Refresh the cache only when it is missing or older than `max_age`. Guarded/never raises —
    safe to call from the tail of `mass_hiring.collect()`."""
    try:
        cached = load_cached(path=path)
        fetched_at = int(cached.get("fetched_at") or 0)
        if cached.get("employers") and (time.time() - fetched_at) < max_age:
            return {"refreshed": False, "count": len(cached["employers"])}
        payload = refresh_cache(limit=limit, path=path)
        return {"refreshed": bool(payload.get("employers")),
                "count": len(payload.get("employers") or [])}
    except Exception:
        return {"refreshed": False, "count": 0}


if __name__ == "__main__":
    _limit = _DEFAULT_LIMIT
    if "--limit" in sys.argv:
        try:
            _limit = int(sys.argv[sys.argv.index("--limit") + 1])
        except (ValueError, IndexError):
            pass
    if "--refresh" in sys.argv:
        t = time.time()
        payload = refresh_cache(limit=_limit)
        print(f"refreshed {payload.get('count', 0)} employers "
              f"-> {_CACHE_PATH}  ({time.time() - t:.1f}s)")
    elif "--show" in sys.argv:
        data = load_cached()
        emps = (data.get("employers") or [])[:_limit]
        print(f"cache: {len(data.get('employers') or [])} employers, "
              f"fetched_at={data.get('fetched_at')}")
        for e in emps[:40]:
            st = ",".join(e["states"][:5]) + (f"+{e['additional_state_count']}"
                                              if e.get("additional_state_count") else "")
            print(f"  {e['hiring_sites']:>7,}  [{e['segment']:>10}]  {e['name']}  ({st})")
    else:
        print(__doc__)
