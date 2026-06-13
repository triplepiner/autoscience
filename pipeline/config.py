"""Load and validate config.yaml into a typed-ish dict with sane access helpers."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "models": {"planner": "gpt-5.5", "coder": "gpt-5.5", "judge": "gpt-5.5"},
    "codex": {
        "bin": "codex",
        "model_reasoning_effort": "xhigh",
        "service_tier": "fast",
        "extra_config": {},
    },
    "caps": {
        "max_iterations": 5,
        "wall_clock_per_build_min": 180,
        "wall_clock_per_plan_min": 30,
        "wall_clock_per_judge_min": 60,
        "wall_clock_total_min": 600,
        "max_cost_usd": None,
    },
    "coder": {
        "goal_prompt": "/goal",
        "sandbox": "danger-full-access",
        "allow_subagents": True,
        "resume_on_revise": False,
    },
    "planner": {"sandbox": "workspace-write"},
    "judge": {"sandbox": "workspace-write", "spot_check_min_numbers": 3, "allow_hold": False},
    "isolation": {"mode": "dir"},
    "review_prd_before_build": False,
    "experiment_api": {
        "enabled": True,
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "deepseek/deepseek-v4-flash",
        "api_key_file": "secrets.local",
        "also_set_openai_env": True,
    },
    "compile": {"engine": "auto", "max_passes": 3},
    "venues": [
        {
            "name": "AI4Science@NeurIPS",
            "page_limit": 4,
            "style": "neurips_workshop.sty",
            "scope": ["scientific ML", "sparse recovery", "dynamical systems"],
        }
    ],
    "paths": {"runs_dir": "./runs", "role_prompts_dir": "./prompts"},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    data: dict[str, Any]
    config_path: Path
    root: Path  # directory containing config.yaml; relative paths resolve against it

    # --- convenience accessors -------------------------------------------------
    def model(self, role: str) -> str:
        return self.data["models"][role]

    @property
    def codex(self) -> dict:
        return self.data["codex"]

    @property
    def caps(self) -> dict:
        return self.data["caps"]

    def sandbox(self, role: str) -> str:
        return self.data[role]["sandbox"]

    @property
    def venues(self) -> list[dict]:
        return self.data["venues"]

    @property
    def runs_dir(self) -> Path:
        return (self.root / self.data["paths"]["runs_dir"]).resolve()

    @property
    def prompts_dir(self) -> Path:
        return (self.root / self.data["paths"]["role_prompts_dir"]).resolve()

    def get(self, *keys, default=None):
        node: Any = self.data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
    merged = _deep_merge(DEFAULTS, raw)
    return Config(data=merged, config_path=path, root=path.parent)
