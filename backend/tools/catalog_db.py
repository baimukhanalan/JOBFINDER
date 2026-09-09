"""Postgres `job_catalog` — a persisted catalog of remote jobs across every known
ATS board: title, description, and (greenhouse) application-form questions.

Lives in the SAME isolated `jobfinder_crm` DB as the mail index (CRM_PG_DSN). Written
by catalog_collector.py (threaded scrape), read by the /catalog dashboard tab. Sync
psycopg2 so the collector, cron and the web app can all share it.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2.extras import Json

_ENV = Path(__file__).resolve().parents[1] / ".env"


def _dsn() -> str:
    dsn = os.environ.get("CRM_PG_DSN")
    if dsn:
        return dsn
    try:
        for line in _ENV.read_text().splitlines():
            if line.strip().startswith("CRM_PG_DSN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    raise RuntimeError("CRM_PG_DSN not set (backend/.env or environment)")


_pool = None
_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=_dsn())
    return _pool


@contextmanager
def conn():
    p = _get_pool()
    c = p.getconn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        p.putconn(c)


@contextmanager
def _cur(dict_rows: bool = True):
    with conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor if dict_rows else None)
        try:
            yield cur
        finally:
            cur.close()


# Columns added after the CREATE TABLE, with their DDL types. ensure_schema adds only the ones
# the live table LACKS (checked via information_schema — a plain SELECT), because
# `ADD COLUMN IF NOT EXISTS` takes the table's ACCESS EXCLUSIVE lock even when the column
# already exists — and the nightly collector would then queue every /catalog reader behind a
# no-op DDL if any session held the table (the 2026-09-07..09 mass_hiring_jobs outage pattern).
_EXTRA_COLS = (
    ("regions", "TEXT[]"), ("region_source", "TEXT"),
    # role_category: a functional bucket derived from the title (+ department),
    # classified deterministically at collect time (applier/role_category.py),
    # like regions. Powers the /stats "По ролям" cut. comp_min/comp_max: the
    # posted pay range annualized to USD ints (applier/comp_extract.py) — the
    # range the posting states (mostly base, per US pay-transparency law), NOT a
    # fabricated total comp. *_source ∈ {rule, llm, agent, unknown}.
    ("role_category", "TEXT"), ("role_source", "TEXT"),
    ("comp_min", "INT"), ("comp_max", "INT"), ("comp_currency", "TEXT"), ("comp_source", "TEXT"),
    # est_*: a RESEARCHED APPROXIMATE comp for EVERY job (posted comp exists on only
    # ~48% of rows). est_base_* = estimated base-salary range; est_total_* = estimated
    # TOTAL compensation (base + bonus + equity); both annualized USD ints. Researched
    # per company×role×region combo (a market estimate, NOT the posting's stated pay),
    # so it is kept DISTINCT from the posted comp_* — /stats' posted median stays honest.
    # est_comp_source ∈ {research, rule, none}.
    ("est_base_min", "INT"), ("est_base_max", "INT"), ("est_total_min", "INT"),
    ("est_total_max", "INT"), ("est_comp_currency", "TEXT"), ("est_comp_source", "TEXT"),
    # draft: the full pre-generated application fill-packet (tailored résumé dict +
    # every question answered, per catalog_drafts.py). Reviewed on the /drafts page.
    ("draft", "JSONB"), ("draft_at", "TIMESTAMPTZ"),
    # dead: a posting confirmed gone at the source (e.g. greenhouse job id 404s).
    # Kept as a reversible blacklist marker (not deleted) — hidden from the catalog
    # browse + the draft work-list so a human never opens an apply page that 404s.
    ("dead", "BOOLEAN DEFAULT FALSE"), ("dead_reason", "TEXT"),
    # open_anywhere: deterministic (applier/regions.open_anywhere) — the location names NO specific
    # country/city (a bare "Remote", "worldwide", a broad region incl. Central Asia) or the text says
    # "work from anywhere". Finer than the OTHER region code, which also covers postings PINNED to
    # one foreign country ("Remote - India"); a country query (Казахстан) requires it. Recomputed on
    # every collect (new-first) + one-shot `catalog_collector --backfill-open`.
    ("open_anywhere", "BOOLEAN"),
)


def _existing_columns(cur, table: str) -> set[str]:
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name=%s", (table,))
    return {r[0] for r in cur.fetchall()}


def ensure_schema() -> None:
    with _cur(False) as cur:
        # A blocked DDL fails fast instead of holding the whole catalog hostage for hours.
        cur.execute("SET LOCAL lock_timeout = '15s'")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS job_catalog (
          id            BIGSERIAL PRIMARY KEY,
          ats           TEXT NOT NULL,
          company_key   TEXT,
          company       TEXT,
          external_id   TEXT NOT NULL,
          title         TEXT,
          location      TEXT,
          department    TEXT,
          workplace     TEXT,
          is_remote     BOOLEAN DEFAULT FALSE,
          url           TEXT,
          description   TEXT,
          description_html TEXT,
          questions     JSONB,
          q_count       INT DEFAULT 0,
          first_seen    TIMESTAMPTZ DEFAULT now(),
          last_seen     TIMESTAMPTZ DEFAULT now(),
          UNIQUE (ats, company_key, external_id)
        );""")
        cur.execute("CREATE INDEX IF NOT EXISTS jc_remote ON job_catalog (is_remote, company_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS jc_company ON job_catalog (company_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS jc_fts ON job_catalog USING gin "
                    "(to_tsvector('simple', coalesce(title,'')||' '||coalesce(company,'')"
                    "||' '||coalesce(description,'')));")
        have = _existing_columns(cur, "job_catalog")
        for col, decl in _EXTRA_COLS:
            if col not in have:
                cur.execute(f"ALTER TABLE job_catalog ADD COLUMN IF NOT EXISTS {col} {decl}")
        cur.execute("CREATE INDEX IF NOT EXISTS jc_regions ON job_catalog USING GIN (regions)")
        cur.execute("CREATE INDEX IF NOT EXISTS jc_role ON job_catalog (role_category)")


