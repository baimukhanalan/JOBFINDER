"""Offer/interview-efficiency layer for the apply engine (owner directives 2026-09-20).

Three levers, ONE goal — land an interview/offer FAST, without tripping ATS velocity/spam:

  (1) HIGH-PAY FIRST. Order every apply queue by disclosed pay DESC so the best-paying
      positions are attempted first and preferentially (catalog: estimated total / posted comp;
      mass-hiring: hourly rate). Knob: env `APPLY_ORDER` (default `pay_desc`).

  (2) MULTI-CANDIDATE PER POSITION, STOP ON RESPONSE. Apply up to K distinct synthetic personas
      to ONE position to raise the odds one lands, but STOP adding candidates the moment ANY
      persona on that position has reached an INTERVIEW or OFFER (detected from the mail_index
      furthest stage, joined to the position through the prefill persona dirs). Knob: env
      `APPLY_CANDIDATES_PER_POSITION` (K, default 2).

  (3) FAST-OFFER WEIGHT. The offer-producing mass-hiring lanes (BPOs — the offer comes upstream,
      no long tech-interview loop) outrank the ~0-converting synthetic-tech catalog lane. Kept as
      an advisory `lane_priority()` + the pay-desc ordering; cross-lane cadence is the crontab's
      job and already favours the BPO lanes (5×/day) over the operator-triggered catalog drain.

SPAM / VELOCITY SAFETY — why multiplying candidates here does NOT trip the cluster:
  * The per-COMPANY velocity cap (`company_velocity.guard`, 2/day · 6/week) still governs the
    CATALOG path. `plan_positions` runs the whole flat K-per-position plan THROUGH that guard, so
    K copies of a position (all one company) can never exceed that company's remaining daily
    budget — the redundancy raises the odds WITHIN the cap, it does not lift it. K defaults small.
  * MASS-HIRING ATSes (Maximus/TP/Taleo/Foundever/…) are NOT in job_catalog, so the catalog
    velocity guard is a deliberate no-op for them: those lanes are BUILT for volume (each fresh
    synthetic persona mints a fresh assessment invite = another shot at an offer — see the lane
    docs; Maximus intentionally runs 5×/day/job). There the STOP-ON-RESPONSE condition is the
    governor — once a position yields an interview/offer we stop spending personas on it — and the
    identity is already UNIQUE per fill (`synth_persona`), which is the correct anti-cluster lever
    (the Salmon post-mortem: a fixed identity at volume builds the cluster, a fresh one per fill
    does not), not a per-company throttle on lanes designed to fan out.

Everything above the `live wrappers` divider is PURE + injectable, so it unit-tests with no
DB / mail / network / disk. The thin wrappers below do the real joins (job rows, mail furthest
stage, prefill persona counts) and are FULLY GUARDED — any error falls back to the caller's
original behaviour. This layer must NEVER break a lane.
"""
from __future__ import annotations

import logging
import os
from collections import OrderedDict

logger = logging.getLogger("offer_priority")

# mail_index furthest-stage strings that mean the position ALREADY produced the outcome we want
# → stop adding candidates. `offer` may not exist in the current taxonomy (it tops out at
# `interview`); it is included so the stop still fires if/when an offer stage lands. Case-folded.
LANDED_STAGES = frozenset({"interview", "offer"})

# Lanes that produce a real offer FASTEST (BPO mass-hiring: hire-on-the-spot / short assessment,
# the offer is upstream) vs the synthetic-tech catalog lane (0-converts for synthetic personas per
# the funnel). Advisory weight for any mixed-lane planning; higher = attempt first.
FAST_OFFER_LANES = ("maximus", "teleperformance", "tp", "foundever", "taleo", "ttec",
                    "kelly", "smartrecruiters", "sutherland", "workday", "icims", "hiring_events")
_LANE_WEIGHT = {name: 100 for name in FAST_OFFER_LANES}


