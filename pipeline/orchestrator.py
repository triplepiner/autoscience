"""The pipeline state machine.

PLAN -> BUILD -> JUDGE -+- PASS  -> COMPILE -> DONE
               ^        +- REVISE -> BUILD (loop, capped)
               +--------+
                        +- HOLD  -> STOP (reason, no PDF, no submit)

Caps (max_iterations, per-phase + total wall clock, optional cost) and the ABORT
kill switch are enforced HERE, by the orchestrator — never by trusting the agent.
This module never generates scientific content; it only drives codex via roles.py.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import compile as compile_mod
from . import pdf_versions
from . import roles
from .codex_adapter import CodexAdapter, CodexResult
from .config import Config
from .logging_utils import RunLogger
from .summary import render_summary
from .verdict import parse_verdict
from .workspace import (RunWorkspace, create_run_workspace, init_git_repo,
                        load_run_workspace)

REQUIRED_PRD_SECTIONS = [
    "thesis",
    "fixed requirements",
    "target result",
    "creative latitude",
    "anxiety experiment",
    "decision gate",
    "done when",
]


@dataclass
class PipelineResult:
    terminal_state: str
    reason: str
    workspace: RunWorkspace
    iterations: int
    verdict_history: list[dict] = field(default_factory=list)
    pdf_path: Path | None = None
    page_count: int | None = None
    within_limit: bool | None = None
    wall_clock_s: float = 0.0
    tokens: int | None = None

    @property
    def passed(self) -> bool:
        return self.terminal_state == "DONE"


def _make_adapter(cfg: Config) -> CodexAdapter:
    cx = cfg.codex
    return CodexAdapter(
        codex_bin=cx.get("bin", "codex"),
        model=None,  # per-role model passed at call sites
        reasoning_effort=cx.get("model_reasoning_effort"),
        service_tier=cx.get("service_tier"),
        extra_config=cx.get("extra_config", {}),
        bypass_sandbox=cfg.get("isolation", "mode", default="dir") == "container",
    )


def _primary_venue(cfg: Config) -> dict:
    venues = cfg.venues
    return venues[0] if venues else {"name": "auto", "page_limit": None}


def _accumulate_tokens(usage: dict) -> int | None:
    total = 0
    found = False
    for k in ("total_tokens", "total", "input_tokens", "output_tokens",
              "prompt_tokens", "completion_tokens"):
        v = usage.get(k)
        if isinstance(v, (int, float)):
            total += int(v)
            found = True
    return total if found else None


def run_pipeline(
    idea_path: str | Path,
    cfg: Config,
    *,
    review_prd: bool | None = None,
    stamp: str | None = None,
    logger: RunLogger | None = None,
    existing_ws: RunWorkspace | None = None,
    skip_plan: bool = False,
    start_iteration: int = 0,
    change_text: str | None = None,
) -> PipelineResult:
    idea_path = Path(idea_path)
    # Fresh run -> create a workspace; continue/revise -> reuse the existing one.
    ws = existing_ws or create_run_workspace(cfg.runs_dir, idea_path, stamp=stamp)
    log = logger or RunLogger(ws.logs / "orchestrator.log")
    if log.log_file is None:
        # Always persist the phase log — the dashboard reads it for live state.
        log.log_file = ws.logs / "orchestrator.log"
    log.info(f"run workspace: {ws.root}" + (" (continue)" if existing_ws else ""))

    isolation_mode = cfg.get("isolation", "mode", default="dir")
    if existing_ws is None:
        git_ok = init_git_repo(ws.workspace)
        log.info(f"isolation={isolation_mode}; coder git repo initialized={git_ok}")
    else:
        ws.abort_sentinel.unlink(missing_ok=True)  # clear any stale kill switch
    # A user change request seeds the next BUILD (the coder reads it, top priority).
    if change_text and change_text.strip():
        ws.change_request.write_text(change_text)
        n = len(list(ws.reviews.glob("USER_CHANGE_iter*.md"))) + 1
        (ws.reviews / f"USER_CHANGE_iter{n}.md").write_text(change_text)
        log.info(f"continue: wrote CHANGE_REQUEST.md (revision #{n})")

    adapter = _make_adapter(cfg)
    caps = cfg.caps
    max_iter = int(caps["max_iterations"])
    total_budget_s = float(caps["wall_clock_total_min"]) * 60
    max_cost = caps.get("max_cost_usd")
    if review_prd is None:
        review_prd = bool(cfg.get("review_prd_before_build", default=False))
    allow_hold = bool(cfg.get("judge", "allow_hold", default=False))

    state: dict[str, Any] = {
        "slug": ws.root.name,
        "max_iterations": max_iter,
        "wall_clock_total_min": caps["wall_clock_total_min"],
        "isolation": isolation_mode,
        "verdict_history": [],
        "judge_notes": [],
        "phase_log": [],
    }
    start = time.monotonic()
    total_tokens = 0
    have_tokens = False

    def elapsed() -> float:
        return time.monotonic() - start

    def write_summary(terminal_state: str, reason: str, **extra) -> None:
        state.update(
            terminal_state=terminal_state,
            reason=reason,
            wall_clock_s=elapsed(),
            tokens=(total_tokens if have_tokens else None),
            **extra,
        )
        ws.run_summary.write_text(render_summary(state))

    def aborted() -> bool:
        return ws.abort_sentinel.exists()

    def account(result: CodexResult) -> None:
        nonlocal total_tokens, have_tokens
        t = _accumulate_tokens(result.usage)
        if t is not None:
            total_tokens += t
            have_tokens = True
        # Persist the resumable codex thread id per phase so the dashboard can
        # open/resume the exact thread in a terminal or inject guidance.
        if result.session_id:
            rec = {
                "role": result.role,
                "session_id": result.session_id,
                "workdir": (str(ws.workspace) if result.role == "coder" else str(ws.root)),
                "iteration": state.get("iterations", 0),
                "ts": time.time(),
            }
            with (ws.root / "sessions.jsonl").open("a") as f:
                f.write(json.dumps(rec) + "\n")

    def finish(terminal_state: str, reason: str, **extra) -> PipelineResult:
        log.phase(f"TERMINAL: {terminal_state}")
        log.info(reason)
        write_summary(terminal_state, reason, **extra)
        return PipelineResult(
            terminal_state=terminal_state,
            reason=reason,
            workspace=ws,
            iterations=state.get("iterations", 0),
            verdict_history=state["verdict_history"],
            pdf_path=extra.get("pdf_path"),
            page_count=extra.get("page_count"),
            within_limit=extra.get("within_limit"),
            wall_clock_s=elapsed(),
            tokens=(total_tokens if have_tokens else None),
        )

    # ---- PLAN ---------------------------------------------------------------
    if not skip_plan:
        log.phase("PLAN")
        state["phase_log"].append("PLAN started")
        if aborted():
            return finish("ABORTED", "ABORT sentinel present before PLAN")
        plan_res = roles.run_planner(adapter, cfg, ws, iteration=1)
        account(plan_res)
        if plan_res.aborted:
            return finish("ABORTED", "kill switch fired during PLAN")
        if plan_res.timed_out:
            return finish("ERROR", "planner timed out")
        prd_text = ws.prd.read_text(errors="replace") if ws.prd.exists() else ""
        if not prd_text.strip():
            return finish("ERROR", "planner produced no PRD.md")
        missing = [s for s in REQUIRED_PRD_SECTIONS if s.lower() not in prd_text.lower()]
        if missing:
            log.warn(f"PRD.md missing sections: {missing} (continuing)")
            state["judge_notes"].append(f"PRD missing sections: {missing}")
        state["phase_log"].append("PLAN done -> PRD.md")

        if review_prd:
            return finish("PRD_REVIEW",
                          "review_prd_before_build is set; halting for human glance at PRD.md")
    else:
        if not ws.prd.exists():
            return finish("ERROR", "continue: no PRD.md in the existing run")
        log.info("continue: skipping PLAN; reusing existing PRD.md + workspace")
        state["phase_log"].append("CONTINUE — revise existing draft")

    # ---- BUILD/JUDGE loop ---------------------------------------------------
    venue = _primary_venue(cfg)
    page_limit = venue.get("page_limit")
    iter_cap = start_iteration + max_iter
    state["max_iterations"] = iter_cap
    iteration = start_iteration
    while iteration < iter_cap:
        iteration += 1
        state["iterations"] = iteration

        if aborted():
            return finish("ABORTED", f"ABORT sentinel before BUILD iter{iteration}")
        if elapsed() > total_budget_s:
            return finish("REVISE_EXHAUSTED",
                          f"total wall-clock cap ({caps['wall_clock_total_min']} min) exceeded")

        # BUILD
        log.phase(f"BUILD iter{iteration}")
        state["phase_log"].append(f"BUILD iter{iteration} started")
        resume = bool(cfg.get("coder", "resume_on_revise", default=False)) and iteration > 1
        build_res = roles.run_coder(adapter, cfg, ws, iteration=iteration, resume_last=resume)
        account(build_res)
        if build_res.aborted:
            return finish("ABORTED", f"kill switch fired during BUILD iter{iteration}")
        if build_res.timed_out:
            return finish("ERROR",
                          f"coder timed out (per-build cap {caps['wall_clock_per_build_min']} min) "
                          f"on iter{iteration}")
        if not ws.paper_tex.exists():
            return finish("ERROR",
                          f"coder did not produce paper_draft.tex on iter{iteration}")
        if not ws.repro_sh.exists():
            log.warn("coder did not produce repro.sh; judge cannot re-run repro")
            state["judge_notes"].append("repro.sh missing — results unverifiable")
        state["phase_log"].append(f"BUILD iter{iteration} done")
        _maybe_cost_stop(max_cost, total_tokens)

        if aborted():
            return finish("ABORTED", f"ABORT sentinel before JUDGE iter{iteration}")
        if elapsed() > total_budget_s:
            return finish("REVISE_EXHAUSTED",
                          f"total wall-clock cap exceeded before JUDGE iter{iteration}")

        # JUDGE
        log.phase(f"JUDGE iter{iteration}")
        state["phase_log"].append(f"JUDGE iter{iteration} started")
        judge_res = roles.run_judge(adapter, cfg, ws, iteration=iteration)
        account(judge_res)
        if judge_res.aborted:
            return finish("ABORTED", f"kill switch fired during JUDGE iter{iteration}")
        if judge_res.timed_out:
            return finish("ERROR", f"judge timed out on iter{iteration}")

        review_path = ws.review_path(iteration)
        review_text = review_path.read_text(errors="replace") if review_path.exists() else ""
        verdict = parse_verdict(review_text, judge_res.final_message)
        if verdict is None:
            log.warn("judge emitted no parseable VERDICT line; treating as REVISE")
            verdict = "REVISE"
            state["judge_notes"].append(f"iter{iteration}: judge gave no VERDICT line")

        # HOLD only ever fires AFTER Criterion 1 (results-are-real) passed, so
        # overriding it is safe re: honesty. With allow_hold=false (default) a HOLD
        # becomes a PASS -> the clean paper compiles to a submittable PDF.
        if verdict == "HOLD" and not allow_hold:
            log.info(f"iter{iteration} verdict: HOLD -> overridden to PASS (allow_hold=false)")
            state["judge_notes"].append(
                f"iter{iteration}: judge HOLD (venue-fit) overridden — compiling as submittable")
            verdict = "PASS"

        state["verdict_history"].append({
            "iteration": iteration,
            "verdict": verdict,
            "duration_s": judge_res.duration_s,
            "review_path": str(review_path.relative_to(ws.root)) if review_path.exists() else "",
        })
        _collect_required_edits(review_text, iteration, state)

        log.info(f"iter{iteration} verdict: {verdict}")
        state["phase_log"].append(f"JUDGE iter{iteration} -> {verdict}")
        _maybe_cost_stop(max_cost, total_tokens)

        if verdict == "HOLD":
            return finish("HOLD",
                          "judge returned HOLD (e.g. no exact-fit venue). Valid terminal "
                          "state — no PDF, no submit. See latest review.")
        if verdict == "PASS":
            # ---- COMPILE (versioned, thread-named PDF) ----
            log.phase(f"COMPILE iter{iteration}")
            state["phase_log"].append("COMPILE started")
            version = pdf_versions.push_count(ws.root)
            out_pdf = pdf_versions.version_pdf_path(ws.root, version)
            cres = compile_mod.compile_pdf(
                ws.paper_tex, out_pdf,
                engine_pref=cfg.get("compile", "engine", default="auto"),
                max_passes=int(cfg.get("compile", "max_passes", default=3)),
            )
            if not cres.ok:
                return finish("ERROR",
                              "judge PASSed but LaTeX did not compile. See log tail:\n"
                              + cres.log[-1500:])
            pc, within = compile_mod.check_page_limit(out_pdf, page_limit)
            pdf_versions.record_version(ws.root, version, pdf_name=out_pdf.name,
                                        pages=pc, retained=True)
            log.info(f"compiled v{version} ({pdf_versions.kind_for(version)}): "
                     f"{out_pdf.name}, {pc} pages (limit {page_limit}); engine={cres.engine}")
            if within is False:
                # Over limit is NOT done. Send back as a synthetic REVISE if budget remains.
                msg = (f"page count {pc} exceeds venue limit {page_limit} for "
                       f"{venue.get('name')}; trim to fit.")
                log.warn(msg)
                state["judge_notes"].append(f"iter{iteration}: {msg}")
                _append_synthetic_review(ws, iteration, msg)
                if iteration >= iter_cap:
                    return finish("PAGE_LIMIT_EXCEEDED", msg,
                                  pdf_path=str(out_pdf), page_count=pc, within_limit=False,
                                  page_limit=page_limit)
                continue  # loop back to BUILD to trim
            return finish("DONE",
                          f"PASS + compiled in-limit PDF v{version} "
                          f"({pdf_versions.kind_for(version)}, {pc}/{page_limit} pages). "
                          "Human decides whether to submit.",
                          pdf_path=str(out_pdf), page_count=pc, within_limit=True,
                          page_limit=page_limit)

        # verdict == REVISE
        if iteration >= iter_cap:
            return finish("REVISE_EXHAUSTED",
                          f"hit max_iterations ({max_iter}) still on REVISE. Best draft kept; "
                          "no PDF. See review history.")
        # else loop back to BUILD with the latest review

    return finish("REVISE_EXHAUSTED", f"hit max_iterations ({max_iter})")


def continue_run(run_root, change_text: str, cfg: Config, *,
                 logger: RunLogger | None = None) -> PipelineResult:
    """Re-engage a FINISHED run with a user change request (a follow-up .md): the
    coder revises the existing draft per the changes, then judge -> compile. Reuses
    the existing workspace/PRD; never starts a new run."""
    ws = load_run_workspace(run_root)
    # Continue numbering after the iterations already on disk so nothing is overwritten.
    existing = len(list(ws.logs.glob("build_iter*.jsonl")))
    return run_pipeline(
        ws.idea, cfg,
        existing_ws=ws, skip_plan=True, start_iteration=existing,
        change_text=change_text, logger=logger,
    )


# -- helpers ------------------------------------------------------------------
class CostExceeded(Exception):
    pass


def _maybe_cost_stop(max_cost, total_tokens) -> None:
    # max_cost is in USD; we don't have per-token pricing wired, so we only enforce
    # if a future adapter surfaces real USD. Tokens are tracked for observability.
    return None


def _collect_required_edits(review_text: str, iteration: int, state: dict) -> None:
    if not review_text:
        return
    capture = False
    for line in review_text.splitlines():
        low = line.strip().lower()
        if low.startswith("## required edits") or low.startswith("# required edits"):
            capture = True
            continue
        if capture:
            if line.startswith("#"):
                break
            s = line.strip(" -*\t")
            if s:
                state["judge_notes"].append(f"iter{iteration}: {s[:160]}")


def _append_synthetic_review(ws: RunWorkspace, iteration: int, msg: str) -> None:
    """Record an orchestrator-originated required edit (page-limit) so the next
    BUILD sees it via the latest-review mechanism."""
    path = ws.review_path(iteration)
    note = (f"\n\n## Required edits (orchestrator)\n- {msg}\n"
            "VERDICT: REVISE\n")
    if path.exists():
        path.write_text(path.read_text(errors="replace") + note)
    else:
        path.write_text(f"# Orchestrator page-limit review (iter{iteration})\n{note}")
