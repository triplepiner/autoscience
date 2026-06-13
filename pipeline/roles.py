"""Role invocation: compose the prompt for each role and call the adapter.

The orchestrator stays role-agnostic; this module owns the per-role prompt
composition, sandbox selection, working directory, and timeout.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .codex_adapter import CodexAdapter, CodexResult
from .config import Config
from .experiment_api import experiment_env, resolve_model
from .logging_utils import utc_now_iso
from .workspace import RunWorkspace


def _read(path: Path) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _compose(role: str, role_prompt: str, body: str) -> str:
    """Prepend a stable machine marker the mock can grep; real codex treats it as a
    harmless preamble label."""
    header = f"# autoscience role: {role}\n# composed: {utc_now_iso()}\n\n"
    return f"{header}{role_prompt}\n\n---\n\n{body}"


def run_planner(adapter: CodexAdapter, cfg: Config, ws: RunWorkspace, iteration: int) -> CodexResult:
    role_prompt = _read(cfg.prompts_dir / "planner.md")
    idea = _read(ws.idea)
    body = (
        "## Your working directory\n"
        f"You are launched in `{ws.root}`. Read `idea.md` (below) and write `PRD.md` here.\n\n"
        "## idea.md\n```\n" + idea + "\n```\n"
    )
    prompt = _compose("planner", role_prompt, body)
    timeout = cfg.caps["wall_clock_per_plan_min"] * 60
    return adapter.run(
        role="planner",
        prompt=prompt,
        workdir=ws.root,
        sandbox=cfg.sandbox("planner"),
        output_last_message=ws.logs / f"plan_iter{iteration}.final.txt",
        logs_prefix=ws.logs / f"plan_iter{iteration}",
        timeout_s=timeout,
        model=cfg.model("planner"),
        skip_git_repo_check=True,
        abort_sentinel=ws.abort_sentinel,
    )


def run_coder(adapter: CodexAdapter, cfg: Config, ws: RunWorkspace, iteration: int,
              resume_last: bool = False) -> CodexResult:
    role_prompt = _read(cfg.prompts_dir / "coder.md")
    prd = _read(ws.prd)
    latest = ws.latest_review()
    review_block = ""
    if latest is not None:
        review_block = (
            f"\n## Latest judge review ({latest.name}) — address every required edit:\n"
            "```\n" + _read(latest) + "\n```\n"
        )
    # A user change request (follow-up .md on a finished run) is TOP priority.
    change_block = ""
    if ws.change_request.exists():
        change_block = (
            "\n## USER CHANGE REQUEST (HIGHEST PRIORITY — this is why you are running again)\n"
            "The human reviewed the finished paper and asked for these changes. Apply them\n"
            "to the existing draft (keep what works, change what they asked), then keep the\n"
            "results honest and reproducible:\n```\n" + _read(ws.change_request) + "\n```\n"
        )
    goal = cfg.get("coder", "goal_prompt", default="/goal")
    exp_env = experiment_env(cfg, model_override=resolve_model(cfg, ws.root))
    api_block = _api_block(cfg, exp_env)
    body = (
        f"## Goal directive\n{goal}\n\n"
        "## Your workspace\n"
        f"You are launched in `{ws.workspace}` with full permissions CONFINED HERE.\n"
        "Produce paper_draft.tex, repro.sh, BUILD_LOG.md, results/, figures/, src/.\n\n"
        + change_block + api_block +
        "## PRD.md (your contract — read from ../PRD.md, reproduced here)\n```\n" + prd + "\n```\n"
        + review_block
    )
    prompt = _compose("coder", role_prompt, body)
    timeout = cfg.caps["wall_clock_per_build_min"] * 60
    bypass = cfg.get("isolation", "mode", default="dir") == "container"
    return adapter.run(
        role="coder",
        prompt=prompt,
        workdir=ws.workspace,
        sandbox=cfg.sandbox("coder"),
        output_last_message=ws.logs / f"build_iter{iteration}.final.txt",
        logs_prefix=ws.logs / f"build_iter{iteration}",
        timeout_s=timeout,
        model=cfg.model("coder"),
        resume_last=resume_last,
        skip_git_repo_check=False,  # workspace is a git repo
        env_overrides=exp_env,      # OpenRouter key/model for LLM-API experiments
        abort_sentinel=ws.abort_sentinel,
    )


def _api_block(cfg: Config, exp_env: dict) -> str:
    if not exp_env:
        return ""
    model = exp_env.get("OPENROUTER_DEFAULT_MODEL", "")
    base = exp_env.get("OPENROUTER_BASE_URL", "")
    return (
        "## LLM API available for experiments\n"
        "If your experiment needs to call an LLM, an OpenAI-compatible API is already\n"
        "configured in your ENVIRONMENT — do NOT hardcode or print the key:\n"
        f"  - `OPENROUTER_API_KEY` (also exported as `OPENAI_API_KEY`)\n"
        f"  - base URL `{base}` (also `OPENAI_BASE_URL`)\n"
        f"  - **default model: `{model}`** (use this unless the PRD says otherwise)\n"
        "Read the key from the environment in your code and in `repro.sh` (e.g.\n"
        "`os.environ['OPENROUTER_API_KEY']`); never write it into any file or commit it.\n"
        "The judge re-runs repro.sh with the same env, so keep API calls deterministic\n"
        "where possible (fixed prompts, temperature 0, cache responses to results/).\n\n"
    )


def run_judge(adapter: CodexAdapter, cfg: Config, ws: RunWorkspace, iteration: int) -> CodexResult:
    role_prompt = _read(cfg.prompts_dir / "judge.md")
    prd = _read(ws.prd)
    venues = cfg.venues
    venue_lines = "\n".join(
        f"  - {v.get('name')} (page_limit={v.get('page_limit')}, scope={v.get('scope')})"
        for v in venues
    )
    body = (
        "## Your working directory\n"
        f"You are launched in `{ws.root}`. The paper + artifacts are under `workspace/`.\n"
        f"Re-run the repro: `cd workspace && bash repro.sh`.\n"
        f"Write your review to `reviews/JUDGE_REVIEW_iter{iteration}.md` and end your\n"
        "final message with the `VERDICT:` line.\n\n"
        f"## This is iteration {iteration}.\n\n"
        f"## Target venues (no exact fit -> HOLD)\n{venue_lines}\n\n"
        "## PRD.md (the contract)\n```\n" + prd + "\n```\n"
    )
    prompt = _compose("judge", role_prompt, body)
    timeout = cfg.caps["wall_clock_per_judge_min"] * 60
    return adapter.run(
        role="judge",
        prompt=prompt,
        workdir=ws.root,
        sandbox=cfg.sandbox("judge"),
        output_last_message=ws.logs / f"judge_iter{iteration}.final.txt",
        logs_prefix=ws.logs / f"judge_iter{iteration}",
        timeout_s=timeout,
        model=cfg.model("judge"),
        skip_git_repo_check=True,
        # judge re-runs repro.sh -> may call the LLM API (same model the coder used)
        env_overrides=experiment_env(cfg, model_override=resolve_model(cfg, ws.root)),
        abort_sentinel=ws.abort_sentinel,
    )
