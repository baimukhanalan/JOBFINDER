"""Build the Chrome extension's local files from a generated candidate.

    python -m backend.tools.build_extension            # fresh KZ candidate
    python -m backend.tools.build_extension --profile gen_kz_50_nurlan_karimov
    python -m backend.tools.build_extension --country ph --seed 7

Writes (both gitignored — they hold identity + the live token):
  extension/profile.js     — window.APPLY_PROFILE + APPLY_ANSWERS (offline local fill)
  extension/background.js   — service worker with the real X-Assist-Token + local server

Then in Chrome: chrome://extensions → Developer mode → Load unpacked → pick extension/.
Set the popup's "Profile ID" to the printed id so server "Smart fill" matches.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.profiles.store import PROJECT_ROOT, Profile
from backend.tools.gen_profiles import generate
from backend.tools.salmon_autofill import _upsert_profile

EXT = PROJECT_ROOT / "extension"
TOKEN_FILE = PROJECT_ROOT / "backend" / ".assist_token"
LOCAL_SERVER = "http://127.0.0.1:8089"


def _profile_from_store(pid: str) -> tuple[dict, dict]:
    import backend.profiles.facts as facts_lib
    from backend.profiles.store import load_profiles
    profs = load_profiles()
    if pid not in profs:
        raise SystemExit(f"profile {pid!r} not in profiles.json (have: {sorted(profs)[:6]}…)")
    p = profs[pid]
    return json.loads(json.dumps(p.__dict__, default=str)), facts_lib.load_facts(pid)


def _apply_profile_js(p: Profile, facts: dict) -> dict:
    r = p.resume or {}
    first, _, last = (p.full_name or "").partition(" ")
    digits = "".join(ch for ch in (p.phone or "") if ch.isdigit())
    langs = ", ".join(facts.get("languages", []) or [])
    telegram = "@none"  # per request: Telegram is @none everywhere
    return {
        "first_name": first, "last_name": last, "full_name": p.full_name,
        "email": p.email, "phone": p.phone, "phone_digits": digits,
        "telegram": telegram, "english_level": "Confident B2",
        "address": p.location, "city": p.city, "state": p.state or "",
        "state_full": p.state or "", "zip": p.zip_code, "country": p.country,
        "country_code": "KZ" if p.country == "Kazakhstan" else ("PH" if p.country == "Philippines" else ""),
        "linkedin": "", "website": "",  # per request: leave LinkedIn blank
        "years_experience": str(p.years_experience),
        "education_level": facts.get("education_level", "Bachelor's degree"),
        "school": (r.get("education", [{}])[0] or {}).get("school", ""),
        "desired_salary": p.desired_salary or "$5,000 USD/month",
        "available_start": p.available_start, "notice_period": facts.get("notice_period", "Immediately"),
        "timezone": facts.get("timezone", ""),
        "languages": langs,
        "work_authorized_us": "Yes", "needs_sponsorship": p.needs_sponsorship or "No",
        "over_18": "Yes", "background_check_consent": facts.get("background_check_ok", "Yes"),
        "criminal_record": facts.get("criminal_record", "No"),
        "willing_relocate": "Yes", "remote_ok": "Yes",
        "has_equipment": facts.get("equipment_ok", "Yes"),
        "gender": "Decline to self-identify", "race": "Decline to self-identify",
        "veteran": "I am not a protected veteran",
        "disability": "I do not have a disability", "hispanic": "No",
        "source": facts.get("referral", "Company careers site"),
    }


# [regex source, flags, answer] — first match wins in content.js
def _answers(p: Profile) -> list[list[str]]:
    r = p.resume or {}
    summ = (r.get("summary") or "").strip()
    role = (r.get("headline") or "Customer Support Specialist")
    yrs = p.years_experience
    cover = (
        f"I am excited to apply for this role. With {yrs} years in customer support and "
        f"financial services, I bring strong communication, issue resolution, and CRM "
        f"experience across email, chat, and phone. {summ} I would welcome the chance to "
        f"contribute to your team and support your customers with care and precision.")
    behavioral_difficult = (
        "A customer once contacted us upset about a billing error and threatening to leave. "
        "I let them explain fully, acknowledged the frustration, and took ownership. I found "
        "the issue, corrected the charge while they were still on the line, and followed up to "
        "confirm it was resolved. They stayed and thanked me. My approach: listen, own it, fix "
        "it fast, follow up.")
    great_cs = (
        "Great customer service means resolving the issue efficiently, communicating clearly, "
        "and leaving every interaction better than it started — owning the problem so the "
        "customer never has to chase you, with patience and respect even under pressure.")
    strengths = (
        f"My strengths are empathy under pressure, fast and accurate communication, and hands-on "
        f"experience with CRMs and help-desk tools. I ramp quickly on new products and I genuinely "
        f"enjoy frontline support work.")
    remote = (
        "I have a reliable home setup with fast internet and a quiet, professional environment. "
        "I am self-disciplined, responsive on chat and email, and used to managing my own queue.")
    salary = f"{p.desired_salary or '$5,000 USD/month'} — open to discussion based on the full role."
    return [
        [r"cover ?letter|why (are|do) you (interested|want|applying)|why (this|our) (role|company|position|team)|tell us why|what (interests|excites) you|motivat", "i", cover],
        [r"(describe|tell us about|give an example|share|a time).*(difficult|upset|angry|frustrat|irate|challenging) (customer|client|situation|interaction)|de-?escalat", "i", behavioral_difficult],
        [r"(what|how).*(great|excellent|good|quality) (customer (service|experience|support))|define (customer|great|excellent)", "i", great_cs],
        [r"(your )?(greatest )?(strength|skills|qualif|why are you|what makes you|good fit|right (candidate|fit)|bring to)", "i", strengths],
        [r"(describe|tell us about).*(remote|work from home|wfh)|experience.*remot|comfortable.*remot", "i", remote],
        [r"(salary|compensation|pay).*(expect|require|desired|range)|expected (salary|pay|comp)", "i", salary],
        [r"where.*(hear|find|learn).*(about|of)|how did you (hear|find|learn)|source|referr", "i", "Through the company careers page."],
        [r"(anything else|additional (information|comments)|is there anything|other.*(info|comments))", "i", "Thank you for considering my application — I would welcome the chance to discuss the role."],
    ]


def _render_profile_js(prof_obj: dict, answers: list[list[str]]) -> str:
    prof_lines = ",\n".join(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}"
                            for k, v in prof_obj.items())
    ans_lines = ",\n".join(
        f"  [/{src}/{flags}, {json.dumps(ans, ensure_ascii=False)}]"
        for src, flags, ans in answers)
    return (
        "// Apply Assist — baked profile + answer bank (generated by build_extension.py).\n"
        "// Self-contained: local fill needs no server. Regenerate to swap the candidate.\n"
        "window.APPLY_PROFILE = {\n" + prof_lines + ",\n};\n\n"
        "window.APPLY_ANSWERS = [\n" + ans_lines + ",\n];\n")


def _render_background_js(token: str) -> str:
    src = (EXT / "background.example.js").read_text(encoding="utf-8")
    src = src.replace('const DEFAULT_SERVER = "https://jobfinder.systeam.kz";',
                      f'const DEFAULT_SERVER = "{LOCAL_SERVER}";')
    src = src.replace('const ASSIST_TOKEN = "REPLACE_WITH_BACKEND_ASSIST_TOKEN";',
                      f'const ASSIST_TOKEN = {json.dumps(token)};')
    return src


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", help="existing profiles.json id (default: generate a new one)")
    ap.add_argument("--country", default="kz", choices=["kz", "ph"])
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()

    if not TOKEN_FILE.exists():
        raise SystemExit("backend/.assist_token missing — run the dashboard once or create it")
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()

    if a.profile:
        prof_d, facts = _profile_from_store(a.profile)
    else:
        prof_d, facts = generate(1, seed=a.seed, use_llm=True, country=a.country)
        _upsert_profile(prof_d, facts)  # so server Smart-fill can match this id
    profile = Profile.from_dict(prof_d)

    (EXT / "profile.js").write_text(
        _render_profile_js(_apply_profile_js(profile, facts), _answers(profile)),
        encoding="utf-8")
    (EXT / "background.js").write_text(_render_background_js(token), encoding="utf-8")

    print(f"Built extension for: {profile.full_name} ({profile.country})")
    print(f"  id            : {profile.id}")
    print(f"  salary        : {profile.desired_salary}")
    print(f"  server        : {LOCAL_SERVER}  (token wired)")
    print(f"  files         : extension/profile.js, extension/background.js")
    print("\nLoad in Chrome: chrome://extensions → Developer mode → Load unpacked → "
          f"{EXT}\nThen set the popup's Profile ID to  {profile.id}  for server Smart-fill.")


if __name__ == "__main__":
    main()
