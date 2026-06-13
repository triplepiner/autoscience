"""Per-run concierge: a separate codex-backed agent you chat with about ONE run,
plus the unblock actions (open the codex thread in a real terminal, or inject
guidance into the coder thread headlessly).

This is intentionally distinct from the planner/coder/judge agents: the concierge
is read-only by default and exists to help Makar understand and unblock a run, not
to write the paper.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path

from .codex_adapter import CodexAdapter
from .config import Config
from .experiment_api import experiment_env, resolve_model


def _read(p: Path, n: int = 24_000) -> str:
    try:
        return p.read_text(errors="replace")[-n:] if p.exists() else ""
    except OSError:
        return ""


def load_chat(run_root: Path) -> list[dict]:
    f = run_root / "chat.jsonl"
    out = []
    if f.exists():
        for line in f.read_text(errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def _append_chat(run_root: Path, entry: dict) -> None:
    with (run_root / "chat.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\n")


def list_sessions(run_root: Path) -> list[dict]:
    f = run_root / "sessions.jsonl"
    out = []
    if f.exists():
        for line in f.read_text(errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def latest_session(run_root: Path, role: str = "coder") -> dict | None:
    sess = [s for s in list_sessions(run_root) if s.get("role") == role and s.get("session_id")]
    if not sess:
        # fall back to any session
        sess = [s for s in list_sessions(run_root) if s.get("session_id")]
    return sess[-1] if sess else None


def _run_snapshot(run_root: Path) -> str:
    """A compact, embedded snapshot so the concierge answers well at low effort
    without having to crawl the disk."""
    log = _read(run_root / "logs" / "orchestrator.log", 8000)
    log_tail = "\n".join(log.splitlines()[-40:])
    reviews = sorted((run_root / "reviews").glob("JUDGE_REVIEW_iter*.md"))
    latest_review = _read(reviews[-1], 6000) if reviews else "(none yet)"
    return (
        f"## Run directory\n{run_root}\n\n"
        f"## idea.md\n{_read(run_root / 'idea.md', 4000)}\n\n"
        f"## PRD.md (planner output)\n{_read(run_root / 'PRD.md', 8000) or '(not written yet)'}\n\n"
        f"## BUILD_LOG.md (coder)\n{_read(run_root / 'workspace' / 'BUILD_LOG.md', 5000) or '(not written yet)'}\n\n"
        f"## Latest judge review\n{latest_review}\n\n"
        f"## run_summary.md\n{_read(run_root / 'run_summary.md', 5000) or '(run not finished)'}\n\n"
        f"## Orchestrator log tail\n{log_tail}\n"
    )


CONCIERGE_SYSTEM = """You are the RUN CONCIERGE for an autonomous research-paper pipeline.
A single run goes planner -> coder -> judge in a loop until the judge says PASS/HOLD or
caps fire. You are a SEPARATE, read-only assistant: you help Makar understand this one
run's status, diagnose blocks, and decide the next action. You can read every file under
the run directory.

Be concise and concrete. When useful, tell Makar exactly which action to take:
  - "Open in terminal" to take over the codex thread interactively and resolve a block.
  - "Inject guidance" to send an instruction into the coder thread without leaving the app.
  - "Abort" to stop the run.
