"""Bounded second-factor verification for FDIC-reported bank websites."""
from __future__ import annotations

import argparse
import html
import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Mapping

import httpx

from backend.tools import company_discovery_db as company_db
from backend.tools.company_enrichment import (
    _get, canonical_domain, enrich_company, official_url,
)
from backend.tools.employer_official_crosswalk import exact_legal_key
from backend.tools.employer_official_enrichment import fetch_fdic_enrichment


class _IdentityContexts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[tuple[str, str]] = []
        self._stack: list[tuple[str, str, list[str]]] = []
        self._schema = False
        self._schema_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = dict(attrs)
        marker = " ".join((attrs_dict.get("id") or "", attrs_dict.get("class") or ""))
        kind = ""
        if tag.lower() == "title":
            kind = "title"
        elif tag.lower() == "footer" or re.search(r"\b(?:footer|legal)\b", marker, re.I):
            kind = "footer_or_legal"
        if kind:
            self._stack.append((kind, tag.lower(), []))
        if tag.lower() == "script" and "ld+json" in (attrs_dict.get("type") or "").lower():
            self._schema = True
            self._schema_text = []

    def handle_data(self, data: str) -> None:
        for _kind, _tag, values in self._stack:
            values.append(data)
        if self._schema:
            self._schema_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._schema:
            raw = "".join(self._schema_text).strip()
            try:
                payload = json.loads(html.unescape(raw))
            except (ValueError, TypeError):
                payload = None
            stack = [payload]
            while stack:
                value = stack.pop()
                if isinstance(value, Mapping):
                    if value.get("name"):
                        self.contexts.append(("schema_name", str(value["name"])))
                    stack.extend(value.values())
                elif isinstance(value, list):
                    stack.extend(value)
            self._schema = False
        for index in range(len(self._stack) - 1, -1, -1):
            kind, opening_tag, values = self._stack[index]
            if opening_tag == tag.lower():
                self.contexts.append((kind, " ".join(values)))
                del self._stack[index]
                break


def identity_contexts(page_html: str) -> list[tuple[str, str]]:
    parser = _IdentityContexts()
    try:
        parser.feed(page_html or "")
    except Exception:
        return []
    output = []
    for kind, value in parser.contexts:
        clean = " ".join(html.unescape(value).split())
        if clean:
            output.append((kind, clean))
    return output


def verify_identity_page(*, proposed_domain: str, final_url: str, page_html: str,
                         legal_name: str, brand_name: str = "",
                         trade_name: str = "") -> dict:
    domain = canonical_domain(proposed_domain)
    if not domain or canonical_domain(final_url) != domain:
        return {"passed": False, "reason": "conflicting_redirect",
                "proposed_domain": domain, "final_url": final_url}
    names = []
    for name in (legal_name, brand_name, trade_name):
        key = exact_legal_key(name)
        if key and (key, str(name)) not in names:
            names.append((key, str(name)))
    for context_type, context in identity_contexts(page_html):
        context_key = exact_legal_key(context)
        for name_key, name in sorted(names, key=lambda item: len(item[0]), reverse=True):
            if re.search(rf"(?:^| ){re.escape(name_key)}(?: |$)", context_key):
                return {"passed": True, "reason": "exact_identity_in_trusted_context",
                        "proposed_domain": domain, "final_url": final_url,
                        "matched_name": name, "context_type": context_type,
                        "context_excerpt": context[:500]}
    return {"passed": False, "reason": "exact_identity_not_in_title_schema_legal_footer",
            "proposed_domain": domain, "final_url": final_url}


