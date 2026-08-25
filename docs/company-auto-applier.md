# Isolated company auto-applier

## Safety boundary

This worker is independent from `job_catalog`, `/catalog/fill_all`, the shared co-pilot browser,
synthetic personas, and the existing status files. It uses only `company_remote_jobs` and writes only
to `company_remote_application*` tables plus `uploads/company_remote_apply/`.

“Auto-applier” means automatic policy checking, résumé tailoring and form pre-fill. It never clicks
Submit. A named human must approve the exact current job/question hash before a live form is opened,
then review and submit the resulting form personally. Only that person can record
`human_submitted`; there is no automatic `submitted` state.

## State flow

```text
queued -> claimed -> awaiting_approval -> approved -> preparing
       -> blocked                         -> ready_for_review -> human_submitted
                                          -> needs_input
```

The PostgreSQL queue uses `FOR UPDATE SKIP LOCKED`, a separate per-profile lease, stale-lease
recovery, unique `(job, profile)` and `(apply URL, profile)` constraints, and hash revalidation before
every important transition. A changed JD, question set, profile or fact sheet invalidates approval.
Existing `job_catalog` identities are read only as an exclusion list, preventing duplicate work
between the two systems.

## Required gates

- real, non-sample, non-synthetic profile;
- reachable phone/email and verified reply route;
- non-empty résumé and fact sheet;
- fresh active confirmed-remote job with a complete question set and HTTPS apply URL;
- deterministic US/Canada region compatibility and sponsorship check;
- honest résumé/JD fit score; scoring errors fail closed;
- explicit human review for demographics, legal attestations, consent, salary, authorization,
  identity documents, recordings and every live unfilled/review field.

## Commands

```bash
python -m backend.tools.company_applier init
python -m backend.tools.company_applier enqueue --profile REAL_PROFILE_ID --limit 500
python -m backend.tools.company_applier prepare --profile REAL_PROFILE_ID --limit 20
python -m backend.tools.company_applier list --profile REAL_PROFILE_ID --state awaiting_approval

# Explicit human decision, bound to the current stored hash
python -m backend.tools.company_applier approve --id APPLICATION_ID --actor alan

# Opens and fills approved forms, but never submits
python -m backend.tools.company_applier fill-approved --profile REAL_PROFILE_ID --limit 1
python -m backend.tools.company_applier list --profile REAL_PROFILE_ID --state ready_for_review

# Only after the human submits and sees external confirmation
python -m backend.tools.company_applier mark-submitted \
  --id APPLICATION_ID --actor alan --receipt '{"confirmation":"manual"}'
```

For a separate deployment worktree, point candidate data at the authoritative live checkout without
copying it:

```bash
export COMPANY_APPLY_PROFILES_FILE=/home/projects/jobfinder/backend/data/profiles.json
export COMPANY_APPLY_FACTS_DIR=/home/projects/jobfinder/backend/data/facts
export COMPANY_APPLY_ARTIFACTS_DIR=/home/projects/jobfinder-discovery/uploads/company_remote_apply
```

Do not schedule `approve`, `fill-approved`, or `mark-submitted` globally. Approval is a human action;
the fill worker may be scheduled only for a specific real profile after that profile is selected and
validated. Use both a dedicated application lock and the shared Playwright lock so it cannot overlap
question scraping.

## Separate live worktree

The feature branch must not replace or merge into live `main`. Once SSH access is restored, deploy
the collector/worker as a detached worktree such as `/home/projects/jobfinder-discovery`, reuse the
live `backend/.env` only through a protected symlink, and leave all PM2 services in
`/home/projects/jobfinder` unchanged.

Recommended collector schedule after a bounded smoke succeeds:

```cron
30 9 * * * mkdir -p /home/programmer/.cache/jobfinder /home/projects/jobfinder-discovery/logs && /usr/bin/flock -n /home/programmer/.cache/jobfinder/company-jobs.lock /usr/bin/flock -n /home/programmer/.cache/jobfinder/playwright.lock /usr/bin/timeout -k 5m 6h /bin/bash -lc 'cd /home/projects/jobfinder-discovery && exec /usr/bin/python3 -m backend.tools.company_jobs collect --status novel --limit-companies 100 --question-limit 200' >> /home/projects/jobfinder-discovery/logs/company-jobs.log 2>&1
```

The existing `catalog_forms` cron must acquire the same `playwright.lock` before this line is enabled.
