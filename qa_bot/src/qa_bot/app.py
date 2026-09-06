"""Local bootstrap only: validate configuration and print run metadata."""
import argparse
import asyncio
import os
from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4

from qa_bot.config import load_config
from qa_bot.domain.state import RunPhase, RunState


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QA skeleton: local dry-run bootstrap")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--extract", action="store_true")
    parser.add_argument("--route", action="store_true")
    parser.add_argument("--url-env", default="QA_PAGE_URL")
    parser.add_argument("--allow-origin", action="append", default=[])
    parser.add_argument("--log", type=Path, default=Path("runs/reports/observations.jsonl"))
    parser.add_argument("--mode", choices=("dry-run",), default="dry-run")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError, TypeError) as error:
        parser.exit(2, f"Configuration error: {error}\n")
    state = RunState(run_id=str(uuid4()), phase=RunPhase.DRY_RUN_READY)
    if args.observe or args.extract or args.route:
        url = os.environ.get(args.url_env)
        if not url or not args.allow_origin:
            parser.exit(2, "Observe requires URL environment variable and --allow-origin.\n")
        from qa_bot.orchestration.observe import observe_once
        from qa_bot.reporting.observation_log import state_event, append_event
        try:
            state = asyncio.run(observe_once(
                url, tuple(args.allow_origin), state,
                timeout=config.operation_timeout_seconds, extract=args.extract, route=args.route))
            event = state_event(state)
            append_event(args.log, event)
        except Exception:
            parser.exit(2, "Observation failed; browser closed. No page content logged.\n")
        print(json.dumps(event, ensure_ascii=False, indent=2))
        return 3 if state.extraction_errors or (args.route and state.selected_adapter is None) else 0
    print(json.dumps({
        "status": "skeleton_ready",
        "state": asdict(state),
        "config": asdict(config),
        "adapters_initialized": [],
        "actions_executed": 0,
    }, ensure_ascii=False, indent=2))
    return 0
