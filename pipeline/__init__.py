"""Autonomous workshop-paper pipeline orchestrator.

This package is the MACHINE that drives codex agents (planner -> coder -> judge).
It never produces scientific content itself; everything substantive routes through
codex. See orchestrator.py for the state machine.
"""

__all__ = ["orchestrator", "codex_adapter", "config", "verdict", "compile", "roles"]
