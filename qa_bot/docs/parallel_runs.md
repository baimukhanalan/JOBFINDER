# Bounded parallel QA runs

`qa_bot.parallel_supervisor` owns up to ten independent Chrome workers. Planning
does not visit JobFinder or start an assessment. It selects explicit original row
numbers from `runs/authorized-scope.json`; it never reselects the current first 30
rows from the changing inbox. The manifest includes the scope file hash, exact
profile mapping, evidence directories, and four separate localhost ports per
worker. Changed scope or edited manifests are rejected.

The real batch used original rows 7, 8, 9, 13, 14, 15, 16, 17, 21 and 29 in
learning mode. This is the corresponding offline planning syntax; these recorded
attempts are now finished and must not be treated as unused invitations:

```sh
cd /Users/alanbaimukhan/Documents/PROJECTS/jobf/SHL-question-bank/qa_bot
PYTHONPATH=src .venv/bin/python -m qa_bot.parallel_supervisor plan \
  --scope runs/authorized-scope.json \
  --output runs/my-reviewed-batch \
  --rows 7,8,9,13,14,15,16,17,21,29 \
  --concurrency 10 --base-port 25769 \
  --speech-bank-dir runs/shared-speech --learn
```

Review `manifest.json` before starting. These example rows are within the recorded
scope; the plan does not prove that their invitations are fresh or unconsumed.
The shared speech directory must contain the intended speech bank and its audio
files. Workers default to `--replay-only`; a missing exact answer requires
attention and does not enable model generation or skip the question. For an
explicitly selected learning run, add `--learn` when creating its plan. That plan
records `replay_only: false` and permits generation for previously unseen answers.
Exact existing answers are still reused. A later replay verification needs its own
default replay-only plan; a learning run does not prove independence from models.
The older `runs/parallel-10-replay-20260907/manifest.json`, using rows
4, 5, 6, 10–16, was never started and is not a ready batch. Actual preflight found
no single TP invitation for rows 4, 5, 6, 11 and 12, a completed invitation for
row 10, and resume screens for rows 13–16. Its original freshness assumption was
invalid; manifest validity alone does not establish usable or fresh invitations.

With JobFinder credentials already present in the local process environment:

```sh
PYTHONPATH=src .venv/bin/python -m qa_bot.parallel_supervisor start \
  runs/my-reviewed-batch/manifest.json
PYTHONPATH=src .venv/bin/python -m qa_bot.parallel_supervisor status \
  runs/my-reviewed-batch/manifest.json
```

The launcher returns immediately. A detached supervisor owns each browser's input
pipe, so ending a chat tool invocation does not close the workers' standard input.
Each attempt gets a separate Chrome process/context, process group, output
directory, localhost port set, and generated token supplied only through its
environment. OS locks reserve profiles across batches; already running interactive
`qa_bot.live_session` processes for the selected names or profile IDs are rejected.
Each batch also has one exclusive supervisor lock. Existing run output cannot be
overwritten or automatically restarted.

Terms are never accepted by the scheduler. `awaiting_terms` leaves that browser
open for review. After reviewing that attempt's terms, an explicit local operator
command can send the existing guarded acceptance action:

```sh
PYTHONPATH=src .venv/bin/python -m qa_bot.parallel_supervisor control \
  runs/my-reviewed-batch/manifest.json --row 7 --action accept_reviewed_terms
```

`--action close` explicitly stops a selected attempt. There is no arbitrary
JavaScript, click, skip, or restart command in this control channel. Submitted
controls remain as local audit records.

The status meanings deliberately distinguish execution from success:

| State | Evidence |
|---|---|
| `running` | Worker processes remain alive. |
| `awaiting_terms` | The worker observed the terms screen; no acceptance sent. |
| `needs_attention` | A module failed, a control request was invalid, or final confirmation/coverage is incomplete; the browser stays open without restarting the attempt. |
| `platform_complete` | All required submission coverage and two fresh clean final-page observations at least one second apart were verified. This does not establish correctness or an official score. |
| `timed_out` | The platform or runner reported a timeout/failure. |
| `failed` | A worker/service exited or failed to start. |
| `interrupted` / `cancelled` | The supervisor or an explicit operator stopped the attempt. |

The supervisor does not infer that an answer was correct, reset assessment
timers, or respawn failed attempts. It closes its child process groups on normal
shutdown and handled termination. A host crash or uncatchable process kill needs
manual process/evidence inspection; automatic recovery is intentionally absent.
The status command checks whether the recorded supervisor PID is alive, rather
than treating an old status file as a live run.

Final-page detection accepts the exact terminal message with the known skip link;
other visible content, including a submit confirmation, prevents successful
closure. The supervisor obtains two fresh state responses through the input pipe
it already owns. Old observations cannot verify a later final-page transition.
`qa_bot.run_audit` derives required modules from the actual assessment menu and
checks unique submissions, missing questions, duplicates, timeouts, and the last
simulation submission. A final screen with Sales 0/20 remains incomplete and keeps
the browser available for investigation.

Page and observer errors remain visible as diagnostic counts rather than blocking
terms control. Actual module failures retain `needs_attention`. Terms visibility
is tracked separately; the explicit reviewed-terms action also retains its live
DOM guard. External command records omit invitation URLs, tokens, answer text,
and raw command payloads. `no_logged_manual_answer_controls` describes recorded
commands only; it cannot exclude unlogged physical browser interaction.

Validated locally with fake subprocess workers: ten-way manifest isolation,
bounded scheduling, duplicate profile/owner rejection, scope tampering rejection,
busy ports, error preservation, explicit terms control, timeout/exit distinction,
partial log records, process cleanup, and survival after a detached launcher exits.
The latest confirmed full suite passed 367 tests after the submission diagnostics
and read-only inventory changes.

