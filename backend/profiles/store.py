"""Multi-profile store: load N real people's data + per-platform session paths.

Each person is one Profile. The auto-apply engine looks up a profile by id, uses
its identity fields to fill forms, its structured `resume` to tailor a résumé, and
its per-tenant `storage_state` file to reuse a logged-in session (injected cookies).

Real data lives in backend/data/profiles.json (gitignored). A committed
profiles.example.json holds a clearly-fake sample so the pipeline runs end-to-end
without anyone's real data.
"""
import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "backend" / "data"
REAL_PROFILES = DATA_DIR / "profiles.json"
EXAMPLE_PROFILES = DATA_DIR / "profiles.example.json"
SESSIONS_DIR = PROJECT_ROOT / "data_sessions"  # storage_state per (profile, tenant)


@dataclass
class Profile:
    id: str
    full_name: str
    email: str
    phone: str
    location: str = "Remote, US"
    city: str = ""
    state: str = ""
    zip_code: str = ""
    street_address: str = ""       # street line — real onboarded people + synth demo personas
    country: str = "United States"
    linkedin_url: str = ""
    work_authorization: str = "US Citizen"
    needs_sponsorship: str = "No"
    years_experience: str = "5"
    desired_salary: str = ""
    available_start: str = "Immediately"
    resume_path: str = ""          # last rendered résumé file (set by the runner)
    resume: dict = field(default_factory=dict)  # structured base résumé (real facts)
    mailbox: str = ""              # application-mail address indexed by inbox_index.py
    is_sample: bool = False
    is_synthetic: bool = False     # a demo persona invented by synth_persona (never a real roster person)
    sex: str = ""                  # the persona's assigned sex (male/female) — for coherent gender/
    #                                pronoun answers on demographic fields; "" = unknown/decline

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"profile {d.get('id')!r}: unknown keys {sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_form_dict(self) -> dict:
        """Identity fields in the shape backend.applier.analyzer expects."""
        return {
            "full_name": self.full_name,
            "email": self.email,
            "phone": self.phone,
            "resume_path": self.resume_path,
            "location": self.location,
            "city": self.city,
            "state": self.state,
            "zip_code": self.zip_code,
            "street_address": self.street_address,
            "country": self.country,
            "linkedin_url": self.linkedin_url,
            "years_experience": self.years_experience,
            "desired_salary": self.desired_salary,
            "work_authorization": self.work_authorization,
            "needs_sponsorship": self.needs_sponsorship,
            "available_start": self.available_start,
            "sex": self.sex,
        }

    def storage_state_path(self, tenant: str) -> str:
        safe = "".join(c for c in (tenant or "").lower() if c.isalnum() or c in "-_") or "default"
        return str(SESSIONS_DIR / self.id / f"{safe}.json")


def _source_path(path: str | Path | None) -> Path:
    if path:
        return Path(path)
    return REAL_PROFILES if REAL_PROFILES.exists() else EXAMPLE_PROFILES


def load_profiles(path: str | Path | None = None) -> dict[str, Profile]:
    src = _source_path(path)
    raw = json.loads(src.read_text(encoding="utf-8"))
    out: dict[str, Profile] = {}
    for entry in raw:
        prof = Profile.from_dict(entry)
        if prof.id in out:
            raise ValueError(f"duplicate profile id: {prof.id}")
        out[prof.id] = prof
    return out


def is_sample_profile(profile: Profile) -> bool:
    """True for the committed fake profile — real applications must never go out
    under it (fake name/email burns the posting)."""
    return bool(getattr(profile, "is_sample", False)) or getattr(profile, "id", "") == "sample"


def get_profile(profile_id: str, path: str | Path | None = None) -> Profile:
    profs = load_profiles(path)
    if profile_id not in profs:
        raise KeyError(f"profile not found: {profile_id!r} (have: {sorted(profs)})")
    return profs[profile_id]
