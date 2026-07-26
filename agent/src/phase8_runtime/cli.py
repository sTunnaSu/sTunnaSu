"""Command-line entry point for the bounded Phase 8 runtime."""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from src.phase8_runtime.configuration import load_runtime_config
from src.phase8_runtime.models import RuntimeMode
from src.phase8_runtime.runtime import build_phase8_runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentLarry Phase 8 Alpaca-paper runtime")
    parser.add_argument("--config", required=True, help="Phase 8 JSON/YAML configuration")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--research-only", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--paper-execute", action="store_true")
    parser.add_argument(
        "--authorize-paper-execution",
        action="store_true",
        help="Second explicit flag required with --paper-execute; never authorizes live trading",
    )
    return parser


def _mode(args: argparse.Namespace) -> RuntimeMode:
    if args.paper_execute:
        return RuntimeMode.PAPER_EXECUTE
    if args.dry_run:
        return RuntimeMode.DRY_RUN
    return RuntimeMode.RESEARCH_ONLY


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = _mode(args)
    if mode is RuntimeMode.PAPER_EXECUTE and not args.authorize_paper_execution:
        parser.error("--paper-execute also requires --authorize-paper-execution")
    config = load_runtime_config(
        args.config,
        mode=mode,
        authorize_paper_execution=args.authorize_paper_execution,
    )
    runtime = build_phase8_runtime(config)
    try:
        result = runtime.run()
    finally:
        runtime.close()
    print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
    return 0 if not result.safety_halts else 2
