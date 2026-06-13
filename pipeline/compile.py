"""COMPILE phase: turn paper_draft.tex into final/paper.pdf and check page count.

Orchestrator-driven (deterministic, no extra codex call). Prefers offline MacTeX
`pdflatex`; falls back to `tectonic`. Page count via `pdfinfo`, with a regex
fallback if poppler isn't present.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CompileResult:
    ok: bool
    pdf_path: Path | None
    engine: str | None
    page_count: int | None
    page_limit: int | None
    within_limit: bool | None
    log: str

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"compile FAILED (engine={self.engine})"
        lim = "" if self.page_limit is None else f"/{self.page_limit}"
        verdict = "OK" if self.within_limit else "OVER LIMIT"
        return f"compiled with {self.engine}: {self.page_count}{lim} pages [{verdict}]"


def _have(binname: str) -> bool:
    return shutil.which(binname) is not None


def _engines(pref: str) -> list[str]:
    if pref == "pdflatex":
        return ["pdflatex"]
    if pref == "tectonic":
        return ["tectonic"]
    # auto: offline MacTeX first, tectonic as fallback
    order = []
    if _have("pdflatex"):
        order.append("pdflatex")
    if _have("tectonic"):
        order.append("tectonic")
    return order or ["pdflatex"]


def compile_pdf(
    tex_path: Path,
    out_pdf: Path,
    engine_pref: str = "auto",
    max_passes: int = 3,
    timeout_s: float = 180,
) -> CompileResult:
    tex_path = Path(tex_path)
    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    build_dir = out_pdf.parent / "_latexbuild"
    build_dir.mkdir(parents=True, exist_ok=True)

    if not tex_path.exists():
        return CompileResult(False, None, None, None, None, None,
                             f"no tex file at {tex_path}")

    logs: list[str] = []
    for engine in _engines(engine_pref):
        produced = _run_engine(engine, tex_path, build_dir, max_passes, timeout_s, logs)
        if produced and produced.exists():
            shutil.copy2(produced, out_pdf)
            return CompileResult(
                ok=True, pdf_path=out_pdf, engine=engine,
                page_count=None, page_limit=None, within_limit=None,
                log="\n".join(logs[-40:]),
            )
    return CompileResult(False, None, None, None, None, None, "\n".join(logs[-60:]))


def _run_engine(engine, tex_path, build_dir, max_passes, timeout_s, logs) -> Path | None:
    stem = tex_path.stem
    if engine == "pdflatex":
        for i in range(max_passes):
            cmd = [
                "pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                "-output-directory", str(build_dir), str(tex_path),
            ]
            r = _run(cmd, cwd=tex_path.parent, timeout_s=timeout_s)
            logs.append(f"[pdflatex pass {i+1}] rc={r.returncode}")
            if r.returncode != 0:
                logs.append(_tail(r.stdout))
                # one bad pass can still yield a pdf on a later pass; keep going
        pdf = build_dir / f"{stem}.pdf"
        return pdf if pdf.exists() else None
    if engine == "tectonic":
        cmd = ["tectonic", "-X", "compile", "--outdir", str(build_dir),
               "--keep-logs", str(tex_path)]
        r = _run(cmd, cwd=tex_path.parent, timeout_s=timeout_s)
        logs.append(f"[tectonic] rc={r.returncode}")
        if r.returncode != 0:
            # older tectonic uses a flat CLI without the `compile` subcommand
            cmd2 = ["tectonic", "--outdir", str(build_dir), str(tex_path)]
            r = _run(cmd2, cwd=tex_path.parent, timeout_s=timeout_s)
            logs.append(f"[tectonic flat] rc={r.returncode}")
        if r.returncode != 0:
            logs.append(_tail(r.stdout + "\n" + r.stderr))
        pdf = build_dir / f"{stem}.pdf"
        return pdf if pdf.exists() else None
    return None


def _run(cmd, cwd, timeout_s):
    try:
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, returncode=124,
                                           stdout=str(e.stdout or ""), stderr="timeout")
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr=str(e))


def _tail(s: str, n: int = 1500) -> str:
    s = s or ""
    return s[-n:]


def page_count(pdf_path: Path) -> int | None:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return None
    if _have("pdfinfo"):
        try:
            r = subprocess.run(["pdfinfo", str(pdf_path)], capture_output=True,
                               text=True, timeout=30)
            m = re.search(r"^Pages:\s*(\d+)", r.stdout, re.MULTILINE)
            if m:
                return int(m.group(1))
        except subprocess.SubprocessError:
            pass
    # Fallback: count page objects in the raw PDF (rough but works for simple docs).
    try:
        data = pdf_path.read_bytes()
        counts = len(re.findall(rb"/Type\s*/Page[^s]", data))
        if counts:
            return counts
        m = re.findall(rb"/Count\s+(\d+)", data)
        if m:
            return max(int(x) for x in m)
    except OSError:
        return None
    return None


def check_page_limit(pdf_path: Path, limit: int | None) -> tuple[int | None, bool | None]:
    pc = page_count(pdf_path)
    if pc is None or limit is None:
        return pc, None
    return pc, pc <= limit