def apply_passes(passes: list[Mapping[str, Any]]) -> dict:
    if not passes:
        return {"selected": 0, "updated": 0}
    by_id = {int(item["company_id"]): item for item in passes}
    if len(by_id) != len(passes):
        raise ValueError("duplicate company_id in FDIC domain verification")
    ids = sorted(by_id)
    with company_db._cur(False) as cur:
        cur.execute("""
          SELECT c.id,c.external_ids->>'fdic_cert',m.domain_verified
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE m.in_target_population AND c.id=ANY(%s) FOR UPDATE
        """, (ids,))
        locked = {int(row[0]): {"cert": str(row[1] or ""), "verified": bool(row[2])}
                  for row in cur.fetchall()}
        if set(locked) != set(ids):
            raise RuntimeError("could not lock every FDIC verification pass")
        for company_id in ids:
            item = by_id[company_id]
            cert = str(item["fdic_cert"])
            proposal = item["domain_proposal"]
            identity = item["identity"]
            if locked[company_id]["cert"] != cert or proposal["entity_id"] != f"fdic_cert:{cert}":
                raise RuntimeError("FDIC certificate changed before verification apply")
            if not identity.get("passed") or identity["proposed_domain"] != proposal["domain"]:
                raise RuntimeError("only passed exact-domain identities may be applied")
            observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            first_factor = {
                "class": "structured_corporate_source", "provider": "fdic_bankfind",
                "entity_id": proposal["entity_id"], "candidate_domain": proposal["domain"],
                "assertion": "institution_reported_primary_website",
                "source_url": proposal["provenance"]["source_url"],
                "observed_at": proposal["provenance"]["observed_at"],
            }
            second_factor = {
                "class": "official_site_identity", "provider": "official_site_identity",
                "entity_id": proposal["entity_id"], "domain": proposal["domain"],
                "assertion": "exact_bank_identity_in_trusted_page_context",
                "homepage_url": identity["final_url"],
                "matched_name": identity["matched_name"],
                "context_type": identity["context_type"],
                "context_excerpt": identity["context_excerpt"],
                "observed_at": observed_at,
            }
            encoded = json.dumps([first_factor, second_factor])
            qualification = json.dumps({"fdic_domain_verification": {
                "status": "passed", "first_factor": first_factor,
                "second_factor": second_factor}})
            cur.execute("""
              UPDATE company_employer_master SET candidate_domain=%s,domain_verified=TRUE,
                identity_confidence=GREATEST(identity_confidence,0.99),
                domain_evidence=COALESCE((SELECT jsonb_agg(e)
                  FROM jsonb_array_elements(domain_evidence) e
                  WHERE NOT (e->>'provider' IN ('fdic_bankfind','official_site_identity'))
                ),'[]'::jsonb) || %s::jsonb,
                qualification_evidence=qualification_evidence || %s::jsonb,
                last_verified_at=now(),updated_at=now() WHERE company_id=%s
            """, (proposal["domain"], encoded, qualification, company_id))
            if cur.rowcount != 1:
                raise RuntimeError("FDIC domain master update failed")
            enrichment = item.get("careers_enrichment") or {}
            provenance = json.dumps({"fdic_domain_verification": {
                "first_factor": first_factor, "second_factor": second_factor}})
            cur.execute("""
              UPDATE company_discovery SET domain=%s,domain_confidence=0.99,
                careers_url=COALESCE(NULLIF(%s,''),careers_url),
                ats=COALESCE(NULLIF(%s,''),ats),
                ats_slug=COALESCE(NULLIF(%s,''),ats_slug),
                ats_url=COALESCE(NULLIF(%s,''),ats_url),
                careers_confidence=GREATEST(COALESCE(careers_confidence,0),
                                            COALESCE(%s,0)),
                provenance=provenance || %s::jsonb,updated_at=now() WHERE id=%s
            """, (proposal["domain"], enrichment.get("careers_url"),
                  enrichment.get("ats"), enrichment.get("ats_slug"),
                  enrichment.get("ats_url"), enrichment.get("careers_confidence"),
                  provenance, company_id))
    return {"selected": len(passes), "updated": len(passes)}


def verify_linked_fdic_domains(*, limit: int = 20, min_interval: float = 0.15,
                               fdic_fetcher: Callable[..., dict] = fetch_fdic_enrichment,
                               client: httpx.Client | None = None) -> dict:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT c.id,c.legal_name,c.trade_name,m.brand_name,
                 c.external_ids->>'fdic_cert' AS cert
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE m.in_target_population AND NULLIF(c.external_ids->>'fdic_cert','') IS NOT NULL
          ORDER BY c.id LIMIT %s
        """, (max(1, min(int(limit), 100)),))
        rows = [dict(row) for row in cur.fetchall()]
    owned = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(15.0), headers={
            "User-Agent": "JobFinder-FDIC-identity-verification/1.0"})
    passes = []
    failures = []
    try:
        for index, row in enumerate(rows):
            try:
                fdic = fdic_fetcher(row["cert"])
                proposal = fdic.get("proposed_domain_evidence")
                if not proposal:
                    failures.append({"company_id": row["id"], "fdic_cert": row["cert"],
                                     "reason": "fdic_webaddr_missing", "domain_proposal": None})
                    continue
                response = _get(client, official_url(proposal["domain"]), retries=1)
                if response is None:
                    failures.append({"company_id": row["id"], "fdic_cert": row["cert"],
                                     "reason": "official_site_unavailable",
                                     "domain_proposal": proposal})
                    continue
                identity = verify_identity_page(
                    proposed_domain=proposal["domain"], final_url=str(response.url),
                    page_html=response.text, legal_name=row.get("legal_name") or "",
                    brand_name=row.get("brand_name") or "",
                    trade_name=row.get("trade_name") or "")
                if not identity["passed"]:
                    failures.append({"company_id": row["id"], "fdic_cert": row["cert"],
                                     "reason": identity["reason"],
                                     "domain_proposal": proposal, "identity": identity})
                    continue
                careers = enrich_company({"id": row["id"], "domain": proposal["domain"]},
                                         client=client)
                passes.append({"company_id": row["id"], "fdic_cert": row["cert"],
                               "domain_proposal": proposal, "identity": identity,
                               "careers_enrichment": careers})
            except Exception as exc:
                failures.append({"company_id": row["id"], "fdic_cert": row["cert"],
                                 "reason": "bounded_verification_error", "error": str(exc)})
            finally:
                if min_interval > 0 and index + 1 < len(rows):
                    time.sleep(min_interval)
    finally:
        if owned:
            client.close()
    applied = apply_passes(passes)
    return {"selected": len(rows), "passed": len(passes), "failed": len(failures),
            "apply_selected": applied["selected"], "updated": applied["updated"],
            "careers_found": sum(bool((item.get("careers_enrichment") or {}).get(
                "careers_url")) for item in passes),
            "ats_found": sum(bool((item.get("careers_enrichment") or {}).get("ats"))
                             for item in passes),
            "passes": passes, "failures": failures}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--min-interval", type=float, default=0.15)
    args = parser.parse_args(argv)
    print(json.dumps(verify_linked_fdic_domains(
        limit=args.limit, min_interval=args.min_interval), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