# ---- knobs -----------------------------------------------------------------------------------
def apply_order() -> str:
    """Queue ordering (env `APPLY_ORDER`): `none`/`as_is` (DEFAULT — keep the caller's order, NO
    high-pay accent, per owner 2026-09-20), `pay_desc` (high-pay first), or `pay_asc`. An unknown
    value → `none`."""
    v = (os.environ.get("APPLY_ORDER") or "none").strip().lower()
    return v if v in ("pay_desc", "pay_asc", "none", "as_is") else "none"


def candidates_per_position(default: int = 2) -> int:
    """K — distinct personas to send to ONE position before the stop condition takes over (env
    `APPLY_CANDIDATES_PER_POSITION`). Clamped 1..8: a big K on the catalog path is trimmed by the
    velocity cap anyway, and on the volume lanes a big K needlessly deepens the spam risk."""
    try:
        k = int(os.environ.get("APPLY_CANDIDATES_PER_POSITION") or default)
    except (TypeError, ValueError):
        k = default
    return max(1, min(k, 8))


def lane_priority(lane: str) -> int:
    """Advisory fast-offer weight for a lane name (higher attempts first); 0 for the catalog lane
    and anything unknown. Cross-lane cadence is the crontab's job; this documents the intent."""
    return _LANE_WEIGHT.get((lane or "").strip().lower(), 0)


# ---- pay keys (pure) -------------------------------------------------------------------------
def _pos_nums(*vals) -> list[float]:
    out: list[float] = []
    for v in vals:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out.append(f)
    return out


def pay_key_catalog(row) -> float:
    """High-pay-first sort key from a `job_catalog` row: the HIGHEST disclosed annual figure over
    posted comp + estimated total/base (matches interview_priority's 'rank on the top figure').
    0.0 when nothing is disclosed (sorts last under pay_desc)."""
    row = row or {}
    nums = _pos_nums(row.get("comp_max"), row.get("comp_min"),
                     row.get("est_total_max"), row.get("est_total_min"),
                     row.get("est_base_max"), row.get("est_base_min"))
    return max(nums) if nums else 0.0


def pay_key_masshiring(row, *, hourly_pay=None) -> float:
    """High-pay-first sort key from a `mass_hiring_jobs` row: the TOP hourly rate. A POSTED rate
    edges out a category ESTIMATE at the same nominal (estimate → a tiny penalty). 0.0 when
    unknown. `hourly_pay` injectable (default `mass_hiring.hourly_pay` — pure, no DB)."""
    if hourly_pay is None:
        from backend.tools.mass_hiring import hourly_pay
    try:
        hp = hourly_pay(row or {})
    except Exception:
        hp = None
    if not hp:
        return 0.0
    lo, hi, is_est = hp
    top = float(hi or lo or 0.0)
    return (top - 0.01) if is_est else top


def order_by_pay(items, pay_key_of, *, order=None) -> list:
    """Stable-sort `items` (job ids OR rows) high-pay-first. `pay_key_of(item) -> float`. `order`
    defaults to `apply_order()`; `none`/`as_is` returns the items unchanged (stable)."""
    order = order or apply_order()
    if order in ("none", "as_is"):
        return list(items)
    return sorted(items, key=lambda it: pay_key_of(it), reverse=(order != "pay_asc"))


# ---- stop-on-response (pure) -----------------------------------------------------------------
def is_landed(stage) -> bool:
    """True iff a furthest-stage string means the position already reached interview/offer."""
    return (stage or "").strip().lower() in LANDED_STAGES


def landed_positions(jobids, furthest_stage_of) -> set:
    """The subset of `jobids` whose position already reached interview/offer.
    `furthest_stage_of(jobid) -> stage str | None` (injectable). Errors per id are swallowed
    (an unknown stage is NOT landed — never wrongly stop a live position)."""
    out: set = set()
    for j in jobids:
        try:
            if is_landed(furthest_stage_of(j)):
                out.add(j)
        except Exception:
            pass
    return out


_UNLIMITED = 1 << 30


