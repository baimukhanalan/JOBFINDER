"""Self-verifying probe + auto-promoter for the Progressive lane (see roberthalf_probe_promote)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import roberthalf_probe_promote as pp  # noqa: E402

run_once = pp.run_once
classify = pp.classify
box_is_quiet = pp.box_is_quiet
already_verified = pp.already_verified


def main() -> None:
    pp._cli("progressive", "PROGRESSIVE_ADVANCE")


if __name__ == "__main__":
    main()