_UP_COLS = ("ats", "company_key", "company", "external_id", "title", "location",
            "department", "workplace", "is_remote", "url", "description",
            "description_html", "questions", "q_count", "regions", "region_source",
            "role_category", "role_source", "comp_min", "comp_max", "comp_currency",
            "comp_source", "est_base_min", "est_base_max", "est_total_min",
            "est_total_max", "est_comp_currency", "est_comp_source", "open_anywhere")
_QI = _UP_COLS.index("questions")


def upsert_jobs(rows: list[dict]) -> int:
    """Batch insert/update by (ats, company_key, external_id). Questions and q_count
    are only ever raised (COALESCE/GREATEST), so a later description-only refresh
    never wipes questions already collected."""
    if not rows:
        return 0
    vals = []
    for r in rows:
        v = [r.get(c) for c in _UP_COLS]
        v[_QI] = Json(r["questions"]) if r.get("questions") is not None else None
        vals.append(v)
    ph = "(" + ",".join(["%s"] * len(_UP_COLS)) + ")"
    sql = ("INSERT INTO job_catalog (" + ",".join(_UP_COLS) + ") VALUES " + ph +
           " ON CONFLICT (ats, company_key, external_id) DO UPDATE SET "
           "title=EXCLUDED.title, location=EXCLUDED.location, department=EXCLUDED.department, "
           "workplace=EXCLUDED.workplace, is_remote=EXCLUDED.is_remote, url=EXCLUDED.url, "
           "description=EXCLUDED.description, description_html=EXCLUDED.description_html, "
           "questions=COALESCE(EXCLUDED.questions, job_catalog.questions), "
           "q_count=GREATEST(EXCLUDED.q_count, job_catalog.q_count), "
           "regions=COALESCE(EXCLUDED.regions, job_catalog.regions), "
           "region_source=COALESCE(EXCLUDED.region_source, job_catalog.region_source), "
           # role/comp: PRESERVE an existing value (gold backfill / earlier rule) —
           # only fill when the row is still NULL, so a 77%-accurate collect-time
           # deterministic pass never clobbers a fleet-labeled row.
           "role_category=COALESCE(job_catalog.role_category, EXCLUDED.role_category), "
           "role_source=COALESCE(job_catalog.role_source, EXCLUDED.role_source), "
           "comp_min=COALESCE(job_catalog.comp_min, EXCLUDED.comp_min), "
           "comp_max=COALESCE(job_catalog.comp_max, EXCLUDED.comp_max), "
           "comp_currency=COALESCE(job_catalog.comp_currency, EXCLUDED.comp_currency), "
           "comp_source=COALESCE(job_catalog.comp_source, EXCLUDED.comp_source), "
           "est_base_min=COALESCE(job_catalog.est_base_min, EXCLUDED.est_base_min), "
           "est_base_max=COALESCE(job_catalog.est_base_max, EXCLUDED.est_base_max), "
           "est_total_min=COALESCE(job_catalog.est_total_min, EXCLUDED.est_total_min), "
           "est_total_max=COALESCE(job_catalog.est_total_max, EXCLUDED.est_total_max), "
           "est_comp_currency=COALESCE(job_catalog.est_comp_currency, EXCLUDED.est_comp_currency), "
           "est_comp_source=COALESCE(job_catalog.est_comp_source, EXCLUDED.est_comp_source), "
           # deterministic from the CURRENT location/text -> new-first (a re-collect refreshes it)
           "open_anywhere=COALESCE(EXCLUDED.open_anywhere, job_catalog.open_anywhere), "
           "last_seen=now()")
    with _cur(False) as cur:
        cur.executemany(sql, vals)
        return cur.rowcount


