# Bounded parallel QA runs

`qa_bot.parallel_supervisor` owns up to ten independent Chrome workers. Planning
does not visit JobFinder or start an assessment. It selects explicit original row
numbers from `runs/authorized-scope.json`; it never reselects the current first 30
rows from the changing inbox. The manifest includes the scope file hash, exact
profile mapping, evidence directories, and four separate localhost ports per
worker. Changed scope or edited manifests are rejected.

Create a reviewable offline plan, using the rows selected for this batch:

```sh
PYTHONPATH=src .venv/bin/python -m qa_bot.parallel_supervisor plan \
  --scope runs/authorized-scope.json \
  --output runs/my-reviewed-batch \
  --rows 4,5,6,10,11,12,13,14,15,16 \
  --concurrency 10 --base-port 19769 \
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
| `needs_attention` | A module logged an error or a control request was invalid; the attempt is preserved without restarting it. |
| `platform_complete` | The exact platform completion text was observed. Question counts and correctness still need an independent audit. |
| `timed_out` | The platform or runner reported a timeout/failure. |
| `failed` | A worker/service exited or failed to start. |
| `interrupted` / `cancelled` | The supervisor or an explicit operator stopped the attempt. |

The supervisor does not infer that an answer was correct, reset assessment
timers, or respawn failed attempts. It closes its child process groups on normal
shutdown and handled termination. A host crash or uncatchable process kill needs
manual process/evidence inspection; automatic recovery is intentionally absent.
The status command checks whether the recorded supervisor PID is alive, rather
than treating an old status file as a live run.

Validated locally with fake subprocess workers: ten-way manifest isolation,
bounded scheduling, duplicate profile/owner rejection, scope tampering rejection,
busy ports, error preservation, explicit terms control, timeout/exit distinction,
partial log records, process cleanup, and survival after a detached launcher exits.
This local verification does not establish host capacity or successful completion
of ten simultaneous external assessments.