def remaining_candidates(applied_count, stage, k) -> int:
    """How many MORE personas a position should still receive: 0 if it already landed
    (interview/offer), else max(0, K - already_applied). `k <= 0` means an UNLIMITED lifetime
    budget (the volume lanes rely on the stop-on-response + a per-run cap instead)."""
    if is_landed(stage):
        return 0
    if int(k) <= 0:
        return _UNLIMITED
    return max(0, int(k) - max(0, int(applied_count or 0)))


def plan_positions(jobids, *, furthest_stage_of=None, applied_count_of=None, pay_key_of=None,
                   k=None, per_position_cap=None, order=None, velocity_guard=None,
                   dedupe=True) -> list:
    """Build the ordered, capped, guard-trimmed flat apply plan for a set of POSITIONS.

    (1) de-dupe + order the positions high-pay-first (`order_by_pay`);
    (2) per position compute how many MORE personas it needs — 0 if it already reached
        interview/offer (STOP), else K − already-applied (`remaining_candidates`), the LIFETIME
        budget. `per_position_cap` (default None = no extra cap) further limits how many copies
        THIS invocation emits (e.g. a mass-hiring run's `rounds`), so a run never over-fires even
        when the lifetime budget is large;
    (3) emit that many copies (the multi-candidate redundancy), preserving pay order;
    (4) run the whole flat plan through `velocity_guard` (the per-company cap) so K-per-position
        can never exceed a company's remaining budget.

    Every hook is injectable and defaults to a safe no-op (`furthest_stage_of` → nothing landed,
    `applied_count_of` → 0, `velocity_guard` → identity), so the planner is fully unit-testable
    and a live wrapper only supplies the joins. Returns the ordered target-id list (with repeats).
    `k=0` means an UNLIMITED lifetime budget (used by the volume lanes that only want the
    stop-on-response + per-run cap, never a lifetime cap that would starve their invite pipeline)."""
    k = candidates_per_position() if k is None else int(k)
    pay_key_of = pay_key_of or (lambda j: 0.0)
    furthest_stage_of = furthest_stage_of or (lambda j: None)
    applied_count_of = applied_count_of or (lambda j: 0)

    ids = list(OrderedDict.fromkeys(jobids)) if dedupe else list(jobids)
    ids = order_by_pay(ids, pay_key_of, order=order)

    plan: list = []
    for j in ids:
        try:
            stage = furthest_stage_of(j)
        except Exception:
            stage = None
        try:
            applied = applied_count_of(j)
        except Exception:
            applied = 0
        need = remaining_candidates(applied, stage, k)
        if per_position_cap is not None:
            need = min(need, max(0, int(per_position_cap)))
        plan.extend([j] * need)

    if velocity_guard is not None and plan:
        try:
            kept = velocity_guard(plan)
            if isinstance(kept, tuple):
                kept = kept[0]
            plan = list(kept)
        except Exception:
            logger.info("[offer_priority] velocity_guard error — plan left uncapped", exc_info=False)
    return plan


# ================================================================================================
# live wrappers — DB / mail / disk joins. FULLY GUARDED: on ANY error fall back to the caller's
# original behaviour (return the input unchanged / an empty map). This layer must never break a lane.
# ================================================================================================
_PREFILL_DIR = None


def _prefill_root():
    global _PREFILL_DIR
    if _PREFILL_DIR is None:
        from pathlib import Path
        _PREFILL_DIR = Path(__file__).resolve().parents[2] / "uploads" / "prefill"
    return _PREFILL_DIR


