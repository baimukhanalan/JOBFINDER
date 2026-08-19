"""List the CS/support roles that are strictly REMOTE (no hybrid) — the same set
the bot auto-applies to, so the extension flow targets the same positions.

    python -m backend.tools.online_roles          # print roles + apply URLs
    python -m backend.tools.online_roles --url    # URLs only (one per line)

Open these in your Chrome and run the extension on them; onsite roles are excluded.
"""
from __future__ import annotations

import argparse

import httpx

from backend.tools.roles_dashboard import _is_remote, _workplace
from backend.tools.salmon_autofill import ASHBY_BOARD, _CS_KEYWORDS


def online_cs_roles() -> list[dict]:
    jobs = httpx.get(ASHBY_BOARD, timeout=30).json().get("jobs", [])
    out = [j for j in jobs
           if any(k in j.get("title", "").lower() for k in _CS_KEYWORDS) and _is_remote(j)]
    out.sort(key=lambda j: j.get("title", ""))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", action="store_true", help="print only apply URLs")
    a = ap.parse_args()
    roles = online_cs_roles()
    if a.url:
        for j in roles:
            print(j.get("applyUrl", ""))
        return
    print(f"{len(roles)} CS + remote roles (open these in Chrome for the extension):\n")
    for j in roles:
        print(f"  [{_workplace(j):7}] {j.get('title', '')[:42]:42} {j.get('applyUrl', '')}")


if __name__ == "__main__":
    main()
