"""A candidate's applications — what the bot pre-filled / submitted per job — read from
uploads/prefill/<candidate_id>/<jobid>/report.json. Powers the Кандидаты screen:
where the candidate applied + the résumé PDF the bot used (downloadable).
"""
import json
from pathlib import Path

PREFILL_ROOT = Path(__file__).resolve().parents[2] / "uploads" / "prefill"


def _safe(cid: str) -> str:
    return "".join(ch for ch in str(cid or "") if ch.isalnum() or ch in "-_.")


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
