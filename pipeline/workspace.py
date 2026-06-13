"""Run-workspace creation: the on-disk handoff bus between agents."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .logging_utils import utc_stamp


def slugify(text: str, maxlen: int = 40) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return (text[:maxlen].strip("-")) or "run"


def title_from_idea(idea_text: str, fallback: str) -> str:
    for line in idea_text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return fallback


@dataclass
class RunWorkspace:
    root: Path                # runs/<slug>-<stamp>/
    idea: Path                # idea.md (immutable copy)
    prd: Path                 # PRD.md
    workspace: Path           # coder's isolated git repo
    reviews: Path             # reviews/
    logs: Path                # logs/
    final: Path               # final/
    run_summary: Path         # run_summary.md
    abort_sentinel: Path      # ABORT

    @property
    def paper_tex(self) -> Path:
        return self.workspace / "paper_draft.tex"

    @property
    def repro_sh(self) -> Path:
        return self.workspace / "repro.sh"

    @property
    def build_log(self) -> Path:
        return self.workspace / "BUILD_LOG.md"

    @property
    def final_pdf(self) -> Path:
        return self.final / "paper.pdf"

    @property
    def change_request(self) -> Path:
        return self.root / "CHANGE_REQUEST.md"

    def review_path(self, iteration: int) -> Path:
        return self.reviews / f"JUDGE_REVIEW_iter{iteration}.md"

    def latest_review(self) -> Path | None:
        revs = sorted(self.reviews.glob("JUDGE_REVIEW_iter*.md"))
        return revs[-1] if revs else None


def create_run_workspace(runs_dir: Path, idea_path: Path, stamp: str | None = None) -> RunWorkspace:
    idea_path = Path(idea_path).resolve()
    idea_text = idea_path.read_text(errors="replace")
    title = title_from_idea(idea_text, fallback=idea_path.stem)
    stamp = stamp or utc_stamp()
    root = Path(runs_dir) / f"{slugify(title)}-{stamp}"
    root.mkdir(parents=True, exist_ok=False)

    ws = RunWorkspace(
        root=root,
        idea=root / "idea.md",
        prd=root / "PRD.md",
        workspace=root / "workspace",
        reviews=root / "reviews",
        logs=root / "logs",
        final=root / "final",
        run_summary=root / "run_summary.md",
        abort_sentinel=root / "ABORT",
    )
    for d in (ws.workspace, ws.reviews, ws.logs, ws.final):
        d.mkdir(parents=True, exist_ok=True)

    # Immutable input copy.
    shutil.copy2(idea_path, ws.idea)

    return ws


def load_run_workspace(run_root: Path) -> RunWorkspace:
    """Reconstruct a RunWorkspace for an EXISTING run dir (used to continue/revise
    a finished run without creating a new one)."""
    root = Path(run_root)
    ws = RunWorkspace(
        root=root, idea=root / "idea.md", prd=root / "PRD.md",
        workspace=root / "workspace", reviews=root / "reviews", logs=root / "logs",
        final=root / "final", run_summary=root / "run_summary.md",
        abort_sentinel=root / "ABORT",
    )
    for d in (ws.workspace, ws.reviews, ws.logs, ws.final):
        d.mkdir(parents=True, exist_ok=True)
    return ws


def init_git_repo(workspace: Path) -> bool:
    """Initialize a fresh git repo so the coder's changes are reviewable/rollbackable.
    Returns True on success. Best-effort: a failure just means we pass
    --skip-git-repo-check later."""
    workspace = Path(workspace)
    try:
        subprocess.run(
            ["git", "init", "-q"], cwd=str(workspace), check=True,
            capture_output=True, timeout=30,
        )
        # Local identity so commits inside the sandbox don't depend on global config.
        subprocess.run(["git", "config", "user.email", "coder@autoscience.local"],
                       cwd=str(workspace), check=False, capture_output=True)
        subprocess.run(["git", "config", "user.name", "autoscience-coder"],
                       cwd=str(workspace), check=False, capture_output=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
