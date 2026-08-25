# Isolated company auto-applier

## Safety boundary

This worker is independent from `/catalog/fill_all`, the shared co-pilot browser, synthetic personas,
and the existing bulk/status state. It claims only `company_remote_applications`, writes only to
`company_remote_application*` tables plus `uploads/company_remote_apply/`, and uses the old
`job_catalog` read-only solely to exclude overlapping vacancies at enqueue time.

The automatic final action is opt-in per exact batch. A named person must confirm the count-bound
phrase `SEND N`; authorization is bound to the selected profile, application IDs, current
job/question hashes, and a generated `batch_id`. The worker may then fill and send only rows from
that authorization batch. The shared pre-fill runner remains inert by default: the isolated worker
supplies a callback that runs while its own page is still open.

## State flow

```text
queued -> claimed -> awaiting_approval -> approved -> preparing
       -> blocked                         -> ready_for_review -> human_submitted
                                          -> needs_input

queued -> claimed -> awaiting_approval -> submit_approved -> submitting
       -> blocked                                           -> auto_submitted
                                                            -> needs_input
                                                            -> submission_failed
```

The PostgreSQL queue uses `FOR UPDATE SKIP LOCKED`, a separate per-profile lease, stale-lease
recovery, unique `(job, profile)` and `(apply URL, profile)` constraints, and hash revalidation before
every important transition. A changed JD, question set, profile or fact sheet invalidates approval.
Existing `job_catalog` identities are read only as an exclusion list, preventing duplicate work
between the two systems. A submit worker should always pass the `batch_id` returned by authorization;
this prevents an older authorized package from being claimed by a newer local run.

`needs_input` means no final click occurred (for example CAPTCHA, incomplete fields, review items,
or an ambiguous final control). `submission_failed` means a click may have reached the employer but
no positive receipt was recognized. It is terminal and must never be retried automatically, because
a retry could create a duplicate application. `auto_submitted` requires positive confirmation text
or a confirmation URL captured from the live page.

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

# Manual-review path, bound to the current stored hash
python -m backend.tools.company_applier approve --id APPLICATION_ID --actor alan

# Opens and fills approved forms, but never submits
python -m backend.tools.company_applier fill-approved --profile REAL_PROFILE_ID --limit 1
python -m backend.tools.company_applier list --profile REAL_PROFILE_ID --state ready_for_review

# Only after the human submits and sees external confirmation
python -m backend.tools.company_applier mark-submitted \
  --id APPLICATION_ID --actor alan --receipt '{"confirmation":"manual"}'

# Automatic path: the UI calls apply_db.authorize_batch(...) after the person
# enters exactly `SEND N`, then runs only the returned batch:
python -m backend.tools.company_applier submit-authorized \
  --profile REAL_PROFILE_ID --batch AUTHORIZATION_BATCH_ID --limit 10
```

For a separate deployment worktree, point candidate data at the authoritative live checkout without
copying it:

```bash
export COMPANY_APPLY_PROFILES_FILE=/home/projects/jobfinder/backend/data/profiles.json
export COMPANY_APPLY_FACTS_DIR=/home/projects/jobfinder/backend/data/facts
export COMPANY_APPLY_ARTIFACTS_DIR=/home/projects/jobfinder-discovery/uploads/company_remote_apply
```

Do not schedule authorization globally. It is a user action in the dedicated Mass Hiring section.
Run `submit-authorized` only for the returned profile and `batch_id`; never run an unscoped global
submit loop. Use the separate profile lease plus the shared Playwright lock so it cannot overlap
question scraping. The old `/catalog/fill_all` route, co-pilot and bulk state are not called or
mutated by this flow.

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