_LIST_COLS = ("id", "ats", "company_key", "company", "title", "location", "department",
              "workplace", "is_remote", "url", "description", "description_html",
              "questions", "q_count", "comp_min", "comp_max", "comp_currency",
              "est_base_min", "est_base_max", "est_total_min", "est_total_max",
              "est_comp_currency")


def list_jobs(company: str | None = None, q: str | None = None, remote_only: bool = True,
              limit: int = 30, offset: int = 0, region: str | None = None) -> list:
    w, args = _list_where(company, q, remote_only, region)
    with _cur() as cur:
        cur.execute("SELECT " + ",".join(_LIST_COLS) + " FROM job_catalog WHERE " + w +
                    " ORDER BY (q_count > 0) DESC, company ASC, title ASC LIMIT %s OFFSET %s",
                    tuple(args) + (limit, offset))
        return [dict(r) for r in cur.fetchall()]


def list_job_ids(company: str | None = None, q: str | None = None, remote_only: bool = True,
                 region: str | None = None, limit: int = 3000) -> list:
    """The SAME result set as list_jobs (same filters/order) but only id/company/title, capped —
    feeds the catalog's «Выбрать все» (select every job of the current search into a campaign)."""
    w, args = _list_where(company, q, remote_only, region)
    with _cur() as cur:
        cur.execute("SELECT id, company, title FROM job_catalog WHERE " + w +
                    " ORDER BY (q_count > 0) DESC, company ASC, title ASC LIMIT %s",
                    tuple(args) + (limit,))
        return [dict(r) for r in cur.fetchall()]


def _list_where(company, q, remote_only, region):
    where, args = ["NOT COALESCE(dead, FALSE)"], []
    if remote_only:
        where.append("is_remote=TRUE")
    if company:
        where.append("company_key=%s")
        args.append(company)
    if region:
        where.append("%s = ANY(regions)")
        args.append(region)
    if q:
        # A pure COUNTRY/nationality term ("Kazakhstan"/"Казахстан"/"KZ") means "jobs I'm eligible
        # for", so translate it to a region-eligibility overlap (uses the GIN index on regions);
        # anything else stays full-text search over title/company/description.
        from backend.applier.regions import query_country_aliases, query_eligibility_regions
        elig = query_eligibility_regions(q)
        if elig is not None:
            where.append("regions && %s::text[]")
            args.append(elig)
            if elig == ["OTHER"]:
                # OTHER alone also holds postings PINNED to one foreign country ("Remote - India",
                # "Germany") that a Kazakhstani can't take (a 120-job audit: most of the bucket).
                # Keep only postings open to anywhere OR ones that name the asked country/region.
                aliases = query_country_aliases(q)
                where.append("(open_anywhere = TRUE OR location ILIKE ANY(%s::text[]))")
                args.append(aliases)
        else:
            where.append("to_tsvector('simple', coalesce(title,'')||' '||coalesce(company,'')"
                         "||' '||coalesce(description,'')) @@ plainto_tsquery('simple', %s)")
            args.append(q)
    return " AND ".join(where), args