Never claim a result is verified unless the judge re-ran the repro. Never suggest
submitting to a venue — that is always a human action.
"""


class Concierge:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _adapter(self, reasoning: str = "low") -> CodexAdapter:
        cx = self.cfg.codex
        return CodexAdapter(
            codex_bin=cx.get("bin", "codex"),
            model=self.cfg.model("judge"),
            reasoning_effort=reasoning,
            service_tier=cx.get("service_tier", "fast"),
            extra_config=cx.get("extra_config", {}),
        )

    # -- chat ------------------------------------------------------------------
    def chat(self, run_root: Path, message: str, timeout_s: float = 180) -> dict:
        run_root = Path(run_root)
        history = load_chat(run_root)
        convo = "\n".join(
            f"{'MAKAR' if h['role'] == 'user' else 'CONCIERGE'}: {h['text']}"
            for h in history[-12:]
        )
        prompt = (
            f"# autoscience role: concierge\n{CONCIERGE_SYSTEM}\n\n"
            f"# CURRENT RUN SNAPSHOT\n{_run_snapshot(run_root)}\n\n"
            f"# CONVERSATION SO FAR\n{convo or '(this is the first message)'}\n\n"
            f"# MAKAR'S NEW MESSAGE\n{message}\n\n"
            "Reply directly to Makar. Plain text, concise."
        )
        logs_dir = run_root / "logs"
        ts = int(time.time())
        adapter = self._adapter("low")
        res = adapter.run(
            role="concierge",
            prompt=prompt,
            workdir=run_root,
            sandbox="read-only",
            output_last_message=logs_dir / f"concierge_{ts}.final.txt",
            logs_prefix=logs_dir / f"concierge_{ts}",
            timeout_s=timeout_s,
            skip_git_repo_check=True,
        )
        reply = res.final_message.strip() or (
            "(the concierge agent returned no message — "
            f"exit={res.exit_code}, timed_out={res.timed_out})"
        )
        now = time.time()
        _append_chat(run_root, {"role": "user", "text": message, "ts": now})
        _append_chat(run_root, {"role": "assistant", "text": reply, "ts": now,
                                "session_id": res.session_id})
        return {"reply": reply, "ok": res.ok, "session_id": res.session_id,
                "duration_s": res.duration_s}

    # -- inject (headless resume of a real codex thread) -----------------------
    def inject(self, run_root: Path, guidance: str, role: str = "coder",
               timeout_s: float = 1800) -> dict:
        run_root = Path(run_root)
        sess = latest_session(run_root, role)
        if not sess:
            return {"ok": False, "error": f"no resumable {role} codex session for this run "
                    "(mock runs and not-yet-started phases have none)"}
        workdir = Path(sess.get("workdir") or (run_root / "workspace"))
        ts = int(time.time())
        bypass = self.cfg.get("isolation", "mode", default="dir") == "container"
        adapter = CodexAdapter(
            codex_bin=self.cfg.codex.get("bin", "codex"),
            model=self.cfg.model("coder"),
            reasoning_effort=self.cfg.codex.get("model_reasoning_effort", "xhigh"),
            service_tier=self.cfg.codex.get("service_tier", "fast"),
            bypass_sandbox=bypass,
        )
        _append_chat(run_root, {"role": "user", "text": f"[inject -> {role} thread] {guidance}",
                                "ts": time.time()})
        res = adapter.run(
            role=f"inject-{role}",
            prompt=guidance,
            workdir=workdir,
            sandbox="danger-full-access",
            output_last_message=run_root / "logs" / f"inject_{ts}.final.txt",
            logs_prefix=run_root / "logs" / f"inject_{ts}",
            timeout_s=timeout_s,
            resume_session_id=sess["session_id"],
            env_overrides=(experiment_env(self.cfg, model_override=resolve_model(self.cfg, run_root))
                           if role == "coder" else None),
            abort_sentinel=run_root / "ABORT",
        )
        reply = res.final_message.strip() or f"(inject finished: exit={res.exit_code}, " \
            f"timed_out={res.timed_out})"
        _append_chat(run_root, {"role": "assistant", "text": f"[{role} thread replied] {reply}",
                                "ts": time.time(), "session_id": res.session_id})
        return {"ok": res.ok, "reply": reply, "session_id": res.session_id,
                "timed_out": res.timed_out}

    # -- terminal handoff ------------------------------------------------------
    def terminal_command(self, run_root: Path, role: str = "coder") -> dict:
        run_root = Path(run_root)
        sess = latest_session(run_root, role)
        workspace = run_root / "workspace"
        codex_bin = self.cfg.codex.get("bin", "codex")
        if sess and sess.get("session_id"):
            workdir = sess.get("workdir") or str(workspace)
            cmd = f"cd {shlex.quote(workdir)} && {shlex.quote(codex_bin)} resume {sess['session_id']}"
            desc = f"resume the {role} codex thread ({sess['session_id'][:8]}…)"
        else:
            workdir = str(workspace if workspace.exists() else run_root)
            cmd = f"cd {shlex.quote(workdir)} && {shlex.quote(codex_bin)}"
            desc = "open a fresh codex session in the workspace (no resumable thread found)"
        return {"command": cmd, "description": desc, "workdir": workdir,
                "session_id": sess.get("session_id") if sess else None}

    def open_terminal(self, run_root: Path, role: str = "coder") -> dict:
        run_root = Path(run_root)
        info = self.terminal_command(run_root, role)
        cmd = info["command"]
        # Preferred: write an executable .command file and `open` it. macOS runs
        # .command files in Terminal by default — NO Automation permission needed.
        script = run_root / ".open_terminal.command"
        script.write_text(
            "#!/bin/bash\n"
            "clear\n"
            "echo '── autoscience: resuming the codex thread for this run ──'\n"
            f"echo '{info['description']}'\n"
            "echo\n"
            f"{cmd}\n"
        )
        try:
            script.chmod(0o755)
        except OSError:
            pass
        opened, err = False, ""
        try:
            r = subprocess.run(["open", str(script)], capture_output=True, text=True, timeout=15)
            opened = r.returncode == 0
            err = "" if opened else (r.stderr.strip() or "`open` failed")
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            err = str(e)
        if not opened:
            # Fallback: AppleScript (may prompt for Automation permission once).
            osa = (
                'tell application "Terminal"\n'
                f'  do script "{cmd.replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"\n'
                "  activate\nend tell"
            )
            try:
                r = subprocess.run(["osascript", "-e", osa], capture_output=True, text=True, timeout=15)
                opened = r.returncode == 0
                err = "" if opened else (r.stderr.strip() or err or "osascript failed")
            except (subprocess.SubprocessError, FileNotFoundError) as e:
                err = f"{err}; osascript: {e}"
        info["opened"] = opened
        info["error"] = "" if opened else f"{err}. Copy the command and run it manually."
        return info
