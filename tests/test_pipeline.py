#!/usr/bin/env python3
"""Mock-codex end-to-end test suite. Exercises the FULL state machine, the
fabricated-number gate, caps, the kill switch, HOLD, REVISE_EXHAUSTED, and a REAL
LaTeX compile — in well under a minute, with zero token cost.

Only the codex BINARY is swapped (config.codex.bin -> tests/mock_codex.py); the
adapter, roles, orchestrator, verdict parser, and compiler all run for real.

Run directly:   python3 tests/test_pipeline.py
Or under pytest: pytest -q tests/test_pipeline.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.codex_adapter import CodexAdapter  # noqa: E402
from pipeline.config import load_config  # noqa: E402
from pipeline.orchestrator import run_pipeline  # noqa: E402

MOCK = ROOT / "tests" / "mock_codex.py"
IDEA = ROOT / "ideas" / "toy_sindy.md"


def _cfg(tmp: Path, scenario: str, max_iter: int = 5, allow_hold: bool = False):
    cfg = load_config(ROOT / "config.yaml")
    cfg.data["paths"]["runs_dir"] = str(tmp / "runs")
    # codex.bin must be one executable; a tiny shim forwards args+stdin to the mock.
    cfg.data["codex"]["bin"] = str(_launcher(tmp))
    cfg.data["caps"]["max_iterations"] = max_iter
    cfg.data["judge"]["allow_hold"] = allow_hold
    # Keep per-call wall clocks generous (mock is instant); we test caps via iterations.
    os.environ["MOCK_CODEX_SCENARIO"] = scenario
    return cfg


def _launcher(tmp: Path) -> Path:
    """codex.bin must be one executable. Write a tiny shell shim that calls the
    mock with the current python and forwards all args + stdin."""
    shim = tmp / "mock_codex_shim"
    shim.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "{MOCK}" "$@"\n')
    shim.chmod(0o755)
    return shim


# ---- individual checks -------------------------------------------------------
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail and not cond else ""))


def test_happy(tmp: Path) -> None:
    cfg = _cfg(tmp, "happy")
    r = run_pipeline(IDEA, cfg)
    check("happy: terminal DONE", r.terminal_state == "DONE", r.terminal_state)
    check("happy: 1 iteration", r.iterations == 1, str(r.iterations))
    check("happy: verdict PASS", r.verdict_history and r.verdict_history[-1]["verdict"] == "PASS",
          str(r.verdict_history))
    check("happy: PDF exists", bool(r.pdf_path) and Path(r.pdf_path).exists(), str(r.pdf_path))
    check("happy: within page limit", r.within_limit is True, str(r.within_limit))
    check("happy: run_summary written", r.workspace.run_summary.exists())
    if r.workspace.run_summary.exists():
        txt = r.workspace.run_summary.read_text()
        check("happy: summary names DONE", "DONE" in txt)


def test_fabricated_gate(tmp: Path) -> None:
    """M4: judge must catch a fabricated number on iter1, then PASS after the fix."""
    cfg = _cfg(tmp, "fabricated")
    r = run_pipeline(IDEA, cfg)
    verdicts = [h["verdict"] for h in r.verdict_history]
    check("fabricated: iter1 REVISE (gate caught it)",
          len(verdicts) >= 1 and verdicts[0] == "REVISE", str(verdicts))
    check("fabricated: ends PASS after fix",
          verdicts and verdicts[-1] == "PASS", str(verdicts))
    check("fabricated: terminal DONE", r.terminal_state == "DONE", r.terminal_state)
    check("fabricated: took 2 iterations", r.iterations == 2, str(r.iterations))


def test_hold_override(tmp: Path) -> None:
    """Default allow_hold=false: a judge HOLD is overridden -> compiles to DONE."""
    cfg = _cfg(tmp, "hold")  # allow_hold defaults False
    r = run_pipeline(IDEA, cfg)
    check("hold-override: terminal DONE (judges can't HOLD)", r.terminal_state == "DONE",
          r.terminal_state)
    check("hold-override: verdict recorded as PASS",
          bool(r.verdict_history) and r.verdict_history[-1]["verdict"] == "PASS",
          str(r.verdict_history))
    check("hold-override: PDF produced", bool(r.pdf_path) and Path(r.pdf_path).exists(),
          str(r.pdf_path))


def test_hold_allowed_then_submit(tmp: Path) -> None:
    """allow_hold=true restores the HOLD terminal; the held draft can still be
    compiled into a submittable PDF (the 'submit anyway' core)."""
    cfg = _cfg(tmp, "hold", allow_hold=True)
    r = run_pipeline(IDEA, cfg)
    check("hold-allowed: terminal HOLD", r.terminal_state == "HOLD", r.terminal_state)
    check("hold-allowed: no PDF yet", not r.pdf_path, str(r.pdf_path))
    from pipeline import compile as cm
    out = r.workspace.final / "paper.pdf"
    cres = cm.compile_pdf(r.workspace.paper_tex, out)
    check("submit-anyway: held draft compiles to a PDF", cres.ok and out.exists(),
          cres.summary)


def test_exhaust(tmp: Path) -> None:
    cfg = _cfg(tmp, "exhaust", max_iter=2)
    r = run_pipeline(IDEA, cfg)
    check("exhaust: terminal REVISE_EXHAUSTED", r.terminal_state == "REVISE_EXHAUSTED",
          r.terminal_state)
    check("exhaust: hit max iterations", r.iterations == 2, str(r.iterations))
    check("exhaust: no PDF", not r.pdf_path, str(r.pdf_path))


def test_abort(tmp: Path) -> None:
    cfg = _cfg(tmp, "abort")
    r = run_pipeline(IDEA, cfg)
    check("abort: terminal ABORTED", r.terminal_state == "ABORTED", r.terminal_state)
    check("abort: no PDF", not r.pdf_path, str(r.pdf_path))


def test_noverdict_safety(tmp: Path) -> None:
    """Unparseable verdict must NEVER pass; it degrades to REVISE and exhausts."""
    cfg = _cfg(tmp, "noverdict", max_iter=2)
    r = run_pipeline(IDEA, cfg)
    check("noverdict: never PASS",
          all(h["verdict"] != "PASS" for h in r.verdict_history), str(r.verdict_history))
    check("noverdict: terminal REVISE_EXHAUSTED",
          r.terminal_state == "REVISE_EXHAUSTED", r.terminal_state)


def test_continue_with_changes(tmp: Path) -> None:
    """A finished run can be revised with a follow-up change request: coder revises
    the existing draft, judge re-checks, recompiles — iteration numbering advances
    and prior iterations are not overwritten."""
    cfg = _cfg(tmp, "happy")
    r1 = run_pipeline(IDEA, cfg)
    check("continue: initial run DONE", r1.terminal_state == "DONE", r1.terminal_state)
    from pipeline.orchestrator import continue_run
    r2 = continue_run(r1.workspace.root,
                      "Add a baseline comparison vs random guessing and tighten the abstract.",
                      cfg)
    check("continue: CHANGE_REQUEST.md written",
          (r1.workspace.root / "CHANGE_REQUEST.md").exists())
    check("continue: revised run DONE again", r2.terminal_state == "DONE", r2.terminal_state)
    check("continue: iteration advanced", r2.iterations > r1.iterations,
          f"{r1.iterations} -> {r2.iterations}")
    check("continue: iter1 build log not overwritten",
          (r1.workspace.logs / "build_iter1.jsonl").exists()
          and (r1.workspace.logs / "build_iter2.jsonl").exists())


def test_model_override(tmp: Path) -> None:
    """Default is deepseek-v4-flash; a `model:` line in the .md overrides it."""
    os.environ["OPENROUTER_API_KEY"] = "sk-test-key"  # make env independent of secrets.local
    from pipeline.experiment_api import (experiment_env, model_override_from_text,
                                         resolve_model)
    check("model: parses an explicit slug",
          model_override_from_text("model: openai/gpt-4o") == "openai/gpt-4o")
    check("model: 'default' means default", model_override_from_text("model: default") is None)
    check("model: 'deepseek v4 flash' phrasing -> default",
          model_override_from_text("foo\nmodel: deepseek v4 flash\nbar") is None)
    run = tmp / "runx"; run.mkdir()
    (run / "idea.md").write_text("# x\nthesis: t\nmodel: anthropic/claude-3.5-sonnet\n")
    cfg = load_config(ROOT / "config.yaml")
    check("resolve_model reads idea.md",
          resolve_model(cfg, run) == "anthropic/claude-3.5-sonnet")
    env = experiment_env(cfg, model_override=resolve_model(cfg, run))
    check("override flows into env",
          env.get("OPENROUTER_DEFAULT_MODEL") == "anthropic/claude-3.5-sonnet"
          and env.get("OPENAI_MODEL") == "anthropic/claude-3.5-sonnet", str(env.get("OPENAI_MODEL")))
    (run / "idea.md").write_text("# x\nthesis: t\n")
    check("default is deepseek-v4-flash when no model line",
          experiment_env(cfg).get("OPENROUTER_DEFAULT_MODEL") == "deepseek/deepseek-v4-flash")
    os.environ.pop("OPENROUTER_API_KEY", None)


def test_adapter_timeout(tmp: Path) -> None:
    """M1: a hanging codex call must be SIGTERM->SIGKILL'd at the per-call timeout."""
    os.environ["MOCK_CODEX_SCENARIO"] = "hang"
    adapter = CodexAdapter(codex_bin=str(_launcher(tmp)),
                           model="gpt-5.5", reasoning_effort="xhigh",
                           service_tier="fast", grace_seconds=2, poll_seconds=0.1)
    wd = tmp / "hangwd"
    wd.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    res = adapter.run(
        role="planner",
        prompt="# autoscience role: planner\nhang please",
        workdir=wd, sandbox="workspace-write",
        output_last_message=wd / "out.txt",
        logs_prefix=wd / "hang", timeout_s=2, skip_git_repo_check=True,
    )
    dt = time.monotonic() - t0
    check("timeout: flagged timed_out", res.timed_out is True)
    check("timeout: killed within ~5s", dt < 6.0, f"{dt:.1f}s")
    check("timeout: non-zero exit", res.exit_code != 0, str(res.exit_code))


