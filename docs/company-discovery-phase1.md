# Independent company discovery — Phase 1

## Boundary

This pipeline discovers US companies independently of the current vacancy catalog. It does not
use job aggregators, `targets.json`, `discovered_slugs.json`, or existing job rows as acquisition
inputs. The existing `job_catalog` is read only after import to label overlaps.

No dashboard page or navigation item is part of Phase 1.

## Sources

- SEC EDGAR public filer/ticker data. `--sec-bulk` uses the nightly submissions archive and is the
  preferred high-volume mode because it can include official websites without per-company calls.
- USAspending award recipients. This source requires no API key.
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
