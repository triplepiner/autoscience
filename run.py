#!/usr/bin/env python3
"""Autonomous workshop-paper pipeline — CLI entrypoint.

    python run.py IDEA.md [--config config.yaml] [--review-prd] [--runs-dir DIR]
                  [--model NAME] [--max-iterations N] [--codex-bin PATH]

One idea file in -> one workshop-submittable PDF out (on PASS), gated by a judge
that refuses to ship unverified or oversold results. It NEVER submits to a venue.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline.config import load_config
from pipeline.logging_utils import RunLogger
from pipeline.orchestrator import run_pipeline


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Autonomous workshop-paper pipeline")
    p.add_argument("idea", help="path to idea.md")
    p.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    p.add_argument("--review-prd", action="store_true",
                   help="halt after PLAN for a human glance at PRD.md")
    p.add_argument("--runs-dir", default=None, help="override runs_dir from config")
    p.add_argument("--model", default=None, help="override model for all roles")
    p.add_argument("--max-iterations", type=int, default=None)
    p.add_argument("--codex-bin", default=None, help="override codex binary path")
    args = p.parse_args(argv)

    idea = Path(args.idea)
    if not idea.exists():
        print(f"error: idea file not found: {idea}", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    if args.runs_dir:
        cfg.data["paths"]["runs_dir"] = args.runs_dir
    if args.model:
        for role in ("planner", "coder", "judge"):
            cfg.data["models"][role] = args.model
    if args.max_iterations is not None:
        cfg.data["caps"]["max_iterations"] = args.max_iterations
    if args.codex_bin:
        cfg.data["codex"]["bin"] = args.codex_bin

    log = RunLogger(echo=True)
    log.info("=== Autonomous Workshop-Paper Pipeline ===")
    log.info(f"idea: {idea}")
    log.info(f"model: {cfg.model('planner')} | isolation: {cfg.get('isolation','mode')} | "
             f"max_iter: {cfg.caps['max_iterations']}")

    result = run_pipeline(idea, cfg, review_prd=args.review_prd, logger=log)

    print("\n" + "=" * 64)
    print(f"TERMINAL STATE: {result.terminal_state}")
    print(f"reason        : {result.reason}")
    print(f"iterations    : {result.iterations}")
    if result.pdf_path:
        print(f"PDF           : {result.pdf_path} "
              f"({result.page_count} pages, within_limit={result.within_limit})")
    print(f"run summary   : {result.workspace.run_summary}")
    print("=" * 64)

    # Exit codes: 0 = DONE, 10 = HOLD, 11 = REVISE_EXHAUSTED/PAGE_LIMIT, 1 = ERROR/ABORTED
    return {
        "DONE": 0,
        "HOLD": 10,
        "PRD_REVIEW": 0,
        "REVISE_EXHAUSTED": 11,
        "PAGE_LIMIT_EXCEEDED": 11,
    }.get(result.terminal_state, 1)


if __name__ == "__main__":
    raise SystemExit(main())