def personas_by_jobid(jobids, *, prefill_root=None) -> dict:
    """{jobid(str): [persona_email, ...]} — every persona that has a prefill dir for that jobid.
    Scans `uploads/prefill/<demo>/<jobid>/persona.json` (the same code-path-agnostic hit log
    `company_velocity` uses). `jobids` may mix ints (catalog) and `mh_<id>` strings (mass-hiring).
    `uploads/` is gitignored PII → absent from worktrees, so this resolves only where the tree
    exists (the live deploy). Any error → {} (caller treats every position as un-landed)."""
    import json
    import os as _os
    want = {str(j) for j in (jobids or [])}
    if not want:
        return {}
    root = prefill_root or _prefill_root()
    out: dict[str, list] = {}
    try:
        for demo in _os.scandir(root):
            if not demo.is_dir():
                continue
            for jd in _os.scandir(demo.path):
                if not jd.is_dir() or jd.name not in want:
                    continue
                email = ""
                try:
                    pj = _os.path.join(jd.path, "persona.json")
                    prof = (json.loads(open(pj, encoding="utf-8").read()) or {}).get("profile") or {}
                    email = (prof.get("email") or "").strip().lower()
                except Exception:
                    email = ""
                out.setdefault(jd.name, [])
                if email:
                    out[jd.name].append(email)
    except Exception as exc:
        logger.info("[offer_priority] personas_by_jobid scan failed: %s", exc)
        return {}
    return out


def applied_counts(jobids, *, prefill_root=None) -> dict:
    """{jobid(str): n} — how many personas have already applied to each position (prefill-dir
    count). Used as `applied_count_of` so a position that already got K personas gets no more."""
    return {j: len(emails) for j, emails in
            personas_by_jobid(jobids, prefill_root=prefill_root).items()}


def _stage_by_mailbox(emails) -> dict:
    """{email(lower): furthest_stage} for a set of persona mailboxes, via mail_db. Guarded → {}
    on any error. Uses `mail_db.furthest_stage_for` when present, else a direct query fallback."""
    emails = sorted({(e or "").strip().lower() for e in (emails or []) if e})
    if not emails:
        return {}
    try:
        from backend.tools import mail_db
    except Exception:
        return {}
    fn = getattr(mail_db, "furthest_stage_for", None) or getattr(mail_db, "furthest_stages", None)
    if callable(fn):
        try:
            res = fn(emails) or {}
            return {str(k).strip().lower(): v for k, v in res.items()}
        except Exception:
            pass
    # Fallback: the per-mailbox furthest stage straight from _FURTHEST_STAGE_SQL if exposed.
    sql = getattr(mail_db, "_FURTHEST_STAGE_SQL", None)
    if not sql:
        return {}
    try:
        with mail_db.conn() as cx, cx.cursor() as cur:
            cur.execute(
                "SELECT mailbox, (" + sql + ") AS stage FROM mail_index "
                "WHERE lower(mailbox) = ANY(%s) GROUP BY mailbox", (emails,))
            return {str(m).strip().lower(): s for m, s in cur.fetchall()}
    except Exception as exc:
        logger.info("[offer_priority] _stage_by_mailbox query failed: %s", exc)
        return {}


def furthest_stage_by_jobid(jobids, *, prefill_root=None, stage_by_mailbox=None) -> dict:
    """{jobid(str): furthest_stage} — the FURTHEST stage any persona on each position reached
    (max by `_STAGE_RANK`). Joins position → prefill personas → mail_index stage. Injectable
    `stage_by_mailbox` for tests. Guarded → {} (caller then treats every position as un-landed)."""
    byjob = personas_by_jobid(jobids, prefill_root=prefill_root)
    if not byjob:
        return {}
    all_emails = {e for emails in byjob.values() for e in emails}
    stages = (stage_by_mailbox or _stage_by_mailbox)(all_emails)
    out: dict[str, str] = {}
    for j, emails in byjob.items():
        best, best_rank = None, -1
        for e in emails:
            st = stages.get((e or "").strip().lower())
            r = _STAGE_RANK.get((st or "").strip().lower(), -1)
            if r > best_rank:
                best, best_rank = st, r
        if best:
            out[j] = best
    return out


