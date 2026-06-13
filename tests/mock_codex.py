#!/usr/bin/env python3
"""Mock codex binary. Mimics `codex exec` closely enough that the REAL adapter and
orchestrator subprocess path runs unchanged — only the codex binary is swapped.

It parses the same flags, reads the prompt from stdin (the `-` positional), detects
the role from the `autoscience role:` marker, and behaves per MOCK_CODEX_SCENARIO:

  happy       planner->good PRD; coder->paper F1 matches repro; judge->PASS
  fabricated  coder claims F1=1.00 but repro yields 0.85 -> judge REVISE (Crit-1);
              on revise the coder fixes it -> judge PASS  (proves the gate + loop)
  hold        numbers fine, but judge returns HOLD (no exact-fit venue)
  exhaust     judge ALWAYS returns REVISE -> loop hits max_iterations
  abort       planner writes the ABORT sentinel -> orchestrator kill switch fires
  hang        sleep forever -> adapter per-call timeout must SIGTERM->SIGKILL it
  noverdict   judge writes a review with NO VERDICT line -> safety path = REVISE

The judge genuinely runs `bash repro.sh` and compares its output to the paper's
claimed number; the gate is real verification, not a stub.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

TRUE_F1 = "0.85"  # what repro.sh actually produces, every time


# ---- arg parsing -------------------------------------------------------------
def parse_args(argv: list[str]) -> dict:
    a = {"resume": False, "config": [], "workdir": None, "sandbox": None,
         "output_last_message": None, "output_schema": None, "model": None,
         "json": False, "prompt_stdin": False}
    i = 0
    if i < len(argv) and argv[i] == "exec":
        i += 1
    if i < len(argv) and argv[i] == "resume":
        a["resume"] = True
        i += 1
        if i < len(argv) and argv[i] == "--last":
            i += 1
    while i < len(argv):
        tok = argv[i]
        if tok in ("-m", "--model"):
            a["model"] = argv[i + 1]; i += 2
        elif tok in ("-c", "--config"):
            a["config"].append(argv[i + 1]); i += 2
        elif tok in ("-C", "--cd"):
            a["workdir"] = argv[i + 1]; i += 2
        elif tok in ("-s", "--sandbox"):
            a["sandbox"] = argv[i + 1]; i += 2
        elif tok in ("-o", "--output-last-message"):
            a["output_last_message"] = argv[i + 1]; i += 2
        elif tok == "--output-schema":
            a["output_schema"] = argv[i + 1]; i += 2
        elif tok == "--json":
            a["json"] = True; i += 1
        elif tok in ("--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"):
            i += 1
        elif tok == "-":
            a["prompt_stdin"] = True; i += 1
        else:
            i += 1
    return a


def emit_event(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def detect_role(prompt: str) -> str:
    m = re.search(r"autoscience role:\s*(planner|coder|judge|venue_scout|concierge|batch_match)",
                  prompt, re.IGNORECASE)
    return m.group(1).lower() if m else "unknown"


def iteration_from_prompt(prompt: str) -> int:
    m = re.search(r"JUDGE_REVIEW_iter(\d+)\.md", prompt)
    if m:
        return int(m.group(1))
    m = re.search(r"iteration\s+(\d+)", prompt, re.IGNORECASE)
    return int(m.group(1)) if m else 1


def is_revising(prompt: str) -> bool:
    return "You are REVISING" in prompt or "Required edits" in prompt


# ---- role behaviors ----------------------------------------------------------
def do_planner(workdir: Path, scenario: str) -> str:
    prd = f"""# RESEARCH PRD — Mock Sparse Recovery Study

thesis:
  A tiny SINDy-style sparse recovery pipeline recovers the governing terms of a
  known synthetic system at F1={TRUE_F1} under fixed noise.

fixed requirements (MUST):
  - Synthetic data with a fixed seed; report exact per-cell counts.
  - Metric: F1 of recovered active terms vs ground truth.
  - Honesty: report partial/censored recovery rather than forcing a clean story.
  - One-command repro.sh that regenerates every number.

target result:
  A single results table with F1 and the recovered support, matching repro output.