Additional measurements on the local 16 GiB, ten-core host used no new assessment
invitations:

| Local workload | Observed result |
|---|---|
| Ten independent headless Chrome processes, native DOM clicks, shared virtual microphones and MediaRecorder | 1010 clicks, ten audio streams, no errors; the exercise took 4.00 seconds. |
| Memory during that Chrome workload | Minimum system-wide free percentage from `memory_pressure -Q`: 50%; peak summed RSS: 8.68 GiB. Summed RSS can count shared mappings repeatedly and is not private footprint. |
| Ten processes requesting the same complete 17-clip, 98.01-second conversation with an empty shared transcript cache | 58.07 seconds; 17 actual STT calls and 153 cache reuses, with at most one STT model active. |
| The same ten conversations with the populated cache | 170 lookups in 0.117 seconds; no STT calls. |

The STT measurement used a separate cache and disabled model downloads. The Chrome
fixture was local and lightweight; it was not ten full assessment pages. Ten
different uncached conversations and ten concurrent new Codex requests were not
measured. STT serializes different clips through one shared worker with a
180-second lock wait. Answer preparation locks coalesce identical questions;
there is no global Codex queue for unrelated questions. Replay-only mode avoids
new LLM calls but still requires complete answer coverage. These results support
ten local workers on known repeated content. The later external measurements below
are separate evidence, with their own coverage limits.

## Observed external batch and recovery

`runs/parallel-ten-resume-20260907/manifest.json` launched ten independent browser
owners and thirty service processes concurrently. All ten invitations had been
classified `resume_required`; none was established as fresh. The main batch ran
from 2026-09-06 22:24:42.642 UTC for 600.34 seconds, including operator cleanup.
It ended with three normal platform exits, five server cutoffs and two technical
stops. Those two existing invitations were then continued in
`runs/parallel-ten-recovery-20260907/manifest.json`, using two workers and separate
ports, without resetting the attempts.

| Final result after recovery | Original rows | Evidence |
|---|---|---|
| Normal platform termination | 7, 8, 9, 29 | Final Sales submission followed by HTTP 200 on test end with `exitType: 104`. Row 9 required recovery to finish WriteX and Sales. |
| Server cutoff after Analytical | 13, 14, 15, 16, 17, 21 | HTTP 200 on module switch with `moduleSwitched: false` and `cutoff_cleared: false`, followed by test end with `exitType: 101`. Row 16 completed its remaining seven Analytical questions during recovery. |

The recoveries resolved the two code stops; they did not turn server cutoffs into
successful assessments. Rows 8 and 29 each have 119 newly evidenced submissions
out of the full 140-question WriteX variant: C 5, D 1, scored Typing 1,
Personality 72, Analytical 19, WriteX 1 and Sales 20. Their earlier 21 SVAR A/B
answers were outside these resumed runs. Row 7 also reached Sales 20 but has only
71 Personality submission log records, with a question-change error requiring
separate server-evidence review. Normal termination is neither a published grade
nor proof of hiring eligibility. No fresh full single assessment or ten full
assessments from their first question are claimed.

Both supervisors are now `finished`; all owned browser/service processes were
closed by explicit operator cleanup. Their per-attempt `cancelled` statuses
describe this cleanup, not the actual assessment result. No active attempt or
automatic recovery remains in either batch.

| Measurement | Observed result |
|---|---|
| Real simultaneous owners | Ten browser owners and thirty service processes were observed alive together. |
| Main-batch LLM calls | 29 actual subprocess starts: 28 completed responses and one abstention; no pending call, transport error or timeout at closure. |
| Recovery LLM calls | Four additional starts: three completed responses and one abstention. |
| Combined LLM calls | 33 starts: 31 completed responses and two abstentions. This was a learning run, not a zero-LLM replay proof. |
| Main-batch model concurrency and delay | At most four model calls simultaneously; longest completed call 20.229 seconds. Ten simultaneous cold model calls were not demonstrated. |
| Successful STT records | 51 per-attempt records: 26 newly transcribed and 25 reused; 39 distinct audio hashes, including 26 newly transcribed hashes. |
| Main-batch sampled memory | Minimum system-wide free percentage 42%; peak summed descendant RSS 14,929.7 MiB; peak swap used 3,246 MiB. |

The main resource monitor collected 44 snapshots at approximately twelve-second
intervals, starting about ninety seconds after launch. Startup peaks before that
point are unknown. Summed RSS counts shared mappings repeatedly and is not private
memory usage. Recovery resource monitoring began after its final pages and cannot
establish a recovery startup peak. STT has no inference-start ledger: successful
transcript records alone cannot prove absence of failed inference calls.

Each Codex subprocess writes `isolated/model-calls.jsonl` under its owning evidence
directory. Count unique `call_id` values with `event: started`; corresponding
completion records distinguish completed responses, abstentions, errors and
timeouts. A successful answer reused later does not erase an earlier model call.
The ledger contains safe identifiers, hashes and timing, not raw questions,
answers or credentials. Telemetry write failures do not stop answering, so an
absent ledger alone cannot prove zero calls. A `completed` transport response also
does not prove that answer validation or correctness checks succeeded.

The detailed main and recovery `summary.json` files, their
`resource-monitor.jsonl` files, and the main batch's
`combined-recovery-summary.json` preserve the results and closure status outside
Git. The combined report records four normal terminations and six server cutoffs,
separately from operator cleanup.

Local measurement artifacts are in `runs/parallel-load-20260907/`. Assessment
content, recordings, profile inventories and detailed reports remain outside Git.

Read-only diagnostics are available through `control MANIFEST --row N --action inspect`.
The owner receives only state, controls, screenshot, and sample-device diagnostics;
this command cannot inject clicks, arbitrary commands, or a reload.
