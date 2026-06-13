"""LLM API plumbing for research experiments that need one.

Loads the OpenRouter key from the gitignored secrets file (or the environment)
and returns the env vars to inject into the coder + judge codex subprocesses, so
their experiment code can call an LLM without the key ever touching the repo or the
paper. Mirrors into OPENAI_* so the stock `openai` SDK routes to OpenRouter unchanged.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .config import Config

# A user can override the default model from their .md with a line like:
#   model: openai/gpt-4o      (or  llm_model: ...  /  experiment_model: ...)
_MODEL_RE = re.compile(
    r"^\s*(?:experiment_model|llm[_ ]?model|model)\s*[:=]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE)


def model_override_from_text(text: str | None) -> str | None:
    if not text:
        return None
    m = _MODEL_RE.search(text)
    if not m:
        return None
    v = m.group(1).strip().strip("`\"' ")
    if not v or v.lower() in ("auto", "default", "none", "deepseek v4 flash",
                              "deepseek-v4-flash", "deepseek flash v4"):
        return None  # these all mean "use the default"
    return v


def resolve_model(cfg: Config, run_root) -> str | None:
    """User-stated model wins, checked in the user-authored files only (not the
    LLM-written PRD, to avoid false matches in prose). CHANGE_REQUEST > idea.md."""
    for fn in ("CHANGE_REQUEST.md", "idea.md"):
        p = Path(run_root) / fn
        if p.exists():
            mv = model_override_from_text(p.read_text(errors="replace"))
            if mv:
                return mv
    return None


def load_api_key(cfg: Config) -> str | None:
    # Environment wins (lets a user override without editing files).
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key.strip()
    fname = cfg.get("experiment_api", "api_key_file", default="secrets.local")
    if not fname:
        return None
    p = (cfg.root / fname)
    if not p.exists():
        return None
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("OPENROUTER_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
        if "=" not in line and line.startswith("sk-"):
            return line  # a bare key on its own line
    return None


def experiment_env(cfg: Config, model_override: str | None = None) -> dict[str, str]:
    """Env vars to hand the coder/judge so experiments can call the LLM API.
    Defaults to deepseek/deepseek-v4-flash; `model_override` (from the user's .md)
    wins. Returns {} when disabled or no key is available."""
    ea = cfg.get("experiment_api", default={}) or {}
    if not ea.get("enabled", True):
        return {}
    key = load_api_key(cfg)
    if not key:
        return {}
    base = ea.get("base_url", "https://openrouter.ai/api/v1")
    model = model_override or ea.get("default_model", "deepseek/deepseek-v4-flash")
    env = {
        "OPENROUTER_API_KEY": key,
        "OPENROUTER_BASE_URL": base,
        "OPENROUTER_DEFAULT_MODEL": model,
    }
    if ea.get("also_set_openai_env", True):
        env["OPENAI_API_KEY"] = key
        env["OPENAI_BASE_URL"] = base
        env["OPENAI_MODEL"] = model
    return env


def is_available(cfg: Config) -> bool:
    return bool(experiment_env(cfg))
