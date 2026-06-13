"""Codex CLI adapter — the ONLY module that knows codex-exec flags.

Pinned against `codex exec --help` for codex-cli 0.128.0:
    codex exec [OPTIONS] [PROMPT]
      -m, --model <MODEL>
      -c, --config <key=value>        (repeatable; value parsed as TOML, else literal)
      -C, --cd <DIR>                  working root for the agent
      -s, --sandbox <read-only|workspace-write|danger-full-access>
          --dangerously-bypass-approvals-and-sandbox   (full perms, no sandbox)
          --skip-git-repo-check
          --json                      JSONL events on stdout
      -o, --output-last-message <FILE>  final agent message written here
          --output-schema <FILE>      JSON Schema for the final response
      codex exec resume --last [PROMPT]   resume most recent session

Notes that drove the design:
  * There is NO `--yolo` in 0.128.0. Full perms = `-s danger-full-access`
    (we also support `--dangerously-bypass-approvals-and-sandbox` via a flag).
  * The prompt is passed on STDIN with a `-` positional, so arbitrarily long
    prompts (idea + PRD + review) never hit ARG_MAX.
  * With `--json`, stdout is JSONL events; the human-readable final message is
    captured via `-o`. We tee stdout->.jsonl and stderr->.stderr.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CodexResult:
    role: str
    exit_code: int
    final_message: str
    stdout_path: Path
    stderr_path: Path
    jsonl_events: list[dict] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None     # codex thread/conversation UUID, for resume
    timed_out: bool = False
    aborted: bool = False
    duration_s: float = 0.0
    argv: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.aborted


class CodexAdapter:
    """Builds argv, spawns the subprocess, enforces a hard per-call timeout
    (SIGTERM -> grace -> SIGKILL), tees logs, returns a CodexResult."""

    def __init__(
        self,
        codex_bin: str = "codex",
        model: str | None = None,
        reasoning_effort: str | None = None,
        service_tier: str | None = None,
        extra_config: dict[str, Any] | None = None,
        bypass_sandbox: bool = False,
        grace_seconds: float = 10.0,
        poll_seconds: float = 0.2,
    ):
        self.codex_bin = codex_bin
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.service_tier = service_tier
        self.extra_config = extra_config or {}
        self.bypass_sandbox = bypass_sandbox
        self.grace_seconds = grace_seconds
        self.poll_seconds = poll_seconds

    # -- argv construction -----------------------------------------------------
    def _config_overrides(self) -> list[str]:
        pairs: list[tuple[str, str]] = []
        if self.reasoning_effort:
            pairs.append(("model_reasoning_effort", self.reasoning_effort))
        if self.service_tier:
            pairs.append(("service_tier", self.service_tier))
        for k, v in self.extra_config.items():
            pairs.append((k, str(v)))
        out: list[str] = []
        for k, v in pairs:
            out += ["-c", f'{k}="{v}"' if not _is_tomlish(v) else f"{k}={v}"]
        return out

    def build_argv(
        self,
        *,
        workdir: Path,
        sandbox: str,
        output_last_message: Path,
        skip_git_repo_check: bool,
        output_schema: Path | None,
        resume_last: bool,
        model: str | None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        argv: list[str] = [self.codex_bin, "exec"]
        resuming = resume_last or bool(resume_session_id)
        if resume_session_id:
            argv += ["resume", resume_session_id]
        elif resume_last:
            argv += ["resume", "--last"]
        eff_model = model or self.model
        if eff_model:
            argv += ["-m", eff_model]
        argv += self._config_overrides()
        # `resume` keeps the session's recorded cwd + sandbox: it does NOT accept
        # -C or -s. Only set those on a fresh run.
        if not resuming:
            argv += ["-C", str(workdir)]
            if self.bypass_sandbox and sandbox == "danger-full-access":
                argv += ["--dangerously-bypass-approvals-and-sandbox"]
            else:
                argv += ["-s", sandbox]
        elif self.bypass_sandbox and sandbox == "danger-full-access":
            argv += ["--dangerously-bypass-approvals-and-sandbox"]
        argv += ["--json", "-o", str(output_last_message)]
        if skip_git_repo_check:
            argv += ["--skip-git-repo-check"]
        if output_schema is not None:
            argv += ["--output-schema", str(output_schema)]
        argv += ["-"]  # read the prompt from stdin
        return argv

    # -- run -------------------------------------------------------------------
    def run(
        self,
        *,
        role: str,
        prompt: str,
        workdir: Path,
        sandbox: str,
        output_last_message: Path,
        logs_prefix: Path,
        timeout_s: float,
        model: str | None = None,
        resume_last: bool = False,
        resume_session_id: str | None = None,
        skip_git_repo_check: bool = False,
        output_schema: Path | None = None,
        env_overrides: dict[str, str] | None = None,
        abort_sentinel: Path | None = None,
    ) -> CodexResult:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        output_last_message = Path(output_last_message)
        output_last_message.parent.mkdir(parents=True, exist_ok=True)
        logs_prefix.parent.mkdir(parents=True, exist_ok=True)
        stdout_path = Path(f"{logs_prefix}.jsonl")
        stderr_path = Path(f"{logs_prefix}.stderr")

        argv = self.build_argv(
            workdir=workdir,
            sandbox=sandbox,
            output_last_message=output_last_message,
            skip_git_repo_check=skip_git_repo_check,
            output_schema=output_schema,
            resume_last=resume_last,
            resume_session_id=resume_session_id,
            model=model,
        )

        env = os.environ.copy()
        if env_overrides:
            env.update(env_overrides)

        # Ensure -o file starts empty so we can detect non-writes.
        try:
            output_last_message.write_text("")
        except OSError:
            pass

        timed_out = False
        aborted = False
        start = time.monotonic()
        with stdout_path.open("wb") as out_f, stderr_path.open("wb") as err_f:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=out_f,
                stderr=err_f,
                env=env,
                cwd=str(workdir),
                # New process group so we can signal the whole tree on kill.
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            try:
                if proc.stdin is not None:
                    proc.stdin.write(prompt.encode("utf-8"))
                    proc.stdin.close()
            except BrokenPipeError:
                pass

            deadline = start + timeout_s
            while True:
                if proc.poll() is not None:
                    break
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    self._kill_tree(proc)
                    break
                if abort_sentinel is not None and abort_sentinel.exists():
                    aborted = True
                    self._kill_tree(proc)
                    break
                time.sleep(self.poll_seconds)

        duration = time.monotonic() - start
        exit_code = proc.returncode if proc.returncode is not None else -1

        events = _parse_jsonl(stdout_path)
        usage = _extract_usage(events)
        final_message = _read_final_message(output_last_message, events)
        session_id = _extract_session_id(events)

        return CodexResult(
            role=role,
            exit_code=exit_code,
            final_message=final_message,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            jsonl_events=events,
            usage=usage,
            session_id=session_id,
            timed_out=timed_out,
            aborted=aborted,
            duration_s=duration,
            argv=argv,
        )

    def _kill_tree(self, proc: subprocess.Popen) -> None:
        """SIGTERM the process group, wait grace, then SIGKILL."""
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=self.grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=self.grace_seconds)
        except subprocess.TimeoutExpired:
            pass


# -- helpers -------------------------------------------------------------------
def _is_tomlish(v: str) -> bool:
    """True if v already looks like a TOML value (number/bool/array) and should
    NOT be wrapped in quotes."""
    if v in ("true", "false"):
        return True
    if v.startswith("[") and v.endswith("]"):
        return True
    try:
        float(v)
        return True
    except ValueError:
        return False


def _parse_jsonl(path: Path) -> list[dict]:
    events: list[dict] = []
    if not path.exists():
        return events
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _extract_usage(events: list[dict]) -> dict[str, Any]:
    """Pull the last token/usage info we can find from JSONL events.
    Codex event shapes move between versions, so we scan generously."""
    usage: dict[str, Any] = {}
    for ev in events:
        for key in ("usage", "token_usage", "tokens"):
            node = ev.get(key) if isinstance(ev, dict) else None
            if isinstance(node, dict):
                usage.update(node)
        # nested under msg/info
        for container in ("msg", "info", "data"):
            sub = ev.get(container) if isinstance(ev, dict) else None
            if isinstance(sub, dict):
                for key in ("usage", "token_usage", "tokens"):
                    node = sub.get(key)
                    if isinstance(node, dict):
                        usage.update(node)
    return usage


def _extract_session_id(events: list[dict]) -> str | None:
    """Codex emits the resumable thread/conversation UUID in its events (observed as
    `thread_id` on the first event in codex-cli 0.128.0). Scan generously across
    likely key names so this survives minor version drift."""
    keys = ("thread_id", "session_id", "conversation_id", "rollout_id", "id")
    for ev in events:
        if not isinstance(ev, dict):
            continue
        for k in keys:
            v = ev.get(k)
            if isinstance(v, str) and re.match(r"^[0-9a-fA-F-]{16,}$", v):
                return v
        for container in ("msg", "info", "data", "session", "thread"):
            sub = ev.get(container)
            if isinstance(sub, dict):
                for k in keys:
                    v = sub.get(k)
                    if isinstance(v, str) and re.match(r"^[0-9a-fA-F-]{16,}$", v):
                        return v
    return None


def _read_final_message(output_last_message: Path, events: list[dict]) -> str:
    if output_last_message.exists():
        text = output_last_message.read_text(errors="replace").strip()
        if text:
            return text
    # Fallback: last event that carries a textual message.
    for ev in reversed(events):
        if not isinstance(ev, dict):
            continue
        for key in ("last_agent_message", "message", "text", "content"):
            val = ev.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        msg = ev.get("msg")
        if isinstance(msg, dict):
            for key in ("message", "text", "last_agent_message"):
                val = msg.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return ""
