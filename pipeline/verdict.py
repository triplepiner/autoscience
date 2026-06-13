"""Parse the judge's machine-readable verdict. We NEVER infer from prose —
we grep for an explicit `VERDICT: PASS|REVISE|HOLD` line."""
from __future__ import annotations

import re
from pathlib import Path

VALID = {"PASS", "REVISE", "HOLD"}
_LINE_RE = re.compile(r"^\s*VERDICT:\s*(PASS|REVISE|HOLD)\b", re.IGNORECASE | re.MULTILINE)


def parse_verdict(*texts: str) -> str | None:
    """Return the LAST explicit verdict found across the given texts, or None.
    Checking last-wins lets the judge restate a corrected verdict at the end."""
    found: str | None = None
    for text in texts:
        if not text:
            continue
        for m in _LINE_RE.finditer(text):
            found = m.group(1).upper()
    return found


def parse_verdict_from_files(*paths: Path) -> str | None:
    texts = []
    for p in paths:
        try:
            if p and Path(p).exists():
                texts.append(Path(p).read_text(errors="replace"))
        except OSError:
            continue
    return parse_verdict(*texts)
