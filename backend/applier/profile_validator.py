"""Profile reality gate.

The engine's trust model is a real person reviewing and submitting their own
applications. A profile whose contact data cannot receive a recruiter response
(reserved-fictional phone, placeholder email) must never spend prefill slots or
reach a Submit affordance — applications from it are undeliverable by
construction. Both the batch queue (applier/batch.py) and the dashboard
(dashboard_app.py) consult this gate.
"""
import re

# NANP reserves 555-0100..555-0199 for fiction — no real line can have it.
_FICTIONAL_PHONE = re.compile(r"555[\s.\-]?01\d\d")
_PLACEHOLDER_EMAIL = re.compile(
    r"(?i)@(example\.(com|org|net)|example$|test\.example|invalid$|sample\.)")


def validate_profile(profile: dict) -> list[str]:
    """Return a list of blocking problems; empty list means submittable."""
    problems: list[str] = []
    name = (profile.get("full_name") or "").strip()
    phone = (profile.get("phone") or "").strip()
    email = (profile.get("email") or "").strip()

    if not name:
        problems.append("full_name is empty")
    if not phone:
        problems.append("phone is empty — recruiters cannot call back")
    elif _FICTIONAL_PHONE.search(phone):
        problems.append(
            f"phone {phone!r} is in the reserved-fictional 555-01xx block "
            "— unreachable by construction")
    if not email:
        problems.append("email is empty")
    elif _PLACEHOLDER_EMAIL.search(email):
        problems.append(f"email {email!r} is a placeholder domain")
    return problems


def is_submittable(profile: dict) -> bool:
    return not validate_profile(profile)