def companies(remote_only: bool = True) -> list:
    conds = ["NOT COALESCE(dead, FALSE)"]
    if remote_only:
        conds.append("is_remote=TRUE")
    w = " WHERE " + " AND ".join(conds)
    with _cur() as cur:
        cur.execute("SELECT company_key, company, COUNT(*) AS n FROM job_catalog" + w +
                    " GROUP BY company_key, company ORDER BY n DESC")
        return [dict(r) for r in cur.fetchall()]


def set_questions(ats: str, company_key: str, external_id: str, questions: list) -> int:
    """Attach/refresh the application-form questions for one existing catalog row."""
    with _cur(False) as cur:
        cur.execute("UPDATE job_catalog SET questions=%s, q_count=%s "
                    "WHERE ats=%s AND company_key=%s AND external_id=%s",
                    (Json(questions), len(questions or []), ats, company_key, external_id))
        return cur.rowcount


def mark_dead(keys: list[tuple], reason: str) -> int:
    """Blacklist postings confirmed gone at the source. `keys` is a list of
    (ats, company_key, external_id). Sets dead=TRUE + dead_reason; reversible
    (UPDATE dead=FALSE). Dead rows are hidden from list_jobs + jobs_for_drafting."""
    if not keys:
        return 0
    with _cur(False) as cur:
        cur.executemany(
            "UPDATE job_catalog SET dead=TRUE, dead_reason=%s "
            "WHERE ats=%s AND company_key=%s AND external_id=%s",
            [(reason, a, c, e) for (a, c, e) in keys])
        return cur.rowcount


def rows_missing_questions(ats: str, missing_only: bool = True) -> list:
    """(external_id, company_key, url, title) for rows of this ATS. Default: only rows
    still missing questions (the backfill work-list). missing_only=False returns ALL
    rows of the ATS — used to REFRESH already-stored questions (e.g. to add options)."""
    where = "ats=%s" + (" AND (q_count=0 OR q_count IS NULL)" if missing_only else "")
    with _cur() as cur:
        cur.execute("SELECT external_id, company_key, url, title FROM job_catalog "
                    "WHERE " + where, (ats,))
        return [dict(r) for r in cur.fetchall()]


def set_regions(ats: str, company_key: str, external_id: str, regions: list, source: str,
                 only_if_null: bool = False) -> int:
    """only_if_null=True guards a backfill write against clobbering a tag a concurrent
    collect just set (no flock between the collect and backfill cron jobs)."""
    sql = ("UPDATE job_catalog SET regions=%s, region_source=%s "
           "WHERE ats=%s AND company_key=%s AND external_id=%s")
    if only_if_null:
        sql += " AND regions IS NULL"
    with _cur(False) as cur:
        cur.execute(sql, (regions, source, ats, company_key, external_id))
        return cur.rowcount


def rows_missing_regions(limit: int = 0) -> list:
    sql = ("SELECT ats, company_key, external_id, title, location, description "
           "FROM job_catalog WHERE regions IS NULL")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _cur() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def set_role(ats: str, company_key: str, external_id: str, category: str, source: str,
             only_if_null: bool = False) -> int:
    """Set role_category/role_source for one row. only_if_null guards a backfill
    write against a concurrent collect (same rationale as set_regions)."""
    sql = ("UPDATE job_catalog SET role_category=%s, role_source=%s "
           "WHERE ats=%s AND company_key=%s AND external_id=%s")
    if only_if_null:
        sql += " AND role_category IS NULL"
    with _cur(False) as cur:
        cur.execute(sql, (category, source, ats, company_key, external_id))
        return cur.rowcount


