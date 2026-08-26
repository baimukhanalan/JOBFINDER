# Mass Hiring 10,000 — strict acceptance contract

This supersedes the 2,000-candidate milestone. A database row is not an accepted
employer until every identity and hiring gate below passes. Candidate, quarantined,
verified, qualified, monitoring, and application-queue counts must always be reported
separately.

## Population and isolation

- Build a reservoir of at least 15,000 independently sourced US employer candidates,
  then select exactly 10,000 relevant employers for the master.
- Always include Amazon, Concentrix, Foundever, TTEC, Teleperformance, CVS Health,
  UnitedHealth Group, JPMorgan Chase, Walmart, Target, Hilton, Marriott, Progressive,
  State Farm, and Allstate.
- Keep the mass-hiring company, job, observation, question, score, and application
  tables isolated from the legacy vacancy and application pipeline.
- Never submit an application automatically. Queue admission creates only a
  `pending_review` record and final submission requires an explicit user action.

## Complete employer identity

Every accepted employer must have a legal name, brand name, employer segment,
employee-size evidence, industry, headquarters, official domain, careers URL, ATS
classification, source timestamps, confidence, and provenance.

An official domain needs two independent factors. At least one factor must be a
structured or authoritative source that links a durable entity identity to that exact
domain. The second factor must be live official-site identity evidence. Search rank,
name-only matches, ATS hosts, social pages, directory pages, and homepage body mentions
cannot independently verify a domain.

Employee-count conflicts, unresolved parent/subsidiary collisions, payroll/shell
entities, funds/trusts, sentence-like aggregate names, and mismatched jurisdictions are
quarantined. Staffing, government, education, healthcare, and nonprofit employers are
kept in explicit lanes so they can be ranked with suitable criteria.

## Careers and REMOTE jobs

- Resolve the canonical careers page and exact ATS tenant identity. Shared SAP or
  Eightfold hosts without customer identity are incomplete.
- Support Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, iCIMS, Oracle,
  SuccessFactors, Eightfold, and custom career sites.
- Store only jobs whose official source explicitly establishes REMOTE status. Hybrid,
  optional-remote, and ambiguous postings are excluded.
- Preserve full JD HTML and text, title, source identifiers, canonical job/apply URLs,
  department, locations, employment type, salary, dates, raw payload, and every
  application question visible without submitting.
- Account-, consent-, or multi-step-gated questions remain explicitly partial; they are
  never represented as complete.

## Observation, scoring, and promotion

Absence can close a prior job only after a connector reports a complete successful
scan. Retain first/last seen timestamps and immutable snapshots for changes,
disappearance, and reopening.

After multiple complete observation cycles, retain reproducible evidence for remote,
entry-level, mass-hiring, hiring-activity, and application-ease scores. Monitoring
requires a unique employer representative, no unresolved risk flags, active recent
REMOTE hiring, score confidence at least 0.70, remote score at least 40, mass-hiring
score at least 50, and hiring-activity score at least 45.

Application-queue admission additionally requires an active REMOTE job and complete
question collection. Every queued row remains pending user review.

## Final audit

Completion means all 10,000 rows pass database assertions for required fields and
provenance, every connector has fixture tests plus bounded live checks, at least two
observation cycles are retained, scoring is reproducible, UI controls are verified on
desktop and mobile, legacy tables were not written, and a stratified manual sample
covers accepted, ambiguous, duplicate, low-score, and rejected employers.