creative latitude (coder's call):
  - Threshold/regularization choice, presentation of the support table, and any
    ablation over noise level are the coder's call.

anxiety experiment:
  Most likely rejection: "this is just least squares". Neutralize with a framing
  sentence + an existing-data sensitivity strip over the sparsity threshold (no new
  compute).

artifact release:
  - src/, results/metrics.txt, fixed seed, one-command repro.sh.

decision gate:
  - Submit only if F1 claim matches repro AND an exact-fit venue exists; else HOLD.

done when:
  - paper_draft.tex compiles within the venue page limit and every number traces to
    repro.sh.

## assumptions
  - venue inferred as AI4Science@NeurIPS (scientific ML / sparse recovery).
"""
    (workdir / "PRD.md").write_text(prd)
    if scenario == "abort":
        # Drop the kill switch AFTER a clean PRD so the orchestrator catches it
        # between phases (deterministic clean-teardown path).
        (workdir / "ABORT").write_text("mock: abort scenario\n")
    return ("Wrote PRD.md. thesis: sparse recovery at F1=%s; anxiety experiment = "
            "'just least squares' neutralized via threshold-sensitivity strip." % TRUE_F1)


def do_coder(workdir: Path, scenario: str, prompt: str) -> str:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "results").mkdir(exist_ok=True)
    (workdir / "figures").mkdir(exist_ok=True)
    (workdir / "src").mkdir(exist_ok=True)

    # Decide the CLAIMED number. repro.sh always truly yields TRUE_F1.
    if scenario == "fabricated" and not is_revising(prompt):
        claimed = "1.00"          # the deliberate fabrication on the first build
    else:
        claimed = TRUE_F1         # honest, matches repro (also the fix on revise)

    # repro.sh: genuinely computes and prints F1 deterministically.
    repro = f"""#!/usr/bin/env bash
set -euo pipefail
SEED=7
echo "SEED=$SEED"
python3 - <<'PY'
# Deterministic toy: fixed confusion counts -> F1 = 0.85 exactly.
tp, fp, fn = 17, 3, 3
precision = tp / (tp + fp)
recall = tp / (tp + fn)
f1 = 2 * precision * recall / (precision + recall)
with open("results/metrics.txt", "w") as fh:
    fh.write(f"F1={{f1:.2f}}\\n")
print(f"F1={{f1:.2f}}")
PY
"""
    (workdir / "repro.sh").write_text(repro)
    os.chmod(workdir / "repro.sh", 0o755)

    (workdir / "src" / "recover.py").write_text(
        "# toy sparse-recovery stub; real logic would live here\n"
    )

    scan_date = "2026-06-12"
    tex = _paper_tex(claimed, scan_date)
    (workdir / "paper_draft.tex").write_text(tex)

    (workdir / "BUILD_LOG.md").write_text(
        "# BUILD LOG\n\n"
        f"- Headline number: F1 (claimed in paper = {claimed}).\n"
        "- Source: results/metrics.txt, produced by repro.sh (python block).\n"
        "- repro.sh line: the `f1 = ...` computation -> `F1=...` printed to stdout.\n"
        f"- Seed: 7 (fixed and echoed by repro.sh).\n"
    )
    return f"Built paper_draft.tex (claimed F1={claimed}), repro.sh, BUILD_LOG.md."


def _paper_tex(claimed_f1: str, scan_date: str) -> str:
    # Two-column, math-dense, self-contained — compiles offline with pdflatex.
    # Mirrors the house template so mock runs exercise the real format.
    return r"""\documentclass[10pt,twocolumn]{article}
\usepackage[letterpaper,margin=0.85in,columnsep=0.28in]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{booktabs}
\usepackage[font=small,labelfont=bf]{caption}
\title{\bf Mock Sparse Recovery on a Synthetic System}
\author{autoscience coder \\ autoscience.local \\ \texttt{email not provided}}
\date{""" + scan_date + r"""}
\begin{document}
\twocolumn[
  \begin{@twocolumnfalse}
    \maketitle
    \begin{abstract}
      \noindent A fixed-seed SINDy-style sparse recovery pipeline recovers the
      active terms of a known synthetic system at $F_1=""" + claimed_f1 + r"""$
      under fixed noise. We report the scoped empirical claim only, with every
      number produced by \texttt{repro.sh}.
    \end{abstract}
    \vspace{1.0em}
  \end{@twocolumnfalse}
]

\section{Introduction}
We make a narrow scoped contribution: support recovery on one synthetic system.
In a literature scan on """ + scan_date + r""", we did NOT find an identical
fixed-seed micro-benchmark for this exact configuration. This paper's axis is
threshold sensitivity, distinct from sibling papers focused on noise scaling.

\section{Method}
Fixed seed $s=7$. Let $\mathrm{tp},\mathrm{fp},\mathrm{fn}$ be the active-term
confusion counts. Precision and recall are $P=\mathrm{tp}/(\mathrm{tp}+\mathrm{fp})$
and $R=\mathrm{tp}/(\mathrm{tp}+\mathrm{fn})$, and the support $F_1$ is
\begin{equation}
  F_1 \;=\; \frac{2PR}{P+R}
        \;=\; \frac{2\,\mathrm{tp}}{2\,\mathrm{tp}+\mathrm{fp}+\mathrm{fn}},
  \label{eq:f1}
\end{equation}
evaluated with the exact hyperparameters emitted by \texttt{repro.sh}.

\section{Results}
The recovered support yields $F_1=""" + claimed_f1 + r"""$ (Table~\ref{tab:headline}),
written to \texttt{results/metrics.txt}.
\begin{table}[t]
  \centering
  \caption{Headline result; value produced by \texttt{repro.sh}.}
  \label{tab:headline}
  \small
  \begin{tabular}{lc}
    \toprule
    Quantity & Value \\
    \midrule
    Support $F_1$ & """ + claimed_f1 + r""" \\
    \bottomrule
  \end{tabular}
\end{table}

\section{Checks and Robustness}
Controls pass; the result is seed-independent by construction, and a threshold
strip is provided as an existing-data sensitivity check.

\section{Discussion and Limitations}
We do NOT claim universality. This is a single-system micro-benchmark; partial or
censored recovery is reported as a finding rather than smoothed away.

\section{Conclusion}
A reproducible micro-benchmark for sparse support recovery.

\section*{Reproducibility statement}
Seed $s=7$; per-cell counts $\mathrm{tp}=17,\mathrm{fp}=3,\mathrm{fn}=3$;
one command: \texttt{bash repro.sh}.

\begin{thebibliography}{1}
\bibitem{sindy} Brunton et al., Discovering governing equations, PNAS 2016.
\end{thebibliography}
\end{document}
"""


def do_judge(run_dir: Path, scenario: str, iteration: int) -> str:
    workspace = run_dir / "workspace"
    reviews = run_dir / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)

    # Genuinely re-run the repro and read the observed number.
    observed = _run_repro(workspace)
    claimed = _claimed_f1_from_tex(workspace / "paper_draft.tex")

    crit1_fail = (observed is None) or (claimed is None) or (
        abs(float(observed) - float(claimed)) > 1e-9)

    if scenario == "hold":
        verdict = "HOLD"
        body = ("Numbers verify, but there is no exact-fit venue this cycle. "
                "Holding rather than downgrading into a marginal venue.")
    elif scenario == "exhaust":
        verdict = "REVISE"
        body = ("Structure/venue-fit not yet convincing (forced-revise scenario). "
                "Required edits below.")
    elif scenario == "noverdict":
        # Deliberately omit the VERDICT line to exercise the safety path.
        review = (f"# JUDGE REVIEW iter{iteration}\n\n"
                  f"Re-ran repro: observed F1={observed}, paper claims {claimed}.\n"
                  "No machine-readable verdict emitted (test).\n")
        (reviews / f"JUDGE_REVIEW_iter{iteration}.md").write_text(review)
        return "Judge wrote a review without a VERDICT line (noverdict scenario)."
    elif crit1_fail:
        verdict = "REVISE"
        body = ("Criterion 1 (results are real) FAILED: paper claims F1=%s but a "
                "re-run of repro.sh yields F1=%s." % (claimed, observed))
    else:
        verdict = "PASS"
        body = ("Criterion 1 clean: paper F1=%s matches repro F1=%s. Other criteria "
                "meet threshold." % (claimed, observed))

    required = ""
    if verdict == "REVISE":
        if crit1_fail:
            required = (f"\n## Required edits\n- Fix F1 claim: paper says {claimed}, "
                        f"repro produces {observed}. Make the paper match the artifact.\n")
        else:
            required = ("\n## Required edits\n- Strengthen venue-fit / structure "
                        "justification.\n")

    review = (
        f"# JUDGE REVIEW iter{iteration}\n\n"
        f"## Re-run\n`cd workspace && bash repro.sh` -> observed F1={observed}.\n"
        f"Paper claims F1={claimed}.\n\n"
        f"## Rubric\n"
        f"1. Results are real (GATE): {'FAIL' if crit1_fail else 'pass'}\n"
        f"2. No overselling: pass\n3. Reproducibility: pass\n"
        f"4. Anxiety experiment present: pass\n"
        f"5. Venue fit: {'HOLD' if scenario=='hold' else 'pass'}\n"
        f"6. Structure conforms: pass\n\n"
        f"## Notes\n{body}\n"
        f"{required}\n"
        f"VERDICT: {verdict}\n"
    )
    (reviews / f"JUDGE_REVIEW_iter{iteration}.md").write_text(review)
    return f"Judge re-ran repro (observed F1={observed}, claimed {claimed}).\nVERDICT: {verdict}"


def do_venue_scout(scenario: str) -> str:
    """Canned, schema-shaped venue list across tiers (for fast plumbing tests)."""
    venues = [
        {"name": "AI4Science Workshop", "kind": "workshop", "tier": "1",
         "host": "NeurIPS 2026", "url": "https://ai4sciencecommunity.github.io",
         "cfp_url": "https://ai4sciencecommunity.github.io/neurips2026.html",
         "deadline": "2026-09-20", "deadline_verified": True,
         "deadline_source": "fetched CFP page", "archival": "non-archival",
         "fit_score": 88, "fit_reason": "scientific ML + reproducibility is squarely in scope",
         "accept_odds": 45, "odds_reason": "strong fit but competitive; honest scoping helps",
         "fixes": ["add a baseline comparison", "tighten the abstract claim"],
         "notes": "non-archival -> can still submit to a journal later"},
        {"name": "ML Reproducibility Workshop", "kind": "workshop", "tier": "2",
         "host": "ICLR 2026", "url": "https://reproml.org",
         "cfp_url": "https://reproml.org/cfp", "deadline": "2026-02-10",
         "deadline_verified": False, "deadline_source": "estimate from last year",
         "archival": "non-archival", "fit_score": 82,
         "fit_reason": "reproducibility is the paper's core",
         "accept_odds": 55, "odds_reason": "niche venue, exact-fit topic",
         "fixes": ["release the full config grid", "add a checklist table"],
         "notes": "DEADLINE UNVERIFIED"},
        {"name": "SciForDL (Science of Deep Learning)", "kind": "main-track", "tier": "2",
         "host": "standalone 2026", "url": "https://example.org/scifordl",
         "cfp_url": "https://example.org/scifordl/cfp", "deadline": "May 18, 2026",
         "deadline_verified": True, "deadline_source": "fetched CFP page",
         "archival": "archival", "fit_score": 70,
         "fit_reason": "empirical-science angle fits",
         "accept_odds": 35, "odds_reason": "archival + higher bar",
         "fixes": ["add theory framing", "more datasets"], "notes": ""},
    ]
    return json.dumps({"summary": "3 fitting venues across tiers (mock).", "venues": venues})


def do_batch_match(prompt: str) -> str:
    """Mock matcher: echo the deterministic FILENAME PRIOR the orchestrator computed,
    covering every ### FILE: listed (run_id empty if no prior)."""
    prior = {}
    m = re.search(r"## FILENAME PRIOR.*?\n(.*?)\n\n", prompt, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r"\s*-\s*(.+?)\s*->\s*(\S+)", line)
            if mm:
                prior[mm.group(1).strip()] = mm.group(2).strip()
    files = re.findall(r"### FILE:\s*(.+)", prompt)
    assigns = []
    for fn in files:
        fn = fn.strip()
        rid = prior.get(fn, "")
        assigns.append({"file": fn, "run_id": rid,
                        "confidence": 90 if rid else 0,
                        "reason": "filename match (mock)" if rid else "no match (mock)"})
    return json.dumps({"assignments": assigns})


def _run_repro(workspace: Path) -> str | None:
    import subprocess
    repro = workspace / "repro.sh"
    if not repro.exists():
        return None
    try:
        r = subprocess.run(["bash", "repro.sh"], cwd=str(workspace),
                           capture_output=True, text=True, timeout=60)
        m = re.search(r"F1=([0-9.]+)", r.stdout)
        return m.group(1) if m else None
    except subprocess.SubprocessError:
        return None


def _claimed_f1_from_tex(tex_path: Path) -> str | None:
    if not tex_path.exists():
        return None
    text = tex_path.read_text(errors="replace")
    # Match both plain `F1=0.85` and the LaTeX math form `F_1=0.85`.
    m = re.search(r"F_?1\s*=\s*([0-9.]+)", text)
    return m.group(1) if m else None


# ---- main --------------------------------------------------------------------
def main() -> int:
    args = parse_args(sys.argv[1:])
    prompt = sys.stdin.read() if args["prompt_stdin"] else ""
    scenario = os.environ.get("MOCK_CODEX_SCENARIO", "happy")
    role = detect_role(prompt)
    workdir = Path(args["workdir"] or ".").resolve()

    emit_event({"type": "session.started", "role": role, "scenario": scenario})

    if scenario == "hang":
        # Never finish; the adapter's per-call timeout must kill us.
        emit_event({"type": "item.started", "note": "hanging"})
        while True:
            time.sleep(1)

    if role == "planner":
        final = do_planner(workdir, scenario)
    elif role == "coder":
        final = do_coder(workdir, scenario, prompt)
    elif role == "judge":
        final = do_judge(workdir, scenario, iteration_from_prompt(prompt))
    elif role == "venue_scout":
        final = do_venue_scout(scenario)
    elif role == "batch_match":
        final = do_batch_match(prompt)
    else:
        final = f"unknown role; scenario={scenario}"

    emit_event({"type": "token_count",
                "usage": {"input_tokens": 120, "output_tokens": 80, "total_tokens": 200}})
    emit_event({"type": "item.completed", "last_agent_message": final})

    if args["output_last_message"]:
        Path(args["output_last_message"]).write_text(final)

    emit_event({"type": "session.completed"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
