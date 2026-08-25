# Remote vacancy collection — Phase 2

## Boundary

Phase 2 starts from the independently discovered companies in `company_discovery`. It does not add
a page, menu item, or route, and it does not read or write the existing `job_catalog`, company target
files, or aggregator discovery. Only vacancies with a strong remote signal are admitted; hybrid,
on-site, “remote optional”, and ambiguous vacancies are rejected.

Public connectors currently cover Greenhouse, Lever, Ashby, Workable, SmartRecruiters and Workday.
Workday requires the exact public tenant/site URL saved during company enrichment; the tenant and
career site cannot be safely inferred from a company name alone.

## Data captured

For every admitted vacancy the collector keeps:

- company and stable ATS job identity;
- title, department, employment type, locations, country/state/city;
- full plain-text and HTML JD, extracted requirements and benefits;
- compensation bounds, currency, publication date, job URL and apply URL;
- the complete public application-question set, including required flags, control types and options;
- raw ATS payload and acquisition provenance.

`company_remote_jobs` is the current state. `company_remote_job_snapshots` is append-only history for
new listings, meaningful edits, closures and reopenings. `company_remote_job_questions` stores the
current authoritative question set, `company_remote_job_question_attempts` retains complete and
partial acquisition evidence, and `company_remote_job_scans` records every board scan.

Question status is deliberately strict. An ATS API response or stable rendered form may be
`success`, including a confirmed empty form. CAPTCHA, unstable or multi-step forms, unlabelled
controls, and navigation failures are recorded as `failed`; a failure never erases the last complete
set. The question reader does not enter values or submit forms.

## Commands

```bash
# Create the isolated tables (requires CRM_PG_DSN)
python -m backend.tools.company_jobs init

# Collect every confirmed remote job and all accessible questions
python -m backend.tools.company_jobs collect --status novel --limit-companies 100

# Operational smoke: ATS/API questions only, no rendered-form fallback
python -m backend.tools.company_jobs collect --status novel \
  --limit-companies 5 --skip-questions

# Inspect current counts
python -m backend.tools.company_jobs stats
```

The default rendered-question limit is unlimited. `--question-limit N` can bound browser work for a
single run; unattempted jobs remain explicitly marked and should be revisited later.

## Failure semantics

A successful complete board response is authoritative, even when it contains zero remote jobs, so
previously active jobs missing from that board are closed. A connector/network failure records a
failed scan and closes nothing. This prevents temporary provider outages from corrupting job history.

Production collectors also treat per-job detail failures as an incomplete board response. Jobs whose
full detail was read are still stored, but no absence-based closures occur until a later scan reads
every required detail successfully. SmartRecruiters and Workday pagination has bounded page and
no-progress guards, so a repeating upstream page fails safely instead of looping or producing a
false-complete result.

Targets are selected from the supported ATS set in oldest-scanned-first order. A non-blocking
PostgreSQL advisory lock permits only one worker to scan a particular company/ATS/board identity at
a time. The raw `ats_slug` remains the database identity; only a trimmed copy is used to construct an
HTTP request.

The job scan transaction is finalized before rendered application forms are opened. Scan metadata
and any missing-job closures commit atomically. Form/question failures are then recorded per job and
never downgrade a completed board scan or erase the last authoritative question set.
