"""Extra mass-hiring connectors (recon 2026-09-23) — Chewy (US holiday CS ramp) + a batch of
Canadian fintech / tech customer-service employers (KOHO, Neo Financial, Wealthsimple, Hootsuite,
Coveo, Clearco, Float).

These live in a SEPARATE module so `mass_hiring.py`'s connector list + registration stays owned by
the maintainer — this file only IMPORTS mass_hiring's reusable readers and adds thin, per-employer
wrappers. To go live the maintainer wires each `fetch_*` into `mass_hiring._SOURCES` (+ the CA branch
handling is automatic — the rows force `us_eligible=True`) and adds the `_AUTO_STATUS` entries.

Reused readers (all keyless):
  · Workday   `_fetch_workday` / `_workday_row`           (Chewy)
  · Ashby     `_fetch_ashby`   / `_ashby_row`             (KOHO, Neo, Wealthsimple, Clearco, Float)
  · Greenhouse`_fetch_greenhouse`/`_greenhouse_row`       (Hootsuite)
  · SmartRec. `_fetch_smartrecruiters`/`_smartrecruiters_row` (Coveo)

The two HARD RULES are enforced inside those readers: `_is_remote` REMOTE + `categorize()` ENTRY
bucket. CANADA employers pass `ca=True` (Ashby/Greenhouse) / `country="ca", force_eligible=True`
(SmartRecruiters): the row is kept only when its location is `_is_ca_remote`, and `us_eligible` is
FORCED True so `collect(us_only=True)` keeps it on the North-America board (there is no CA column —
the location_raw carries the Canada signal for persona nationality), exactly like
`mass_hiring._cnx_ca_row` / `fetch_instacart_canada`.

All COLLECT-FIRST → `auto_status='needs_laptop'` (none has a wired mass-hiring apply strategy yet;
Coveo's SmartRecruiters host IS drivable by the shared SR cron, but stays cosmetic 'needs_laptop'
pending a per-tenant verify, like Wayfair).
"""
from __future__ import annotations

from backend.tools.mass_hiring import (
    _fetch_workday, _workday_row,
    _fetch_ashby, _ashby_row,
    _fetch_greenhouse, _greenhouse_row,
    _fetch_smartrecruiters, _smartrecruiters_row,
)

# ================================================================================================
# Chewy — US pet retailer, HUGE seasonal remote "Customer Care Representative - Work from Home" ramp
# (Oct-Jan). ATS = Workday `chewy.wd5.myworkdayjobs.com/chewy/External` (~285 reqs; careers.chewy.com
# is a Phenom front over this board). Chewy's remote CSR reqs encode the state in the externalPath
# slug (…/Texas-Work-from-Home/…) and/or the TITLE ("Work from Home in Texas") while locationsText is
# "USA - TX - Virtual …" — so read remote from the title (`title_remote`) and confirm US from the path
# when the location is bare (`us_from_path`), same as the Everise Workday lane.
# LIVE-VERIFIED 2026-09-23: 285 reqs → ~2 remote-US ENTRY CSR today (Customer Care Rep WFH in TX + KY);
# the board is otherwise corporate/warehouse. Future-proof: Chewy hires thousands of seasonal WFH
# Customer Care Reps at peak, all captured automatically by categorize().
# Apply would reuse the Workday create-account lane after a per-tenant verify → 'needs_laptop'.
# ------------------------------------------------------------------------------------------------
_CHEWY_HOST = "chewy.wd5.myworkdayjobs.com"
_CHEWY_TENANT = "chewy"
_CHEWY_SITE = "External"


def _chewy_row(j: dict) -> dict | None:
    """PURE (network-free): one Chewy Workday jobPosting → a remote-US mass-hiring row or None."""
    return _workday_row(j, "chewy", "Chewy", _CHEWY_HOST, _CHEWY_SITE,
                        title_remote=True, us_from_path=True)


def fetch_chewy() -> list[dict]:
    return _fetch_workday("chewy", "Chewy", _CHEWY_HOST, _CHEWY_TENANT, _CHEWY_SITE,
                          search_texts=("", "remote"), title_remote=True, us_from_path=True,
                          offset_cap=300)


# ================================================================================================
# Canadian fintech / tech customer-service employers on keyless Ashby boards. All CA-forced
# (ca=True → keep CA-remote rows, force us_eligible so they persist on the NA board). Live yields are
# THIN today (these firms post mostly senior/manager or French-titled CS roles right now), but they
# run bilingual EN/FR remote-CSR ramps seasonally → collect-first captures them the moment they open.
# ------------------------------------------------------------------------------------------------
def _koho_row(j: dict) -> dict | None:
    """PURE: one KOHO Ashby posting → a CA-remote mass-hiring row or None."""
    return _ashby_row(j, "koho", "KOHO", ca=True)


def fetch_koho() -> list[dict]:
    # Ashby org `koho` (~8 reqs). Canadian challenger-bank fintech. LIVE 2026-09-23: 0 remote entry
    # CSR today (all product/eng/senior). Future-proof.
    return _fetch_ashby("koho", "KOHO", "koho", ca=True)


def _neo_row(j: dict) -> dict | None:
    """PURE: one Neo Financial Ashby posting → a CA-remote mass-hiring row or None."""
    return _ashby_row(j, "neo", "Neo Financial", ca=True)


