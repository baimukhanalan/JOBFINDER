"""Official DOL OFLC LCA disclosure connector for employer hiring signals.

The disclosure proves certified worker-position activity.  It does not prove a
domain, current vacancy, legal-entity resolution, or total employee count.
"""
from __future__ import annotations

import hashlib
import os
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

import httpx

from backend.tools.company_sources import company_record


DOL_LCA_FY2025_Q4_URL = (
    "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/"
    "LCA_Disclosure_Data_FY2025_Q4.xlsx"
)
DEFAULT_CACHE_PATH = (
    Path(__file__).resolve().parents[2] / ".cache" / "jobfinder" / "dol_oflc"
    / "LCA_Disclosure_Data_FY2025_Q4.xlsx"
)
USER_AGENT = "JobFinder-DOL-OFLC/1.0 (+https://github.com/baimukhanalan/JOBFINDER)"
MAX_XLSX_BYTES = 300 * 1024 * 1024
CERTIFIED_STATUSES = frozenset({"CERTIFIED", "CERTIFIED-WITHDRAWN"})
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", reference or "")
    value = 0
    for char in (letters.group(0).upper() if letters else ""):
        value = value * 26 + ord(char) - 64
    return value - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    output: list[str] = []
    with archive.open("xl/sharedStrings.xml") as handle:
        for event, element in ET.iterparse(handle, events=("end",)):
            if element.tag == _NS + "si":
                output.append("".join(node.text or "" for node in element.iter(_NS + "t")))
                element.clear()
    return output


