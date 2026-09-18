"""The full nightly collect must reap postings that disappeared at their source, WITHOUT ever
aging a board that flapped:

  * `deactivate_stale` is per-source-success-gated — only boards that returned >=1 row this run
    are eligible to have their un-re-seen rows marked dead; a board that came back empty/failed is
    left completely alone.
  * the reaper only runs on a FULL (non-filtered, non-limited) collect.
  * `sweep_blocklist_gone` reaps ONLY the blocklisted-slug rows that vanished from a fresh board
    fetch (keeps the still-live ones), and SKIPS a board whose fresh fetch came back empty.

All DB calls are monkeypatched — no network, no Postgres.
"""
from backend.tools import catalog_collector as cc

_EMPTY_SLUGS = {"greenhouse": {}, "ashby": {}, "lever": {}, "workable": {}, "breezy": {}}


def _wire(monkeypatch, slugs, fetch):
    monkeypatch.setattr(cc.catalog_db, "ensure_schema", lambda: None)
    monkeypatch.setattr(cc.catalog_db, "upsert_jobs", lambda rows: len(rows))
    monkeypatch.setattr(cc.catalog_db, "counts", lambda: {})
    monkeypatch.setattr(cc, "_slugs", lambda: dict(slugs))
    monkeypatch.setattr(cc.ats_boards, "fetch_board", fetch)
    # keep the blocklist sweep inert unless a test drives it directly
    monkeypatch.setattr(cc, "sweep_blocklist_gone", lambda: {"boards": 0, "dead": 0})


def _job(slug, jid="j1"):
    return {"id": jid, "title": "Customer Service Representative", "isRemote": True,
            "applyUrl": f"https://boards.greenhouse.io/{slug}/jobs/{jid}", "location": "Remote, USA"}


def test_stale_reaper_only_ages_successfully_fetched_boards(monkeypatch):
    def fetch(ats, slug):
        return [_job(slug)] if slug == "live" else []      # 'flap' returns [] (transient/empty)
    slugs = dict(_EMPTY_SLUGS, greenhouse={"live": "Live", "flap": "Flap"})
    _wire(monkeypatch, slugs, fetch)
    captured = {}
    monkeypatch.setattr(cc.catalog_db, "deactivate_stale",
                        lambda seen, ts, reason="stale": captured.update(
                            boards=set(seen), reason=reason) or 0)
    cc.run(with_questions=False)
    assert captured["boards"] == {("greenhouse", "live")}   # 'flap' NOT aged
    assert captured["reason"] == "stale"


def test_stale_reaper_skipped_on_filtered_or_limited_run(monkeypatch):
    slugs = dict(_EMPTY_SLUGS, greenhouse={"live": "Live"})
    _wire(monkeypatch, slugs, lambda ats, slug: [_job(slug)])
    calls = {"n": 0}
    monkeypatch.setattr(cc.catalog_db, "deactivate_stale",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or 0)
    cc.run(with_questions=False, ats_filter="greenhouse")   # filtered → no reaper
    cc.run(with_questions=False, limit=1)                    # limited  → no reaper
    assert calls["n"] == 0


def test_blocklist_sweep_reaps_only_gone(monkeypatch):
    monkeypatch.setattr(cc.catalog_db, "ensure_schema", lambda: None)
    monkeypatch.setattr(cc.boards, "blocked_slugs", lambda: {"nogigiddy"})
    monkeypatch.setattr(cc.catalog_db, "live_boards_for_slugs",
                        lambda keys: [("workable", "nogigiddy")])
    monkeypatch.setattr(cc.catalog_db, "live_external_ids",
                        lambda ats, slug: {"a", "b", "c"})   # 3 live in the catalog
    # the fresh board still lists a + b; c is GONE
    monkeypatch.setattr(cc.ats_boards, "fetch_board",
                        lambda ats, slug: [{"id": "a"}, {"id": "b"}])
    marked = {}
    monkeypatch.setattr(cc.catalog_db, "mark_dead",
                        lambda keys, reason: marked.update(keys=list(keys), reason=reason)
                        or len(keys))
    out = cc.sweep_blocklist_gone()
    assert marked["keys"] == [("workable", "nogigiddy", "c")]   # ONLY the gone one
    assert marked["reason"] == "blocklist-gone"
    assert out == {"boards": 1, "dead": 1}


def test_blocklist_sweep_skips_empty_fetch(monkeypatch):
    # an empty/transient fresh fetch must NEVER mass-dead a whole aggregator
    monkeypatch.setattr(cc.catalog_db, "ensure_schema", lambda: None)
    monkeypatch.setattr(cc.boards, "blocked_slugs", lambda: {"nogigiddy"})
    monkeypatch.setattr(cc.catalog_db, "live_boards_for_slugs",
                        lambda keys: [("workable", "nogigiddy")])
    monkeypatch.setattr(cc.catalog_db, "live_external_ids", lambda ats, slug: {"a", "b"})
    monkeypatch.setattr(cc.ats_boards, "fetch_board", lambda ats, slug: [])   # empty → skip
    monkeypatch.setattr(cc.catalog_db, "mark_dead",
                        lambda keys, reason: (_ for _ in ()).throw(
                            AssertionError("must not reap on an empty fetch")))
    assert cc.sweep_blocklist_gone() == {"boards": 0, "dead": 0}
