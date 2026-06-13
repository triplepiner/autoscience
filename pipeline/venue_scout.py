"""Venue scout: a codex agent that searches the web for workshops and main tracks
(tier 1/2/3, big AND niche) that fit a finished paper, and rates each on fit,
acceptance odds, deadline, archival status, and what to fix to improve odds.

Honesty discipline (same spirit as the judge): deadlines must be grounded in a
fetched CFP page. Anything it can't verify is marked unverified — never invent a
date. Output is forced into a JSON schema via `codex exec --output-schema`.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .codex_adapter import CodexAdapter
from .config import Config

VENUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "venues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["workshop", "main-track", "journal", "other"]},
                    "tier": {"type": "string", "enum": ["1", "2", "3", "unknown"]},
                    "host": {"type": "string"},
                    "url": {"type": "string"},
                    "cfp_url": {"type": "string"},
                    "deadline": {"type": "string"},
                    "deadline_verified": {"type": "boolean"},
                    "deadline_source": {"type": "string"},
                    "archival": {"type": "string", "enum": ["archival", "non-archival", "unknown"]},
                    "fit_score": {"type": "integer"},
                    "fit_reason": {"type": "string"},
                    "accept_odds": {"type": "integer"},
                    "odds_reason": {"type": "string"},
                    "fixes": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
                "required": ["name", "kind", "tier", "host", "url", "cfp_url", "deadline",
                             "deadline_verified", "deadline_source", "archival", "fit_score",
                             "fit_reason", "accept_odds", "odds_reason", "fixes", "notes"],
            },
        },
    },
    "required": ["summary", "venues"],
}

SCOUT_SYSTEM = """You are the VENUE SCOUT for a finished research paper. Search the
ACTUAL WEB (browse / fetch pages) for venues this paper could be submitted to, and
rank them. Cover the full spectrum:
  - tier 1 (e.g. NeurIPS / ICML / ICLR main tracks and their flagship workshops),
  - tier 2 (strong specialized conferences and well-known workshops),
  - tier 3 (smaller / niche / regional workshops and symposia) —
include BIG names AND small/niche venues; do not only list the famous ones.

For EVERY venue return: name, kind (workshop/main-track/journal), tier, host event,
homepage url, CFP url, paper-submission deadline, whether you VERIFIED that deadline
by fetching the CFP page (deadline_verified true/false + deadline_source = the URL or
quoted text), archival vs non-archival (workshops are often non-archival — check),
a fit_score 0-100 with a one-line reason, an honest accept_odds 0-100 with a reason,
and a concrete list of FIXES that would raise the odds for THAT venue.

HONESTY (hard rule): never invent a deadline. If you could not fetch a real CFP page,
set deadline to your best estimate, deadline_verified=false, and say so in notes.
A wrong deadline is worse than "unknown". Prefer venues whose scope genuinely matches
the paper; be realistic about odds, not flattering.
"""


def _read(p: Path, n: int = 16_000) -> str:
    try:
        return p.read_text(errors="replace")[:n] if p.exists() else ""
    except OSError:
        return ""


def _paper_context(run_root: Path) -> str:
    tex = _read(run_root / "workspace" / "paper_draft.tex", 14_000)
    prd = _read(run_root / "PRD.md", 8_000)
    idea = _read(run_root / "idea.md", 4_000)
    title, abstract = _title_abstract(tex)
    return (
        f"## Paper title\n{title or '(unknown — infer from the draft)'}\n\n"
        f"## Abstract\n{abstract or '(see draft)'}\n\n"
        f"## idea.md\n{idea}\n\n"
        f"## PRD.md\n{prd}\n\n"
        f"## paper_draft.tex (truncated)\n{tex}\n"
    )


def _title_abstract(tex: str) -> tuple[str, str]:
    title = ""
    m = re.search(r"\\title\{(?:\\bf\s*)?(.+?)\}", tex, re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
    abstract = ""
    m = re.search(r"\\begin\{abstract\}(.+?)\\end\{abstract\}", tex, re.DOTALL)
    if m:
        abstract = re.sub(r"\s+", " ", re.sub(r"\\noindent", "", m.group(1))).strip()
    return title, abstract


def load_venues(run_root: Path) -> dict:
    f = Path(run_root) / "venues.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


class VenueScout:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def scout(self, run_root: Path, timeout_s: float = 900) -> dict:
        run_root = Path(run_root)
        scratch = run_root / "scout"
        scratch.mkdir(parents=True, exist_ok=True)
        schema_path = scratch / "venue_schema.json"
        schema_path.write_text(json.dumps(VENUE_SCHEMA))

        prompt = (
            f"# autoscience role: venue_scout\n{SCOUT_SYSTEM}\n\n"
            f"# THE PAPER\n{_paper_context(run_root)}\n\n"
            "Now browse the web and return the ranked venue list as JSON matching the "
            "required schema. Aim for 8-15 venues across tiers, sorted best-fit first."
        )
        cx = self.cfg.codex
        bypass = self.cfg.get("isolation", "mode", default="dir") == "container"
        adapter = CodexAdapter(
            codex_bin=cx.get("bin", "codex"),
            model=self.cfg.model("judge"),
            reasoning_effort=cx.get("model_reasoning_effort", "xhigh"),
            service_tier=cx.get("service_tier", "fast"),
            bypass_sandbox=bypass,
        )
        ts = int(time.time())
        res = adapter.run(
            role="venue_scout",
            prompt=prompt,
            workdir=scratch,
            sandbox="danger-full-access",   # needs network to browse CFP pages
            output_last_message=run_root / "logs" / f"scout_{ts}.final.txt",
            logs_prefix=run_root / "logs" / f"scout_{ts}",
            timeout_s=timeout_s,
            output_schema=schema_path,
            skip_git_repo_check=True,
            abort_sentinel=run_root / "ABORT",
        )
        data = _parse_json(res.final_message)
        if data is None:
            data = {"summary": f"scout returned no parseable JSON (exit={res.exit_code}, "
                    f"timed_out={res.timed_out}).", "venues": []}
        data["_scouted_ts"] = time.time()
        data["_ok"] = res.ok
        # sort best-fit first, defensively
        try:
            data["venues"].sort(key=lambda v: -int(v.get("fit_score") or 0))
        except (KeyError, TypeError, ValueError):
            pass
        (run_root / "venues.json").write_text(json.dumps(data, indent=2))
        return data


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # pull the first {...} block
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None
