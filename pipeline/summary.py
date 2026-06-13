"""Render run_summary.md — one glance tells Makar: pass?, how many rounds, what the
judge kept complaining about, where the PDF is."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .logging_utils import utc_now_iso


def render_summary(state: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append

    a(f"# Run summary — {state.get('slug', '?')}")
    a("")
    a(f"- **Terminal state:** `{state.get('terminal_state', '?')}`")
    a(f"- **Reason:** {state.get('reason', '')}")
    a(f"- **Iterations used:** {state.get('iterations', 0)} / {state.get('max_iterations', '?')}")
    a(f"- **Wall clock:** {state.get('wall_clock_s', 0):.1f}s "
      f"(cap {state.get('wall_clock_total_min', '?')} min)")
    cost = state.get("cost_usd")
    a(f"- **Cost:** {('$%.4f' % cost) if isinstance(cost, (int, float)) else 'n/a'}")
    a(f"- **Tokens:** {state.get('tokens', 'n/a')}")
    pdf = state.get("pdf_path")
    a(f"- **PDF:** {pdf if pdf else '(none — no PASS)'}")
    pc = state.get("page_count")
    if pc is not None:
        a(f"- **Page count:** {pc} / {state.get('page_limit', '?')} "
          f"({'within limit' if state.get('within_limit') else 'OVER LIMIT'})")
    a(f"- **Isolation:** {state.get('isolation', '?')}")
    a(f"- **Generated:** {utc_now_iso()}")
    a("")

    a("## Verdict history")
    history = state.get("verdict_history", [])
    if not history:
        a("_(no judge iterations ran)_")
    else:
        a("")
        a("| iter | verdict | wall(s) | review |")
        a("|---|---|---|---|")
        for h in history:
            a(f"| {h.get('iteration')} | `{h.get('verdict')}` | "
              f"{h.get('duration_s', 0):.0f} | {h.get('review_path', '')} |")
    a("")

    notes = state.get("judge_notes", [])
    if notes:
        a("## What the judge kept complaining about")
        for n in notes:
            a(f"- {n}")
        a("")

    a("## Phase log")
    for ph in state.get("phase_log", []):
        a(f"- {ph}")
    a("")

    a("## Safety")
    a("- No auto-submission. Terminal output is a PDF + verdict; submitting is a human action.")
    a(f"- Coder isolation: **{state.get('isolation', '?')}** "
      "(danger-full-access confined to runs/<id>/workspace/).")
    a("- Caps + kill switch enforced by the orchestrator, not by trusting the agent.")
    a("")

    return "\n".join(lines) + "\n"