def _worksheet_name(archive: zipfile.ZipFile) -> str:
    sheets = sorted(name for name in archive.namelist()
                    if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
    if not sheets:
        raise ValueError("DOL XLSX has no worksheet")
    return sheets[0]


def iter_xlsx_rows(path: str | Path) -> Iterator[dict[str, str]]:
    """Stream the first XLSX worksheet as header-keyed rows using stdlib only."""
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        headers: list[str] = []
        with archive.open(_worksheet_name(archive)) as handle:
            for event, row in ET.iterparse(handle, events=("end",)):
                if row.tag != _NS + "row":
                    continue
                values: dict[int, str] = {}
                for cell in row.findall(_NS + "c"):
                    index = _column_index(cell.get("r", ""))
                    kind = cell.get("t", "")
                    if kind == "inlineStr":
                        value = "".join(node.text or "" for node in cell.iter(_NS + "t"))
                    else:
                        node = cell.find(_NS + "v")
                        raw = node.text if node is not None and node.text is not None else ""
                        if kind == "s" and raw.isdigit() and int(raw) < len(shared):
                            value = shared[int(raw)]
                        else:
                            value = raw
                    values[index] = value.strip()
                if not headers:
                    width = max(values, default=-1) + 1
                    headers = [values.get(index, "").strip().upper() for index in range(width)]
                elif values:
                    yield {headers[index]: value for index, value in values.items()
                           if index < len(headers) and headers[index]}
                row.clear()


def _value(row: dict[str, str], *names: str) -> str:
    return next((str(row.get(name) or "").strip() for name in names
                 if str(row.get(name) or "").strip()), "")


def _workers(value: str) -> int:
    try:
        return max(0, int(float(str(value or "0").replace(",", ""))))
    except ValueError:
        return 0


def _name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_dol_lca_xlsx(path: str | Path, *, limit: int = 15_000,
                       source_url: str = DOL_LCA_FY2025_Q4_URL,
                       fiscal_year: int = 2025, quarter: int = 4) -> list[dict]:
    """Aggregate certified LCA cases into safely deduplicated employer candidates."""
    source_path = Path(path)
    digest = file_sha256(source_path)
    observed_at = datetime.fromtimestamp(
        source_path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    aggregates: dict[str, dict[str, Any]] = {}
    for row in iter_xlsx_rows(source_path):
        status = _value(row, "CASE_STATUS", "STATUS").upper()
        if status not in CERTIFIED_STATUSES:
            continue
        name = _value(row, "EMPLOYER_NAME")
        key = _name_key(name)
        if not key:
            continue
        workers = _workers(_value(row, "TOTAL_WORKER_POSITIONS", "WORKER_POSITIONS"))
        address = (
            _value(row, "EMPLOYER_ADDRESS1", "EMPLOYER_ADDRESS"),
            _value(row, "EMPLOYER_CITY"),
            _value(row, "EMPLOYER_STATE"),
            _value(row, "EMPLOYER_POSTAL_CODE", "EMPLOYER_POSTAL_CODE1"),
        )
        item = aggregates.setdefault(key, {
            "name": name, "case_count": 0, "worker_positions": 0,
            "statuses": defaultdict(int), "addresses": defaultdict(lambda: [0, 0]),
        })
        item["case_count"] += 1
        item["worker_positions"] += workers
        item["statuses"][status] += 1
        item["addresses"][address][0] += 1
        item["addresses"][address][1] += workers
        if len(name) > len(item["name"]):
            item["name"] = name
    ranked = sorted(aggregates.items(), key=lambda pair: (
        -pair[1]["worker_positions"], -pair[1]["case_count"], pair[0]))
    records = []
    for key, item in ranked[:max(0, int(limit))]:
        address = max(item["addresses"], key=lambda value: (
            item["addresses"][value][1], item["addresses"][value][0], value))
        address_line, city, state, postal_code = address
        external_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        records.append(company_record(
            source="dol_oflc_lca", source_external_id=external_id,
            source_url=source_url, source_observed_at=observed_at,
            legal_name=item["name"], trade_name=item["name"], country="US",
            states=[state] if state else [], metadata={
                "brand_name": item["name"],
                "identity_name_type": "dol_disclosed_employer_name",
                "legal_identity_unverified": True,
                "hiring_signal_present": True,
                "hiring_signal_status": "certified_lca_disclosure",
                "hiring_references": [{
                    "url": source_url,
                    "signal_type": "official_dol_oflc_certified_lca_disclosure",
                }],
                "certified_case_count": item["case_count"],
                "certified_worker_positions": item["worker_positions"],
                "case_status_counts": dict(item["statuses"]),
                "employer_address": {
                    "address_type": "dol_disclosed_employer_address",
                    "address_line1": address_line or None, "city": city or None,
                    "region": state or None, "postal_code": postal_code or None,
                    "country": "US",
                },
                "fiscal_year": fiscal_year, "quarter": quarter,
                "source_file_sha256": digest,
                "source_file_name": source_path.name,
                "source_caveat": (
                    "certified LCA activity is a hiring signal, not domain, current-job, "
                    "employee-count, or resolved legal-identity verification"),
            },
        ))
    return records


def download_dol_lca(*, cache_path: str | Path = DEFAULT_CACHE_PATH,
                     force: bool = False, client: httpx.Client | None = None,
                     max_bytes: int = MAX_XLSX_BYTES) -> Path:
    target = Path(cache_path)
    if target.is_file() and not force:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    owned = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(120.0, connect=15.0), headers={"User-Agent": USER_AGENT})
    total = 0
    try:
        with client.stream("GET", DOL_LCA_FY2025_Q4_URL) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("DOL LCA XLSX exceeds download size limit")
                    handle.write(chunk)
        with temporary.open("rb") as handle:
            signature = handle.read(4)
        if total < 4 or signature != b"PK\x03\x04":
            raise ValueError("DOL LCA response is not an XLSX archive")
        with zipfile.ZipFile(temporary) as archive:
            _worksheet_name(archive)
        os.replace(temporary, target)
        return target
    finally:
        if temporary.exists():
            temporary.unlink()
        if owned:
            client.close()


def fetch_dol_lca_employers(*, limit: int = 15_000,
                            cache_path: str | Path = DEFAULT_CACHE_PATH,
                            force_download: bool = False,
                            client: httpx.Client | None = None) -> list[dict]:
    path = download_dol_lca(
        cache_path=cache_path, force=force_download, client=client)
    return parse_dol_lca_xlsx(path, limit=limit)