def set_role_by_title(title: str, category: str, source: str,
                      only_if_null: bool = False) -> int:
    """Set role_category on EVERY row sharing this exact title (role is a property
    of the title, so the gold set is applied title-wise across all companies)."""
    sql = "UPDATE job_catalog SET role_category=%s, role_source=%s WHERE btrim(title)=btrim(%s)"
    if only_if_null:
        sql += " AND role_category IS NULL"
    with _cur(False) as cur:
        cur.execute(sql, (category, source, title))
        return cur.rowcount


def set_comp(row_id: int, comp_min, comp_max, currency: str, source: str,
             only_if_null: bool = False) -> int:
    """Set the posted pay range for one row by id (comp is per-row, not per-title)."""
    sql = ("UPDATE job_catalog SET comp_min=%s, comp_max=%s, comp_currency=%s, "
           "comp_source=%s WHERE id=%s")
    if only_if_null:
        sql += " AND comp_source IS NULL"
    with _cur(False) as cur:
        cur.execute(sql, (comp_min, comp_max, currency, source, row_id))
        return cur.rowcount


def set_est_comp(row_id: int, base_min, base_max, total_min, total_max, currency: str,
                 source: str, only_if_null: bool = False) -> int:
    """Set the RESEARCHED estimated comp for one row by id: an approximate base range
    (est_base_*) + total-comp range (est_total_*, base+bonus+equity), annualized USD.
    Kept DISTINCT from the POSTED comp_* so /stats' posted median stays honest."""
    sql = ("UPDATE job_catalog SET est_base_min=%s, est_base_max=%s, est_total_min=%s, "
           "est_total_max=%s, est_comp_currency=%s, est_comp_source=%s WHERE id=%s")
    if only_if_null:
        sql += " AND est_comp_source IS NULL"
    with _cur(False) as cur:
        cur.execute(sql, (base_min, base_max, total_min, total_max, currency, source, row_id))
        return cur.rowcount


def set_est_comp_for_combo(company_key, role_category, regions, base_min, base_max,
                           total_min, total_max, currency: str, source: str,
                           only_if_null: bool = False) -> int:
    """Apply one company×role×region research estimate to EVERY job of that combo in a
    single UPDATE (the fleet researches ~1.5k combos, not ~6k jobs). NULL company_key /
    role_category are matched with IS NOT DISTINCT FROM; regions is matched by exact array
    equality (or IS NULL) — the same grouping combos_for_est produced."""
    # `%s::text[]` so an EMPTY combo (regions=[]) casts cleanly (a bare empty ARRAY[] has
    # no inferable type); a non-empty list matches by exact array equality.
    reg_clause = "regions = %s::text[]" if regions is not None else "regions IS NULL"
    sql = ("UPDATE job_catalog SET est_base_min=%s, est_base_max=%s, est_total_min=%s, "
           "est_total_max=%s, est_comp_currency=%s, est_comp_source=%s "
           "WHERE company_key IS NOT DISTINCT FROM %s AND role_category IS NOT DISTINCT FROM %s "
           "AND " + reg_clause)
    args = [base_min, base_max, total_min, total_max, currency, source, company_key, role_category]
    if regions is not None:
        args.append(regions)
    if only_if_null:
        sql += " AND est_comp_source IS NULL"
    with _cur(False) as cur:
        cur.execute(sql, tuple(args))
        return cur.rowcount


