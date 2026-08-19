"""Lightweight apply API: expose the target roster and configured profiles.

The heavy pre-fill (Playwright) runs via the CLI / a worker, not inline in a request.
"""
from fastapi import APIRouter

from backend.data import roster
from backend.profiles.store import load_profiles

apply_router = APIRouter()


@apply_router.get("/roster")
async def get_roster():
    return {"tiers": roster.ROSTER, "avoid": roster.AVOID}


@apply_router.get("/profiles")
async def get_profiles():
    return [
        {"id": p.id, "full_name": p.full_name, "is_sample": p.is_sample,
         "work_authorization": p.work_authorization}
        for p in load_profiles().values()
    ]
