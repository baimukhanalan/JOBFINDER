# Answer Coverage & Per-Person Résumés — Design

Date: 2026-06-11
Branch: feat/semi-auto-apply-engine
Status: approved

## Problem

The semi-auto apply engine will be used at volume by 5+ people (all customer-support /
adjacent roles). Current gaps:

1. **Question coverage is far from 100%.** Rules cover identity/contact + ~10 fixed
   yes/no screeners. Open-text questions are LLM-drafted (max 8 per form) with a prompt
   hardcoded to "customer-support candidate". Closed role-specific screeners
   (selects/radios/multi-checkboxes: "experience with X?", shift choice, salary band,
   education level) are not answered at all — left to the human.
2. **The answer cache is shared across people.** `answer_cache` keys only by normalized
   question, so one person's drafted answer (with their facts) is served to everyone,
   and 5+ applicants end up submitting word-for-word identical answers.
3. **Résumé bodies are shared.** `etalons.json` holds one person's 15 niche résumé
   bodies; other profiles get only their name/email injected — everyone applies with
   the same work history.
4. **The local LLM (Sumrak on :8080) is weak.** Decision: keep it (free), but the
   architecture must not depend on its intelligence.

## Decisions (user-confirmed)

- All users apply to CS/adjacent roles only — no universal-profession generator.
- Keep Sumrak as the LLM; improve prompts and constrain its tasks.
- Closed screeners: auto-answer from per-person facts; when no fact backs the choice,
  fill the most reasonable option and flag `[review]` — human confirms before submit.
- Each person gets their own etalon set (real experience per person).
- Architecture: cascade — deterministic bank → constrained LLM choice → LLM free text →
  `[review]` queue. Never LLM-first.

## Success criterion

For a typical supported-ATS form, the human's only remaining work is: skim the
`[review]`-flagged answers and click Submit. No manual typing. Forms where that
doesn't hold (login wall, captcha, unanswerable required question) are reported as
such, not half-filled silently.

## Design

### 1. Per-person fact sheet — `backend/data/facts/<profile>.json`

One questionnaire per person, the single source of truth for both rules and prompts.
Fields (all optional, flat JSON): shift availability (nights/weekends/overtime),
salary range (hourly + annual), notice period / earliest start, languages, typing WPM,
tools used (Zendesk, Salesforce, Intercom, ...), education level, US state + timezone,
equipment (computer/headset/internet), industry experience flags (healthcare, fintech,
travel, e-commerce, ...), people-management experience, driver's license, consent to
drug test / background check, referral default.

Loader in `backend/profiles/` next to the existing profile store; missing file →
empty facts (engine degrades to current behavior, nothing crashes).

### 2. Rules v2 — `backend/applier/analyzer.py`

Extend `FIELD_PATTERNS` from ~25 to ~50–60 patterns, resolving values from the fact
sheet instead of hardcoded Yes/No where the answer is person-specific: shifts,
weekends, overtime, salary expectation (from range), notice period, education,
languages, typing speed, timezone, state, referral, drug test, equipment, prior
employment at the company. Existing safety guards stay (foreign work-auth, open-ended
detection, label-only keys, no negative auto-checks on radio/checkbox).

### 3. Constrained option choice — new `backend/services/tailor/choices.py`

For every closed question (select/radio/multi-checkbox) the rules didn't cover:
one Sumrak call per form, batched — input: list of {question, options[]}, plus the
fact sheet and the chosen variant's key facts; output: JSON array of option indexes
(or null = leave for human). Validation: each answer must parse to a valid index;
one retry; otherwise the question goes to the `[review]` queue. A choice that is not
directly backed by a fact-sheet fact is filled but flagged `[review]` in the report.

### 4. Open-text drafting v2 — `backend/services/tailor/answers.py`

- Build the prompt from the person's fact sheet + the selected résumé variant
  (niche label, years, headline) + job title/company — remove the hardcoded
  "customer-support candidate" line.
- Raise the per-form cap from 8 to 20 questions, processed in chunks of 8 per LLM
  call (a weak model stays reliable on small batches).
- Keep: no-fabrication rules, `[review]` prefix for behavioral questions, retry with
  backoff, empty-on-failure.

### 5. Answer cache keyed per person + niche — `backend/answer_cache.py`

Key becomes `(profile, niche, normalized_question)`. The existing DB is a cache —
drop/recreate the table with the new composite key, no migration. `get_many`/
`put_many` take profile + niche from the caller (`strategies/base.py`). `<co>`
genericize/personalize stays. This removes identical answers across people; within
one person, reuse across companies is normal human behavior.

### 6. Per-person etalons — `backend/services/tailor/variants.py`

`backend/data/etalons.json` → `backend/data/etalons/<profile>.json` (current file
becomes the existing profile's set). `variant_for(job, profile)` loads the applying
profile's set; profile without a set → variants disabled for them (current fallback
path). Onboarding a new person = fact sheet + their real experience authored into
their niche set (drafts may be generated from their base résumé, the person reviews).

### 7. Report & submit gate

Per-form report gains counts by source: `rule` / `fact` / `llm_choice[review]` /
`llm_draft[review]` / `left_for_human`. The existing completeness gate before
`--submit` extends: unconfirmed `[review]` items block auto-submit; semi-auto
(human submits) flow unchanged.

## Out of scope

- Paraphrase rotation of cached answers (revisit if ATS-side duplication is observed).
- Universal non-CS profession support.
- Switching the LLM backend (config already allows Anthropic fallback).

## Testing

- Unit: rules-v2 resolution from a fact sheet fixture; choices.py index validation
  (valid / out-of-range / garbage / null); cache keying (two profiles, same question →
  distinct rows); variants loading per profile.
- Analyzer regression: pure-logic tests on field matching / radio-group merging
  (HTML fixture forms per ATS are a possible follow-up, not in this iteration).
- Manual: one real prefill run per ATS with `--draft`, verify report source counts.
