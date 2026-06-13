"""Tiny structured logging helpers: a run-scoped logger that tees to console + file."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path


def utc_stamp() -> str:
    """Filesystem-safe UTC timestamp, e.g. 20260612T091500Z."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class RunLogger:
    """Appends to runs/<id>/logs/orchestrator.log and echoes to stderr."""

    def __init__(self, log_file: Path | None = None, echo: bool = True):
        self.log_file = log_file
        self.echo = echo
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)

    def log(self, msg: str, level: str = "INFO") -> None:
        line = f"[{utc_now_iso()}] {level:5s} {msg}"
        if self.echo:
            print(line, file=sys.stderr, flush=True)
        if self.log_file is not None:
            with self.log_file.open("a") as f:
                f.write(line + "\n")

    def info(self, msg: str) -> None:
        self.log(msg, "INFO")

    def warn(self, msg: str) -> None:
        self.log(msg, "WARN")

    def error(self, msg: str) -> None:
        self.log(msg, "ERROR")

    def phase(self, name: str) -> None:
        self.log(f"==== {name} ====", "PHASE")