def rows_missing_est_comp(limit: int = 0) -> list:
    """Live rows with no researched estimate yet — the est-comp backfill work-list. Carries
    the combo dims (company/role/regions) a deterministic estimator keys off."""
    sql = ("SELECT id, company_key, company, role_category, regions, title, location "
           "FROM job_catalog WHERE est_comp_source IS NULL AND NOT COALESCE(dead, FALSE)")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _cur() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def est_from_combo_sibling(company_key, role_category, regions) -> dict | None:
    """The est comp of an EXISTING job in the same (company_key, role_category, regions)
    combo, so a NEW job of a known combo INHERITS the researched value rather than a
    generic role×region median. None if no sibling carries an estimate."""
    reg_clause = "regions = %s::text[]" if regions is not None else "regions IS NULL"
    sql = ("SELECT est_base_min, est_base_max, est_total_min, est_total_max, est_comp_currency "
           "FROM job_catalog WHERE company_key IS NOT DISTINCT FROM %s "
           "AND role_category IS NOT DISTINCT FROM %s AND " + reg_clause +
           " AND est_comp_source IS NOT NULL AND est_base_min IS NOT NULL LIMIT 1")
    args = [company_key, role_category]
    if regions is not None:
        args.append(regions)
    with _cur() as cur:
        cur.execute(sql, tuple(args))
        r = cur.fetchone()
        return dict(r) if r else None


def combos_for_est(limit: int = 0) -> list:
    """Distinct (company_key, company, role_category, regions) combos over LIVE remote jobs
    + a representative title/location + job count — the unit the research fleet estimates
    comp for (comp tracks company×role×region, ~1.5k combos vs ~6k jobs)."""
    sql = ("SELECT company_key, max(company) AS company, role_category, regions, "
           "count(*)::int AS n, (array_agg(title ORDER BY id))[1] AS sample_title, "
           "(array_agg(location ORDER BY id) FILTER "
           "(WHERE location IS NOT NULL AND location <> ''))[1] AS sample_location "
           "FROM job_catalog WHERE NOT COALESCE(dead, FALSE) AND is_remote=TRUE "
           "GROUP BY company_key, role_category, regions "
           # FULLY deterministic order: research-fleet agents each re-query this and take a
           # stable [offset:offset+n] slice, so the ordering must never shift between calls.
           "ORDER BY count(*) DESC, company_key NULLS LAST, role_category NULLS LAST, "
           "regions NULLS LAST")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _cur() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def rows_missing_role(limit: int = 0) -> list:
    """Distinct titles still lacking a role_category (the backfill work-list).
    Role is title-based, so one representative row (+ its department) per title."""
    sql = ("SELECT DISTINCT ON (title) title, department, ats, company_key, external_id "
           "FROM job_catalog WHERE role_category IS NULL AND title IS NOT NULL "
           "ORDER BY title, department NULLS LAST")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _cur() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def rows_missing_comp(limit: int = 0) -> list:
    """Rows with a description but no comp yet (the comp backfill work-list)."""
    sql = ("SELECT id, title, description FROM job_catalog "
           "WHERE comp_source IS NULL AND description IS NOT NULL AND description <> ''")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _cur() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


_JOB_COLS = ("id", "ats", "company_key", "company", "external_id", "title", "location",
             "department", "workplace", "is_remote", "url", "description",
             "questions", "q_count", "regions", "draft", "draft_at", "role_category",
             "comp_min", "comp_max", "comp_currency", "est_base_min", "est_base_max",
             "est_total_min", "est_total_max", "est_comp_currency")


def get_job(job_id: int) -> dict | None:
    """One full catalog row by id (for the draft generator + review page)."""
    with _cur() as cur:
        cur.execute("SELECT " + ",".join(_JOB_COLS) + " FROM job_catalog WHERE id=%s",
                    (job_id,))
        r = cur.fetchone()
        return dict(r) if r else None


def rows_for_open(limit: int = 0, only_null: bool = True) -> list:
    """(id, location, description) work-list for the open_anywhere backfill."""
    sql = "SELECT id, location, description FROM job_catalog"
    if only_null:
        sql += " WHERE open_anywhere IS NULL"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _cur() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def set_open_anywhere(pairs) -> int:
    """Batch-write open_anywhere for [(id, bool), ...]."""
    pairs = [(bool(v), int(i)) for i, v in pairs]
    if not pairs:
        return 0
    with _cur(False) as cur:
        cur.executemany("UPDATE job_catalog SET open_anywhere=%s WHERE id=%s", pairs)
        return len(pairs)


