"""A candidate's applications — what the bot pre-filled / submitted per job — read from
uploads/prefill/<candidate_id>/<jobid>/report.json. Powers the Кандидаты screen:
where the candidate applied + the résumé PDF the bot used (downloadable).
"""
import json
from pathlib import Path

PREFILL_ROOT = Path(__file__).resolve().parents[2] / "uploads" / "prefill"
_PROFILES = Path(__file__).resolve().parents[1] / "data" / "profiles.json"
_DEMO_FILE = Path(__file__).resolve().parents[1] / "data" / "demo_personas.json"
# mtime-guarded cache of profiles.json keyed by id (loaded once, refreshed on change).
_prof_cache: dict = {"mtime": -1.0, "by_id": {}, "resume_ids": set(), "email_to_id": {}}
# mtime-guarded cache of the synthetic demo registry (email -> id).
_demo_cache: dict = {"mtime": -1.0, "by_email": {}}


def _safe(cid: str) -> str:
    return "".join(ch for ch in str(cid or "") if ch.isalnum() or ch in "-_.")


def _render_worthy(resume) -> bool:
    return bool(isinstance(resume, dict) and
                (resume.get("experience") or resume.get("education") or resume.get("summary")))


def _profiles_by_id() -> dict:
    """profiles.json as {id: profile}, cached until the file changes."""
    try:
        mt = _PROFILES.stat().st_mtime
    except OSError:
        return {}
    if mt != _prof_cache["mtime"]:
        by_id: dict = {}
        try:
            d = json.loads(_PROFILES.read_text(encoding="utf-8"))
            if isinstance(d, list):
                by_id = {str(p.get("id")): p for p in d if isinstance(p, dict) and p.get("id")}
            elif isinstance(d, dict):
                by_id = {str(k): v for k, v in d.items() if isinstance(v, dict)}
        except Exception:
            by_id = {}
        _prof_cache.update(mtime=mt, by_id=by_id,
                           resume_ids={cid for cid, p in by_id.items()
                                       if _render_worthy((p or {}).get("resume"))},
                           email_to_id={str((p or {}).get("email") or "").lower(): cid
                                        for cid, p in by_id.items() if (p or {}).get("email")})
    return _prof_cache["by_id"]


def _demo_email_to_id() -> dict:
    """The synthetic demo registry (backend/data/demo_personas.json, {email: {id,name}})
    as {email: id}, cached until the file changes. Lets the candidate-apps chip resolve a
    demo persona's mailbox → its uploads/prefill/<id> dir without scanning every persona."""
    try:
        mt = _DEMO_FILE.stat().st_mtime
    except OSError:
        return {}
    if mt != _demo_cache["mtime"]:
        by_email: dict = {}
        try:
            d = json.loads(_DEMO_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                for email, info in d.items():
                    cid = (info or {}).get("id") if isinstance(info, dict) else None
                    if email and cid:
                        by_email[str(email).lower()] = str(cid)
        except Exception:
            by_email = {}
        _demo_cache.update(mtime=mt, by_email=by_email)
    return _demo_cache["by_email"]


def id_for_email(email: str) -> str | None:
    """Map a candidate mailbox -> candidate id (the uploads/prefill/<id> dir + /candidates/<id>
    page), for the résumé/applications chip. Roster (profiles.json email) first, then the
    synthetic demo registry. None if unknown. Both lookups are cheap cached dicts."""
    e = (email or "").strip().lower()
    if not e:
        return None
    _profiles_by_id()  # warm the roster cache (builds email_to_id)
    cid = _prof_cache["email_to_id"].get(e)
    if cid:
        return cid
    return _demo_email_to_id().get(e) or None


def resume_profile_ids() -> set:
    """Candidate ids (from profiles.json) that carry a render-worthy base résumé — used to
    show the résumé chip for roster candidates that have no per-application PDF yet. Cheap
    (cached), so it can be consulted once per candidate-list render."""
    _profiles_by_id()
    return _prof_cache["resume_ids"]


def base_resume(cid: str) -> dict | None:
    """The candidate's base résumé dict — from profiles.json (roster candidates), else the
    newest prefill persona's résumé (a demo persona not in profiles.json). None if neither
    has render-worthy résumé data."""
    cid = _safe(cid)
    prof = _profiles_by_id().get(cid)
    r = (prof or {}).get("resume")
    if _render_worthy(r):
        return r
    d = PREFILL_ROOT / cid
    if d.is_dir():
        for pj in sorted(d.glob("*/persona.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                rr = ((json.loads(pj.read_text(encoding="utf-8")).get("profile") or {})
                      .get("resume") or {})
            except Exception:
                continue
            if _render_worthy(rr):
                return rr
    return None


def app_count(cid: str) -> int:
    """Cheap count of a candidate's applications (job dirs with a report.json)."""
    d = PREFILL_ROOT / _safe(cid)
    if not d.is_dir():
        return 0
    n = 0
    for j in d.iterdir():
        try:
            if j.is_dir() and (j / "report.json").exists():
                n += 1
        except OSError:
            pass
    return n


def _submitted_map(cid: str) -> dict:
    """uploads/prefill/<cid>/status.json → {jobid: {status, ts}} (empty if absent)."""
    try:
        m = json.loads((PREFILL_ROOT / cid / "status.json").read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def applications_for(cid: str) -> list[dict]:
    """[{jobid, company, title, apply_url, ts, has_resume, submitted}], newest first."""
    cid = _safe(cid)
    d = PREFILL_ROOT / cid
    if not d.is_dir():
        return []
    status = _submitted_map(cid)
    out: list[dict] = []
    for j in d.iterdir():
        rep = j / "report.json"
        try:
            if not (j.is_dir() and rep.exists()):
                continue
            r = json.loads(rep.read_text(encoding="utf-8"))
        except Exception:
            r = {}
        jobid = j.name
        try:
            ts = rep.stat().st_mtime
        except OSError:
            ts = 0.0
        out.append({
            "jobid": jobid,
            "company": r.get("company") or "",
            "title": r.get("job_title") or r.get("title") or "",
            "apply_url": r.get("apply_url") or "",
            "ts": ts,
            "has_resume": (j / "resume.pdf").exists(),
            "submitted": (status.get(jobid) or {}).get("status") == "submitted",
        })
    out.sort(key=lambda a: a["ts"], reverse=True)
    return out