# ---- runner ------------------------------------------------------------------
def main() -> int:
    if not MOCK.exists():
        print(f"mock not found: {MOCK}")
        return 2
    tests = [
        ("happy path -> DONE + PDF", test_happy),
        ("fabricated-number GATE (M4)", test_fabricated_gate),
        ("HOLD overridden -> DONE", test_hold_override),
        ("HOLD allowed + submit-anyway", test_hold_allowed_then_submit),
        ("REVISE_EXHAUSTED cap", test_exhaust),
        ("ABORT kill switch", test_abort),
        ("noverdict safety", test_noverdict_safety),
        ("continue with changes (.md)", test_continue_with_changes),
        ("LLM model override from .md", test_model_override),
        ("adapter timeout kill (M1)", test_adapter_timeout),
    ]
    t0 = time.monotonic()
    for title, fn in tests:
        print(f"\n=== {title} ===")
        tmp = Path(tempfile.mkdtemp(prefix="autosci_test_"))
        try:
            fn(tmp)
        except Exception as e:  # noqa: BLE001
            check(f"{title}: no exception", False, repr(e))
            import traceback
            traceback.print_exc()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    dt = time.monotonic() - t0

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"RESULT: {passed}/{total} checks passed in {dt:.1f}s")
    if passed != total:
        print("\nFailures:")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name} :: {detail}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