def jobs_by_ids(ids) -> dict:
    """Full rows for a set of ids in ONE query (`id = ANY`), keyed by int id. Dead rows are
    INCLUDED, each carrying its `dead` flag, so a caller can decide: the /unfinished ledger still
    wants the apply url of a posting that went dead, while a campaign rotation skips it. Replaces
    N sequential get_job() calls (the /unfinished page did 114 of them per render)."""
    ids = [int(x) for x in (ids or []) if x is not None]
    if not ids:
        return {}
    with _cur() as cur:
        cur.execute("SELECT " + ",".join(_JOB_COLS) + ", COALESCE(dead, FALSE) AS dead "
                    "FROM job_catalog WHERE id = ANY(%s)", (ids,))
        return {int(r["id"]): dict(r) for r in cur.fetchall()}


def jobs_for_drafting(limit: int = 150, regions=("US", "CA"),
                      min_q: int = 1) -> list:
    """A representative work-list for a draft batch: jobs with real questions whose
    regions overlap the given set, spread ACROSS the four ATS (so the review sample
    covers greenhouse/ashby/lever/workable + a range of question counts), not just
    the first board alphabetically."""
    per = max(1, limit // 4)
    picked: list[dict] = []
    with _cur() as cur:
        for ats in ("greenhouse", "ashby", "lever", "workable"):
            cur.execute(
                "SELECT " + ",".join(_JOB_COLS) + " FROM job_catalog "
                "WHERE ats=%s AND q_count >= %s AND is_remote=TRUE AND regions && %s "
                "AND NOT COALESCE(dead, FALSE) "
                "ORDER BY id LIMIT %s",
                (ats, min_q, list(regions), per))
            picked.extend(dict(r) for r in cur.fetchall())
    return picked[:limit]


def set_draft(job_id: int, draft: dict) -> int:
    with _cur(False) as cur:
        cur.execute("UPDATE job_catalog SET draft=%s, draft_at=now() WHERE id=%s",
                    (Json(draft), job_id))
        return cur.rowcount


def list_drafts(q: str | None = None, limit: int = 60, offset: int = 0) -> list:
    where, args = ["draft IS NOT NULL"], []
    if q:
        where.append("to_tsvector('simple', coalesce(title,'')||' '||coalesce(company,'')) "
                     "@@ plainto_tsquery('simple', %s)")
        args.append(q)
    w = " AND ".join(where)
    with _cur() as cur:
        cur.execute("SELECT id, ats, company, title, url, q_count, regions, "
                    "draft->'stats' AS stats, draft->'candidate' AS candidate "
                    "FROM job_catalog WHERE " + w +
                    " ORDER BY draft_at DESC LIMIT %s OFFSET %s",
                    tuple(args) + (limit, offset))
        return [dict(r) for r in cur.fetchall()]


def drafts_count() -> int:
    with _cur(False) as cur:
        cur.execute("SELECT COUNT(*) FROM job_catalog WHERE draft IS NOT NULL")
        return cur.fetchone()[0]


def counts() -> dict:
    with _cur(False) as cur:
        cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE is_remote), "
                    "COUNT(*) FILTER (WHERE q_count > 0), "
                    "COUNT(*) FILTER (WHERE cardinality(regions) = 0) FROM job_catalog")
        t, rem, wq, resolved_empty = cur.fetchone()
        by_region = {}
        for code in ("US", "CA", "UK", "OTHER"):
            cur.execute("SELECT COUNT(*) FROM job_catalog WHERE %s = ANY(regions)", (code,))
            by_region[code] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM job_catalog WHERE regions IS NULL")
        untagged = cur.fetchone()[0]
    # resolved_empty: rows backfilled to regions=[] (stored as '{}') match neither
    # `%s = ANY(regions)` nor `regions IS NULL`, so without this bucket by_region +
    # untagged don't reconcile to total and an "untagged: 0" log line is misleading.
    return {"total": t, "remote": rem, "with_questions": wq,
            "by_region": by_region, "untagged": untagged, "resolved_empty": resolved_empty}
