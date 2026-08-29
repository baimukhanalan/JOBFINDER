"""Funnel-stage ranking tests: a candidate belongs to the FURTHEST inbound stage it
reached, so a progressed candidate no longer shows under an earlier bucket (e.g. a
candidate that got an interview/offer is no longer listed under «Действие»/action_needed).

Pure — exercises the single source of the ranking (mail_db.furthest_stage) with the
standard library alone; no Postgres. mail_db imports psycopg2 but does not connect at
import time (lazy pool), so this loads without a database.
"""
from __future__ import annotations

import importlib
import re
import sys
import unittest

# Some sibling tests (test_mailcrm_inbox) install a minimal fake `backend.tools.mail_db`
# stub into sys.modules to stay dependency-free; drop any such stub so we exercise the REAL
# module (psycopg2 is installed and mail_db does not connect at import — lazy pool).
_stub = sys.modules.get("backend.tools.mail_db")
if _stub is not None and not hasattr(_stub, "furthest_stage"):
    sys.modules.pop("backend.tools.mail_db", None)
mail_db = importlib.import_module("backend.tools.mail_db")


class FurthestStageTests(unittest.TestCase):
    def test_empty_is_other(self):
        self.assertEqual(mail_db.furthest_stage(set()), "other")
        self.assertEqual(mail_db.furthest_stage(None), "other")

    def test_single_kinds_map_to_themselves(self):
        for k in ("offer", "interview", "action_needed", "rejection", "ack", "other"):
            self.assertEqual(mail_db.furthest_stage({k}), k)

    def test_unknown_kind_falls_back_to_other(self):
        self.assertEqual(mail_db.furthest_stage({"some_new_kind"}), "other")

    def test_interview_supersedes_action_needed(self):
        # THE bug: an old action_needed + a later interview must resolve to interview,
        # so the candidate leaves the «Действие» bucket.
        self.assertEqual(
            mail_db.furthest_stage({"action_needed", "interview"}), "interview")

    def test_offer_supersedes_everything(self):
        self.assertEqual(
            mail_db.furthest_stage({"action_needed", "interview", "rejection", "ack", "offer"}),
            "offer")

    def test_action_needed_outranks_rejection_and_ack(self):
        # Deliberate: best-of ranking (matches tools/stats.py) — a later rejection/ack does
        # NOT "un-progress" a lead that still needs action. Only interview/offer supersede it.
        self.assertEqual(mail_db.furthest_stage({"action_needed", "rejection"}), "action_needed")
        self.assertEqual(mail_db.furthest_stage({"action_needed", "ack"}), "action_needed")

    def test_rejection_outranks_ack_and_other(self):
        self.assertEqual(mail_db.furthest_stage({"rejection", "ack", "other"}), "rejection")

    def test_ack_outranks_other(self):
        self.assertEqual(mail_db.furthest_stage({"ack", "other"}), "ack")

    def test_full_priority_order(self):
        rank = ["offer", "interview", "action_needed", "rejection", "ack", "other"]
        # for any pair, the higher-ranked one wins regardless of set order
        for i, hi in enumerate(rank):
            for lo in rank[i + 1:]:
                self.assertEqual(mail_db.furthest_stage({hi, lo}), hi,
                                 f"{hi} should outrank {lo}")

    def test_sql_case_matches_python_ranking(self):
        # Guard: the in-DB CASE must list the ranked kinds in the SAME order as _STAGE_RANK,
        # so mail_db.stage_counts()/candidate_groups() cannot silently diverge from the pure
        # helper the tests lock above.
        sql = mail_db._FURTHEST_STAGE_SQL
        positions = []
        for kind in mail_db._STAGE_RANK[:-1]:  # every ranked kind except the 'other' fallback
            m = re.search(rf"kind='{kind}'", sql)
            self.assertIsNotNone(m, f"{kind} missing from _FURTHEST_STAGE_SQL")
            positions.append(m.start())
        self.assertEqual(positions, sorted(positions),
                         "_FURTHEST_STAGE_SQL kind order must match _STAGE_RANK")


if __name__ == "__main__":
    unittest.main()