def fetch_neo() -> list[dict]:
    # Ashby org `neofinancial` (~82 reqs). Canadian fintech (cards/banking). LIVE 2026-09-23: 0 remote
    # entry CSR today (heavily eng/sales/senior; its high-volume "Advisor" roles are Calgary on-site).
    # Future-proof — Neo ramps remote support seasonally.
    return _fetch_ashby("neo", "Neo Financial", "neofinancial", ca=True)


def _wealthsimple_row(j: dict) -> dict | None:
    """PURE: one Wealthsimple Ashby posting → a CA-remote mass-hiring row or None."""
    return _ashby_row(j, "wealthsimple", "Wealthsimple", ca=True)


def fetch_wealthsimple() -> list[dict]:
    # Ashby org `wealthsimple` (~44 reqs). Canadian robo-advisor/brokerage fintech. LIVE 2026-09-23:
    # 0 remote entry CSR today via categorize() (its remote-CA "Succès Client" associate roles are
    # French-titled + the English CS roles are Senior/Manager). Future-proof — big seasonal Client
    # Success ramps (Remote Canada).
    return _fetch_ashby("wealthsimple", "Wealthsimple", "wealthsimple", ca=True)


def _clearco_row(j: dict) -> dict | None:
    """PURE: one Clearco Ashby posting → a CA-remote mass-hiring row or None."""
    return _ashby_row(j, "clearco", "Clearco", ca=True)


def fetch_clearco() -> list[dict]:
    # Ashby org `clearco` (~3 reqs). Toronto-founded fintech (capital for e-commerce). LIVE 2026-09-23:
    # 0 remote entry CSR today (a BDR posted at "Canada" but not remote-flagged). Future-proof.
    return _fetch_ashby("clearco", "Clearco", "clearco", ca=True)


def _float_row(j: dict) -> dict | None:
    """PURE: one Float Ashby posting → a CA-remote mass-hiring row or None."""
    return _ashby_row(j, "float", "Float", ca=True)


def fetch_float() -> list[dict]:
    # Ashby org `float` (~19 reqs). Canadian corporate-spend fintech (Float Financial). LIVE
    # 2026-09-23: 0 remote entry CSR today (its Customer Success/Activation roles are Toronto HYBRID
    # + Manager-level). Future-proof.
    return _fetch_ashby("float", "Float", "float", ca=True)


# ================================================================================================
# Hootsuite — Canadian social-media SaaS (Vancouver), Greenhouse board `hootsuite` (~13 reqs).
# CA-forced. LIVE 2026-09-23: 0 remote entry CSR today (its Customer Success / support roles are
# Manager-level or off-shore, correctly dropped by _NOT_MASS). Future-proof — Hootsuite runs remote
# Billing/Collections + Scale support hiring. Collect-first → 'needs_laptop'.
# ------------------------------------------------------------------------------------------------
def _hootsuite_row(j: dict) -> dict | None:
    """PURE: one Hootsuite Greenhouse board job → a CA-remote mass-hiring row or None."""
    return _greenhouse_row(j, "hootsuite", "Hootsuite", ca=True)


def fetch_hootsuite() -> list[dict]:
    return _fetch_greenhouse("hootsuite", "Hootsuite", "hootsuite", ca=True)


# ================================================================================================
# Coveo — Canadian AI-search SaaS (Quebec City), SmartRecruiters (careers.smartrecruiters.com/Coveo
# confirmed via a 301 from jobs.smartrecruiters.com/Coveo). CA-forced, reuses the proven SR reader.
# HONEST 2026-09-23: the keyless v1 postings API for slug "Coveo" returns 200 total=0 today (either 0
# externally-listed CA-remote CSR, or the public feed differs from the SR-hosted careers SPA) → 0 rows
# today. The SR apply cron keys on the jobs.smartrecruiters.com apply_url host, so any future CA-remote
# CSR row IS auto-drivable — cosmetic 'needs_laptop' pending a per-tenant verify (like Wayfair).
# CAVEAT for wiring: verify the SR company slug ("Coveo") once it lists remote CSR — a 0 return may be a
# vanity-slug/company-id mismatch rather than an empty board.
# ------------------------------------------------------------------------------------------------
def _coveo_row(j: dict) -> dict | None:
    """PURE: one Coveo SmartRecruiters posting → a CA-remote mass-hiring row or None."""
    return _smartrecruiters_row(j, "coveo", "Coveo", country="ca", force_eligible=True)


def fetch_coveo() -> list[dict]:
    return _fetch_smartrecruiters("coveo", "Coveo", country="ca", force_eligible=True)


# ================================================================================================
# NOT ADDABLE this pass:
#   · Lightspeed Commerce (www.lightspeedhq.com/careers/openings/) — a JS SPA that loads its jobs from
#     a client-side endpoint with NO greenhouse/workday/ashby/lever/smartrecruiters reference in the
#     rendered HTML (checked via httpx AND curl_cffi Chrome impersonation); the keyless-ATS slugs
#     lightspeed*/lightspeedhq/lightspeedcommerce all 404, and ashby `lightspeed` is a DIFFERENT
#     (robotics/construction) company. Needs JS execution / a headful capture (out of scope here).
#   · Shopify — no keyless board (greenhouse/lever/ashby/SR slugs all 404); careers on a custom stack.
# ================================================================================================
