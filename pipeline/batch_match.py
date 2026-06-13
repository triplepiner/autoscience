"""Batch revise: match a zip of markdown files to existing threads.

Given a folder of uploaded `.md` files and the current research threads, decide which
file revises which thread. A deterministic filename prior (idea-NN / slug overlap) is
computed first and handed to a codex agent, which reads each file's content vs each
thread's title/thesis and produces the final file -> thread assignment with a reason.
The human reviews/edits the table before launching, so a wrong guess is cheap.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .codex_adapter import CodexAdapter
from .config import Config
from .workspace import slugify, title_from_idea

MATCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "file": {"type": "string"},
                    "run_id": {"type": "string"},
                    "confidence": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["file", "run_id", "confidence", "reason"],
            },
        }
    },
    "required": ["assignments"],
}


def list_threads(runs_dir: Path) -> list[dict]:
    out = []
    for d in sorted(Path(runs_dir).iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        idea = d / "idea.md"
        if not idea.exists():
            continue
        text = idea.read_text(errors="replace")
        title = title_from_idea(text, fallback=d.name)
        m = re.search(r"^\s*thesis:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
        thesis = (m.group(1).strip() if m else "")[:300]
        out.append({"run_id": d.name, "title": title, "thesis": thesis,
                    "slug": slugify(title)})
    return out


def _idea_num(s: str) -> str | None:
    m = re.search(r"idea[-_ ]?0*(\d+)", s.lower())
    return m.group(1) if m else None


def heuristic_match(files: list[dict], threads: list[dict]) -> dict[str, str]:
    """Filename -> run_id strong prior (idea-NN match, then slug token overlap)."""
    prior: dict[str, str] = {}
    for f in files:
        name = f["name"].lower()
        num = _idea_num(name)
        if num:
            for t in threads:
                if _idea_num(t["run_id"]) == num:
                    prior[f["name"]] = t["run_id"]
                    break
        if f["name"] in prior:
            continue
        ftok = set(re.findall(r"[a-z]{4,}", name + " " + f.get("content", "")[:200].lower()))
        best, best_ov = None, 0
        for t in threads:
            ov = len(ftok & set(re.findall(r"[a-z]{4,}", t["slug"])))
            if ov > best_ov:
                best, best_ov = t["run_id"], ov
        if best and best_ov >= 2:
            prior[f["name"]] = best
    return prior


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


class BatchMatcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def match(self, files: list[dict], threads: list[dict], work_dir: Path,
              timeout_s: float = 300) -> list[dict]:
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        prior = heuristic_match(files, threads)
        thread_lines = "\n".join(
            f"- {t['run_id']} | {t['title']} | thesis: {t['thesis']}" for t in threads)
        prior_lines = "\n".join(f"- {k} -> {v}" for k, v in prior.items()) or "(none)"
        file_blocks = "\n\n".join(
            f"### FILE: {f['name']}\n{f.get('content','')[:1500]}" for f in files)
        prompt = (
            "# autoscience role: batch_match\n"
            "You map each uploaded revision markdown FILE to the existing research THREAD it "
            "belongs to (the paper it should revise). Decide from the filename AND the file's "
            "content (its topic/thesis) vs each thread's title and thesis. If a file matches no "
            "thread, set run_id to an empty string. Every file must appear exactly once.\n\n"
            f"## THREADS (run_id | title | thesis)\n{thread_lines}\n\n"
            f"## FILENAME PRIOR (deterministic guesses — verify, override if content disagrees)\n{prior_lines}\n\n"
            f"## FILES\n{file_blocks}\n\n"
            "Return JSON {assignments:[{file, run_id, confidence 0-100, reason (short)}]}."
        )
        cx = self.cfg.codex
        adapter = CodexAdapter(
            codex_bin=cx.get("bin", "codex"),
            model=self.cfg.model("judge"),
            reasoning_effort="medium",
            service_tier=cx.get("service_tier", "fast"),
        )
        schema_path = work_dir / "match_schema.json"
        schema_path.write_text(json.dumps(MATCH_SCHEMA))
        ts = int(time.time())
        res = adapter.run(
            role="batch_match", prompt=prompt, workdir=work_dir, sandbox="read-only",
            output_last_message=work_dir / f"match_{ts}.final.txt",
            logs_prefix=work_dir / f"match_{ts}", timeout_s=timeout_s,
            output_schema=schema_path, skip_git_repo_check=True,
        )
        data = _parse_json(res.final_message) or {}
        assigns = [a for a in data.get("assignments", []) if isinstance(a, dict)]
        valid_ids = {t["run_id"] for t in threads}
        # normalize + ensure every file present exactly once
        by_file: dict[str, dict] = {}
        for a in assigns:
            fn = a.get("file")
            if not fn:
                continue
            rid = a.get("run_id") or ""
            if rid and rid not in valid_ids:
                rid = prior.get(fn, "")  # codex hallucinated an id -> fall back
            by_file[fn] = {"file": fn, "run_id": rid,
                           "confidence": int(a.get("confidence") or 0),
                           "reason": str(a.get("reason") or "")}
        for f in files:
            if f["name"] not in by_file:
                rid = prior.get(f["name"], "")
                by_file[f["name"]] = {
                    "file": f["name"], "run_id": rid,
                    "confidence": 40 if rid else 0,
                    "reason": "filename prior" if rid else "no confident match — assign manually",
                }
        return [by_file[f["name"]] for f in files]
