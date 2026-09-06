# Bounded parallel QA runs

`qa_bot.parallel_supervisor` owns up to ten independent Chrome workers. Planning
does not visit JobFinder or start an assessment. It selects explicit original row
numbers from `runs/authorized-scope.json`; it never reselects the current first 30
rows from the changing inbox. The manifest includes the scope file hash, exact
profile mapping, evidence directories, and four separate localhost ports per
worker. Changed scope or edited manifests are rejected.

Create a reviewable offline plan, using the rows selected for this batch:

```sh
cd /Users/alanbaimukhan/Documents/PROJECTS/jobf/SHL-question-bank/qa_bot
PYTHONPATH=src .venv/bin/python -m qa_bot.parallel_supervisor plan \
  --scope runs/authorized-scope.json \
  --output runs/my-reviewed-batch \
  --rows 4,5,6,10,11,12,13,14,15,16 \
  --concurrency 10 --base-port 23769 \
  --speech-bank-dir runs/shared-speech
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
The reviewed offline candidate plan at
`runs/parallel-10-replay-20260907/manifest.json` uses these ten rows, the shared
speech bank, and ports 23769–23808. It has not been started. Its selected
invitations still require a freshness check; a successful full single external
attempt remains the gate before the external batch.

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
  runs/my-reviewed-batch/manifest.json --row 4 --action accept_reviewed_terms
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
The latest confirmed full suite passed 337 tests; a subsequent targeted run passed
33 tests. A new full-suite result after the latest changes is not yet claimed.

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
ten local workers on known repeated content, not a claim that ten external tests
have been completed.

External control attempt 26 ended with 135/155
required submissions with no logged manual answer commands after terms: SVAR 27,
Typing 1, Personality 72, Analytical 19 and Computer 16; Sales remained 0/20.
Each computer task produced a client `message.score: 1`, which does not establish
the overall assessment grade. The last simulation submission occurred before its
normal two-second save delay had elapsed; that ordering is being corrected. A successful
`/api/v1/test/end` request preceded browser closure, so the missing Sales module
cannot yet be attributed conclusively to either early submission or early closure.
The former final-text-only shutdown has been replaced by the coverage and stable
final-page checks above. Row 27 has started; no successful outcome is claimed yet.

Local measurement artifacts are in `runs/parallel-load-20260907/`. Assessment
content, recordings, profile inventories and detailed reports remain outside Git.

Read-only diagnostics are available through `control MANIFEST --row N --action inspect`.
The owner receives only state, controls, screenshot, and sample-device diagnostics;
this command cannot inject clicks, arbitrary commands, or a reload.
