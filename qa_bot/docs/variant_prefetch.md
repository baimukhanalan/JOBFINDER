# Exact variant recognition and audio prefetch

`ExactVariantRecognizer` groups identical question sequences into one variant.
A variant can therefore have several source profile/test IDs.  The runtime uses
only an exact prefix of normalized question text and ordered options; it never
uses fuzzy similarity, a majority vote, or the profile identity.

## Runtime flow

1. Capture and persist the current question immediately.
2. Pass the first observed question in a section to `identify(...)`.  Even one
   unique known branch remains `tentative` by default: an unseen variant could
   share that first question.  The prefetch gate requires two observations.
3. If the result is `tentative` or `ambiguous`, submit the current answer through
   the ordinary exact-answer path and observe the next question.  Do not prefetch
   a branch.
4. After `recognized`, call `plan_audio_prefetch(...)`.  The plan labels each
   future spoken item as `exact_replay` when the MP3/WAV is already in
   `SpeechReplayBank`, or `local_tts` when local synthesis is required.
5. Call `materialize_audio_prefetch(...)` during the page's preparation phase.
   It prepares independent items concurrently and preserves sequence order.
6. At `Speak Now`, the timed speech path may only start an already armed
   `AudioBuffer`.  A missing or failed prefetch stops that question.

The planner returns no work for tentative/ambiguous/unknown prefixes, missing
answers, or conflicting answers in duplicated source sequences.  A changed
punctuation mark is an unknown question rather than a match.

## Current-corpus measurement

The measurement uses `data/questions.jsonl` and `SHL_answers_all.csv` as they
stood on 2026-09-06.  Thirteen source tests contain 39 section assignments that
collapse to 28 distinct section variants.

| Observed prefix | Resolved variants | Variant coverage | Resolved assignments | Assignment coverage |
| --- | ---: | ---: | ---: | ---: |
| First question | 26 / 28 | 92.9% | 27 / 39 | 69.2% |
| Up to two questions | 28 / 28 | 100% | 39 / 39 | 100% |
| Up to three questions | 28 / 28 | 100% | 39 / 39 | 100% |

The first-question row is raw corpus separability.  Runtime prefetch remains
blocked at one question by default to guard against a new variant that shares a
known first question.  Every recognition emitted after the two-question gate was
correct on this closed corpus.  Median-scale in-process lookup is
well below one millisecond (measured averages: 0.018 ms for one question,
0.029 ms for two, and 0.042 ms for three); corpus loading took 14.1 ms.

These numbers measure separation of already collected variants, not prediction
of a new unseen sequence.  In leave-one-profile validation, 13 of 39 assignments
(33.3%) had the same full variant in another profile; all 13 were identified
correctly after two questions.  The remaining unique held-out variants returned
`unknown`.  Their current questions must use the real-time local solve/TTS path
and are saved so the next occurrence becomes an exact replay candidate.