# Furthest-stage precedence (higher = further along the funnel). Mirrors the outcome ordering
# used by /stats; only interview/offer count as `landed` for the stop condition, but the full
# ranking lets furthest_stage_by_jobid pick the best stage across a position's personas.
_STAGE_RANK = {
    "offer": 6, "interview": 5, "assessment": 4, "action_needed": 3,
    "rejection": 2, "ack": 1, "other": 0,
}


def landed_check(jobids, *, prefill_root=None, stage_by_mailbox=None):
    """A ready-to-inject `furthest_stage_of` for the LIVE path: pre-computes the stage map for the
    whole `jobids` set in one scan+query, returns a closure jobid→stage. Works for catalog ints
    (prefill dir = the bare int) AND mass-hiring `mh_<id>` strings. On any failure the closure
    returns None for everything (nothing is treated as landed → never wrongly stop a live job)."""
    try:
        stages = furthest_stage_by_jobid(jobids, prefill_root=prefill_root,
                                         stage_by_mailbox=stage_by_mailbox)
    except Exception:
        stages = {}
    return lambda j: stages.get(str(j))


def mh_candidates_per_position(default: int = 0) -> int:
    """LIFETIME per-position cap for the MASS-HIRING volume lanes (env `MH_CANDIDATES_PER_POSITION`).
    Default 0 = UNLIMITED: those ATSes are BUILT for volume (each fresh synthetic persona mints a
    fresh assessment invite = another offer shot — Maximus is the SHL-OPQ invite source), so the
    default governor is the STOP-ON-RESPONSE, not a lifetime cap that would starve that pipeline.
    Set >0 only if the owner wants to also cap total personas per job. Clamped 0..50."""
    try:
        k = int(os.environ.get("MH_CANDIDATES_PER_POSITION") or default)
    except (TypeError, ValueError):
        k = default
    return max(0, min(k, 50))


def plan_mh_batch(ids, *, rounds=1, rows_by_id=None, prefill_root=None, stage_by_mailbox=None,
                  k=None, order=None) -> list:
    """LIVE wrapper for a mass-hiring lane's per-run batch. Replaces a blind `ids * rounds` with:
    high-pay-first ORDER + STOP-ON-RESPONSE (drop jobs whose position reached interview/offer) + an
    optional lifetime cap (`MH_CANDIDATES_PER_POSITION`, default off) + `rounds` copies/run. `ids`
    are `mass_hiring_jobs` primary keys; the prefill jobid namespace is `mh_<id>`. FULLY GUARDED:
    on ANY error returns the plain `list(ids) * rounds` (the caller's original behaviour). Also
    returns exactly that on a worktree where `uploads/` is absent (no personas → nothing landed →
    every job gets its `rounds` copies). Returns `mass_hiring_jobs` ids (NOT `mh_` strings)."""
    ids = [int(x) for x in (ids or [])]
    rounds = max(1, int(rounds or 1))
    fallback = ids * rounds
    if not ids:
        return []
    try:
        if rows_by_id is None:
            from backend.tools import mass_hiring
            rows_by_id = {i: (mass_hiring.job_by_id(i) or {}) for i in ids}
        from backend.tools.mass_hiring import hourly_pay
        mh_ids = [f"mh_{i}" for i in ids]
        stage_map = furthest_stage_by_jobid(mh_ids, prefill_root=prefill_root,
                                            stage_by_mailbox=stage_by_mailbox)
        applied_map = applied_counts(mh_ids, prefill_root=prefill_root)
        return plan_positions(
            ids,
            furthest_stage_of=lambda i: stage_map.get(f"mh_{int(i)}"),
            applied_count_of=lambda i: applied_map.get(f"mh_{int(i)}", 0),
            pay_key_of=lambda i: pay_key_masshiring(rows_by_id.get(int(i)) or {}, hourly_pay=hourly_pay),
            k=(mh_candidates_per_position() if k is None else k),
            per_position_cap=rounds, order=order, velocity_guard=None)
    except Exception as exc:
        logger.info("[offer_priority] plan_mh_batch fell back to ids*rounds: %s", exc)
        return fallback
