# Independent company discovery — Phase 1

## Boundary

This pipeline discovers US companies independently of the current vacancy catalog. It does not
use job aggregators, `targets.json`, `discovered_slugs.json`, or existing job rows as acquisition
inputs. The existing `job_catalog` is read only after import to label overlaps.

The `/mass-hiring` dashboard section displays Company Master and enrichment progress, while
the acquisition tables and workers remain isolated from the legacy vacancy pipeline.

## Sources

- SEC EDGAR public filer/ticker data. `--sec-bulk` uses the nightly submissions archive and is the
  preferred high-volume mode because it can include official websites without per-company calls.
- USAspending award recipients. This source requires no API key.
- GLEIF public LEI Registry. The API applies explicit US, active and general-entity filters and
  provides a durable LEI, legal jurisdiction, registry identifiers, and source timestamps.
- SAM.gov active entities. Set `SAM_API_KEY` in the protected runtime environment; never pass it in
  a command or commit it.

All source records retain their source name, external ID, collection timestamp, and metadata.

## Commands

```bash
# Network-only smoke; no database write
python -m backend.tools.company_discovery collect \
  --source usaspending --limit 5 --dry-run --output /tmp/companies.jsonl

# Create the isolated table in jobfinder_crm
python -m backend.tools.company_discovery init

# 10,000 active, general US legal entities from the official GLEIF LEI Registry
python -m backend.tools.company_discovery collect \
  --source gleif --limit 10000 --max-pages 50

# Large SEC seed (when SEC allows the deployment IP)
python -m backend.tools.company_discovery collect \
  --source sec --sec-bulk --limit 10000 --enrich-web

# Offline fallback for an already downloaded SEC archive
python -m backend.tools.company_discovery collect \
  --source sec --sec-archive /protected/path/submissions.zip --limit 10000 --enrich-web

# Optional SAM.gov source
python -m backend.tools.company_discovery collect --source sam --limit 1000

# Export only companies not found in the vacancy catalog
python -m backend.tools.company_discovery export \
  --status novel --format jsonl --output /protected/path/novel-companies.jsonl
```

`CRM_PG_DSN` must be available through `backend/.env` or the process environment for commands that
read or write PostgreSQL. `SAM_API_KEY` is required only for SAM.gov.

## Matching rules

Strong exact matches use external identifiers, official domains, or `(ATS, ATS slug)`. Exact or
fuzzy name-only matches stay `possible_duplicate` for review; they are never silently treated as
known. Existing vacancy rows are never changed. A detected company is not added to vacancy
collection until a later explicit promotion workflow exists.

## Safety

Web enrichment rejects localhost, link-local, private, and non-HTTP(S) targets, validates redirects,
limits HTML responses to 2 MB, caps parallel workers at 12, and probes no more than three careers
candidates per company.

Rows without a domain have a separate conservative resolver. It checks an exact
normalized company/entity name against Wikidata's structured official-website claim
(`P856`), then optionally uses a public search result only when the candidate homepage
independently shows a strong company-name match. Social, directory and ATS hosts are
not accepted as official domains. Every accepted result keeps confidence and evidence
under `provenance.domain_resolution`:

```bash
python -m backend.tools.company_discovery resolve-domains \
  --limit 10000 --workers 2 --min-interval 1.0
```

For a large exact-only pass, the checkpointed MediaWiki API mode is preferred over
WDQS/SPARQL. It resolves enwiki titles to Wikidata entities in bounded batches,
checks normalized labels/aliases and P856 locally, and persists every completed
100-row chunk immediately:

```bash
python -m backend.tools.company_discovery resolve-domains \
  --source-name gleif_lei --limit 10000 --workers 4 --min-interval 0.25 \
  --no-search-fallback --wikidata-api-bulk --bulk-size 25
```

Provider, HTTP, DNS, and candidate-verification failures remain retryable and are
reported as `transient_errors`; they are never stored as final no-match evidence.

The resolver is bounded to at most four workers and a global request-start interval
of at least 250 ms. `--no-search-fallback` restricts it to exact-name Wikidata P856
evidence; `--dry-run` performs verification without database writes. Completed
negative attempts are stored under `provenance.domain_resolution` and skipped by
default, making large batches resumable; use `--retry-attempted` to explicitly retry
them. It reads only
`company_discovery`, never `job_catalog`, targets, discovered slugs, or vacancy rows.

Existing rows that already have a verified official domain can be enriched in a
bounded, resumable batch without re-running acquisition or domain resolution:

```bash
python -m backend.tools.company_enrichment --limit 10000 --workers 6
```

The worker records its homepage/careers/ATS evidence under
`provenance.web_enrichment`, retries temporary HTTP failures, validates every redirect,
and skips attempted rows unless `--retry-attempted` is explicitly supplied.
