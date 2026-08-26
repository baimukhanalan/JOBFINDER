# Mass Hiring 2,000 — acceptance contract

This phase is employer-focused. Legal-entity registries are discovery inputs only and do not
count toward the 2,000 accepted employers.

## Required population

- Exactly 2,000 US employers in `company_employer_master` with `identity_status=verified`.
- The mandatory seed includes Amazon, Concentrix, Foundever, TTEC, Teleperformance, CVS Health,
  UnitedHealth Group, JPMorgan Chase, Walmart, Target, Hilton, Marriott, Progressive, State Farm,
  and Allstate.
- Every accepted row has brand name, legal/company name, employee-size evidence, industry,
  US headquarters, official domain, careers URL, and an explicit ATS/custom classification.

## Identity gate

An official domain is accepted only with two independent evidence classes:

1. a structured corporate/public source linking the employer to the domain; and
2. official-site identity evidence that matches the brand plus at least one corroborating
   attribute (headquarters, corporate/legal name, or a durable registry/filing identifier).

Name-only Wikidata matches, search rank, directory pages, social profiles, ATS hosts, and generic
homepage text are insufficient. Ambiguous or conflicting candidates remain quarantined.

## Hiring gate

An employer can enter monitoring only when all of the following are true:

- its careers page and ATS/custom source were successfully scanned;
- at least one current job was observed, or a successful zero-job scan was repeated in a later
  observation cycle;
- remote jobs are stored only when the source explicitly confirms remote status;
- full JD, apply URL, source timestamps, and application-question status are recorded;
- source completeness is known, so an incomplete scan never closes prior jobs.

An employer can enter the application queue only when it has current eligible remote jobs and a
non-zero hiring-activity score. No company is promoted because it appears in a registry.

## Required scores

After at least two completed observation cycles, calculate and retain evidence for:

- `remote_score`
- `entry_level_score`
- `mass_hiring_score`
- `application_ease_score`
- `hiring_activity_score`

Scores must be reproducible from stored jobs, snapshots, questions, scan history, salary/location
coverage, and posting/closure cadence. Missing evidence produces a lower confidence score; it is
never silently imputed as positive.

## Final audit

Completion requires database assertions, connector fixture tests, live bounded source checks,
desktop/mobile UI verification, no writes to the legacy vacancy/application pipeline, and a
manual review sample covering high-, medium-, low-score, ambiguous, and rejected employers.
