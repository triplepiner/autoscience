"""Versioned, thread-named PDFs.

Every produced PDF is kept as `final/<thread-slug>-v{N}.pdf` (N = the push it came
from: push 1 = original submission, push 2 = first Submit-Changes revision, ...).
Nothing is overwritten and nothing is deleted. `versions.json` records the history.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .workspace import slugify, title_from_idea


def thread_slug(run_root: Path) -> str:
    """The thread's name as a filename-safe slug (from the idea title)."""
    run_root = Path(run_root)
    idea = run_root / "idea.md"
    title = run_root.name
    if idea.exists():
        title = title_from_idea(idea.read_text(errors="replace"), fallback=run_root.name)
    return slugify(title, maxlen=60)


def push_count(run_root: Path) -> int:
    """Which push we're on: 1 = original, +1 per Submit-Changes revision."""
    reviews = Path(run_root) / "reviews"
    n = len(list(reviews.glob("USER_CHANGE_iter*.md"))) if reviews.exists() else 0
    return 1 + n


def kind_for(version: int) -> str:
    return "initial" if version <= 1 else f"revision {version - 1}"


def version_pdf_path(run_root: Path, version: int | None = None) -> Path:
    run_root = Path(run_root)
    v = version if version is not None else push_count(run_root)
    return run_root / "final" / f"{thread_slug(run_root)}-v{v}.pdf"


def load_versions(run_root: Path) -> list[dict]:
    f = Path(run_root) / "versions.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except json.JSONDecodeError:
            return []
    return []


def _save_versions(run_root: Path, versions: list[dict]) -> None:
    (Path(run_root) / "versions.json").write_text(json.dumps(versions, indent=2))


def record_version(run_root: Path, version: int, *, pdf_name: str | None,
                   pages: int | None, retained: bool, kind: str | None = None,
                   note: str = "", ts: float | None = None) -> None:
    versions = load_versions(run_root)
    versions = [v for v in versions if v.get("version") != version]  # replace same version
    versions.append({
        "version": version,
        "kind": kind or kind_for(version),
        "pdf": pdf_name,
        "pages": pages,
        "retained": retained,
        "note": note,
        "ts": ts if ts is not None else time.time(),
    })
    versions.sort(key=lambda v: v.get("version", 0))
    _save_versions(run_root, versions)


def latest_pdf(run_root: Path) -> Path | None:
    """Newest retained version PDF on disk (versioned first, legacy paper.pdf last)."""
    run_root = Path(run_root)
    versions = [v for v in load_versions(run_root)
                if v.get("retained") and v.get("pdf")]
    if versions:
        p = run_root / "final" / versions[-1]["pdf"]
        if p.exists():
            return p
    # any versioned pdf on disk
    cands = sorted((run_root / "final").glob("*-v*.pdf")) if (run_root / "final").exists() else []
    if cands:
        return cands[-1]
    legacy = run_root / "final" / "paper.pdf"
    return legacy if legacy.exists() else None


def public_versions(run_root: Path) -> list[dict]:
    """Version list for the dashboard, with file URLs for the ones still on disk."""
    run_root = Path(run_root)
    out = []
    for v in load_versions(run_root):
        pdf = v.get("pdf")
        exists = bool(pdf) and (run_root / "final" / pdf).exists()
        out.append({
            "version": v.get("version"),
            "kind": v.get("kind"),
            "pages": v.get("pages"),
            "retained": bool(v.get("retained")) and exists,
            "note": v.get("note", ""),
            "url": f"/files/{run_root.name}/final/{pdf}" if exists else None,
            "filename": pdf,
            "ts": v.get("ts"),
        })
    return out


def migrate_legacy(run_root: Path) -> bool:
    """One-time: rename an old `final/paper.pdf` to the versioned thread-named scheme
    and seed versions.json (marking pushes whose PDFs were overwritten pre-versioning
    as not-retained). Returns True if it migrated. A rename, never a delete."""
    run_root = Path(run_root)
    final = run_root / "final"
    legacy = final / "paper.pdf"
    if (run_root / "versions.json").exists():
        return False
    if not legacy.exists():
        return False
    from . import compile as compile_mod
    cur = push_count(run_root)
    dest = final / f"{thread_slug(run_root)}-v{cur}.pdf"
    try:
        legacy.rename(dest)
    except OSError:
        return False
    pages = compile_mod.page_count(dest)
    ts = dest.stat().st_mtime
    versions = []
    for v in range(1, cur):  # earlier pushes existed but their PDFs were overwritten
        versions.append({"version": v, "kind": kind_for(v), "pdf": None, "pages": None,
                         "retained": False, "note": "overwritten before versioning", "ts": None})
    versions.append({"version": cur, "kind": kind_for(cur), "pdf": dest.name,
                     "pages": pages, "retained": True, "note": "", "ts": ts})
    _save_versions(run_root, versions)
    return True
