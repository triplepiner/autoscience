#!/usr/bin/env python3
"""Mission-control web dashboard for the autoscience pipeline.

    python3 server.py [--port 8765]

- Drag & drop one or more idea.md files -> each launches its own pipeline run
  in a background thread (real codex by default; mock demo mode available).
- Live board: phase tracker, iteration count, verdict history, log streaming.
- Abort button writes the run's ABORT sentinel (the orchestrator's kill switch).
- Serves final/paper.pdf and all run artifacts read-only.
- On Ctrl-C / SIGTERM the server drops ABORT sentinels into every active run so
  in-flight codex subprocesses are torn down cleanly, then exits.

stdlib only — no new dependencies. The dashboard never submits anywhere; it is
a window onto local disk.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pipeline.calendar_ics import build_ics, parse_deadline, venue_event  # noqa: E402
from pipeline.concierge import Concierge, load_chat  # noqa: E402
from pipeline.config import load_config  # noqa: E402
from pipeline.logging_utils import utc_stamp  # noqa: E402
from pipeline.orchestrator import run_pipeline  # noqa: E402
from pipeline import pdf_versions  # noqa: E402
from pipeline.venue_scout import VenueScout, load_venues  # noqa: E402
from pipeline.workspace import slugify, title_from_idea  # noqa: E402


def _pdf_fields(root: Path) -> dict:
    """Versioned-PDF summary for a run (latest PDF url, current version, push count)."""
    lp = pdf_versions.latest_pdf(root)
    retained = [v for v in pdf_versions.public_versions(root) if v.get("retained")]
    latest_v = retained[-1]["version"] if retained else None
    return {
        "has_pdf": lp is not None,
        "pdf_url": f"/files/{root.name}/final/{lp.name}" if lp else None,
        "version": latest_v,
        "push": pdf_versions.push_count(root),
        "n_versions": len(retained),
    }

PHASE_RE = re.compile(r"\] PHASE ==== (.+?) ====")
VERDICT_RE = re.compile(r"iter(\d+) verdict: (PASS|REVISE|HOLD)")
ITER_RE = re.compile(r"iter(\d+)")
SUMMARY_STATE_RE = re.compile(r"\*\*Terminal state:\*\* `([^`]+)`")
SUMMARY_REASON_RE = re.compile(r"\*\*Reason:\*\* (.+)")


class RunManager:
    def __init__(self, root: Path):
        self.root = root
        self.cfg_path = root / "config.yaml"
        base_cfg = load_config(self.cfg_path)
        self.runs_dir = Path(os.environ["AUTOSCIENCE_RUNS_DIR"]).resolve() \
            if os.environ.get("AUTOSCIENCE_RUNS_DIR") else base_cfg.runs_dir
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.uploads = root / "ideas" / "uploaded"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.records: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.mock_shim = self._write_mock_shim()

    def _write_mock_shim(self) -> Path:
        """One-file shim so mock demo mode looks like a single codex binary."""
        mock_py = ROOT / "tests" / "mock_codex.py"
        shim = self.runs_dir / ".mock_codex_shim"
        text = "#!/usr/bin/env bash\n"
        text += ': "${MOCK_CODEX_SCENARIO:=happy}"\n'
        text += "export MOCK_CODEX_SCENARIO\n"
        text += f'exec "{sys.executable}" "{mock_py}" "$@"\n'
        shim.write_text(text)
        shim.chmod(0o755)
        return shim

    # -- launching ---------------------------------------------------------
    def launch(self, filename: str, content: str, mock: bool = False,
               max_iterations: int | None = None) -> dict:
        if not content.strip():
            raise ValueError("empty idea file")
        stamp = utc_stamp() + "-" + secrets.token_hex(2)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "idea.md").strip("._") or "idea.md"
        if not safe.lower().endswith(".md"):
            safe += ".md"
        idea_path = self.uploads / f"{stamp}-{safe}"
        idea_path.write_text(content)

        # Predict the run id exactly the way create_run_workspace will derive it.
        title = title_from_idea(content, fallback=idea_path.stem)
        run_id = f"{slugify(title)}-{stamp}"

        rec = {
            "id": run_id,
            "title": title,
            "root": str(self.runs_dir / run_id),
            "idea_file": str(idea_path),
            "status": "running",
            "terminal_state": None,
            "reason": "",
            "mock": bool(mock),
            "max_iterations": max_iterations,
            "started_ts": time.time(),
            "finished_ts": None,
            "stamp": stamp,
        }
        with self.lock:
            self.records[run_id] = rec
        t = threading.Thread(target=self._worker, args=(rec, idea_path),
                             name=f"run-{run_id}", daemon=False)
        rec["_thread"] = t
        t.start()
        return rec

    def _worker(self, rec: dict, idea_path: Path) -> None:
        try:
            cfg = load_config(self.cfg_path)
            if rec["mock"]:
                cfg.data["codex"]["bin"] = str(self.mock_shim)
            if rec["max_iterations"]:
                cfg.data["caps"]["max_iterations"] = int(rec["max_iterations"])
            result = run_pipeline(idea_path, cfg, stamp=rec["stamp"])
            rec["terminal_state"] = result.terminal_state
            rec["reason"] = result.reason
        except Exception as e:  # noqa: BLE001
            rec["terminal_state"] = "ERROR"
            rec["reason"] = f"orchestrator exception: {e!r}"
        finally:
            rec["status"] = "finished"
            rec["finished_ts"] = time.time()

    # -- continue a finished run with a follow-up .md ------------------------
    def continue_run(self, run_id: str, content: str) -> dict:
        root = self._run_root(run_id)
        if root is None:
            return {"error": "run not found"}
        if not (root / "PRD.md").exists():
            return {"error": "this run has no PRD.md to revise"}
        if not str(content).strip():
            return {"error": "empty change request"}
        with self.lock:
            rec = self.records.get(run_id)
            if rec and rec.get("status") == "running":
                return {"error": "run is already active — wait for it to finish"}
            title = rec["title"] if rec else title_from_idea(
                (root / "idea.md").read_text(errors="replace") if (root / "idea.md").exists() else run_id,
                fallback=run_id)
            is_mock = bool(rec.get("mock")) if rec else False
            rec = rec or {"id": run_id, "title": title, "root": str(root),
                          "mock": is_mock, "max_iterations": None, "stamp": ""}
            rec.update(status="running", terminal_state=None, reason="", title=title,
                       started_ts=time.time(), finished_ts=None)
            self.records[run_id] = rec

        cfg = load_config(self.cfg_path)
        if is_mock:
            cfg.data["codex"]["bin"] = str(self.mock_shim)
        from pipeline.orchestrator import continue_run as orch_continue

        def work():
            try:
                res = orch_continue(root, content, cfg)
                rec["terminal_state"] = res.terminal_state
                rec["reason"] = res.reason
            except Exception as e:  # noqa: BLE001
                rec["terminal_state"] = "ERROR"
                rec["reason"] = f"continue exception: {e!r}"
            finally:
                rec["status"] = "finished"
                rec["finished_ts"] = time.time()
        t = threading.Thread(target=work, name=f"continue-{run_id}", daemon=False)
        rec["_thread"] = t
        t.start()
        return {"ok": True, "started": True,
                "note": "revising the paper per your changes (coder → judge → compile)"}

    # -- abort / shutdown ----------------------------------------------------
    def abort(self, run_id: str) -> bool:
        root = self._run_root(run_id)
        if root is None:
            return False
        (root / "ABORT").write_text("aborted from dashboard\n")
        return True

    def abort_all_active(self) -> int:
        n = 0
        with self.lock:
            recs = list(self.records.values())
        for rec in recs:
            if rec["status"] == "running":
                Path(rec["root"]).mkdir(parents=True, exist_ok=True)
                (Path(rec["root"]) / "ABORT").write_text("server shutdown\n")
                n += 1
        return n

    def join_all(self, timeout_s: float = 20.0) -> None:
        deadline = time.time() + timeout_s
        with self.lock:
            threads = [r.get("_thread") for r in self.records.values()]
        for t in threads:
            if t is not None and t.is_alive():
                t.join(timeout=max(0.1, deadline - time.time()))

    # -- concierge: chat / terminal / inject -----------------------------------
    def _concierge(self) -> Concierge:
        return Concierge(load_config(self.cfg_path))

    def chat(self, run_id: str, message: str) -> dict:
        root = self._run_root(run_id)
        if root is None:
            return {"error": "run not found"}
        return self._concierge().chat(root, message)

    def chat_history(self, run_id: str) -> list[dict]:
        root = self._run_root(run_id)
        return load_chat(root) if root else []

    def sessions(self, run_id: str) -> list[dict]:
        root = self._run_root(run_id)
        if root is None:
            return []
        from pipeline.concierge import list_sessions
        return list_sessions(root)

    def open_terminal(self, run_id: str, role: str) -> dict:
        root = self._run_root(run_id)
        if root is None:
            return {"error": "run not found"}
        return self._concierge().open_terminal(root, role)

    def terminal_command(self, run_id: str, role: str) -> dict:
        root = self._run_root(run_id)
        if root is None:
            return {"error": "run not found"}
        return self._concierge().terminal_command(root, role)

    def inject(self, run_id: str, role: str, guidance: str) -> dict:
        root = self._run_root(run_id)
        if root is None:
            return {"error": "run not found"}
        conc = self._concierge()

        def work():
            try:
                conc.inject(root, guidance, role=role)
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=work, name=f"inject-{run_id}", daemon=True).start()
        return {"ok": True, "started": True,
                "note": f"injecting guidance into the {role} thread; watch the chat for its reply"}

    # -- batch revise (upload a zip of .md, auto-match to threads, launch all) -
    def batch_upload(self, filename: str, content_b64: str) -> dict:
        import base64
        import io
        import zipfile
        bid = utc_stamp() + "-" + secrets.token_hex(2)
        bdir = self.runs_dir / ".batches" / bid
        (bdir / "files").mkdir(parents=True, exist_ok=True)
        try:
            raw = base64.b64decode((content_b64 or "").split(",")[-1])
        except (ValueError, TypeError):
            return {"error": "bad upload encoding"}
        files = []
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for info in z.infolist():
                    if info.is_dir() or "__MACOSX" in info.filename:
                        continue
                    nm = Path(info.filename).name
                    if not nm or nm.startswith(".") or not re.search(r"\.(md|markdown|txt)$", nm, re.I):
                        continue
                    content = z.read(info).decode("utf-8", "replace")
                    (bdir / "files" / nm).write_text(content)
                    files.append({"name": nm, "content": content, "size": len(content)})
        except zipfile.BadZipFile:
            return {"error": "that file is not a valid .zip"}
        if not files:
            return {"error": "no .md files found in the zip"}
        rec = {"id": bid, "dir": str(bdir), "files": files, "status": "matching",
               "assignments": [], "threads": []}
        self.batches[bid] = rec

        def work():
            try:
                from pipeline.batch_match import BatchMatcher, list_threads
                cfg = load_config(self.cfg_path)
                threads = list_threads(self.runs_dir)
                rec["threads"] = [{"run_id": t["run_id"], "title": t["title"]} for t in threads]
                rec["assignments"] = BatchMatcher(cfg).match(files, threads, bdir)
            except Exception as e:  # noqa: BLE001
                rec["error"] = repr(e)
                rec["assignments"] = [{"file": f["name"], "run_id": "", "confidence": 0,
                                       "reason": "matcher failed"} for f in files]
            finally:
                rec["status"] = "ready"
        threading.Thread(target=work, name=f"batch-{bid}", daemon=True).start()
        return {"ok": True, "batch_id": bid, "n_files": len(files)}

    def batch_get(self, bid: str) -> dict:
        rec = self.batches.get(bid)
        if not rec:
            return {"error": "batch not found"}
        threads = rec.get("threads")
        if not threads:
            from pipeline.batch_match import list_threads
            threads = [{"run_id": t["run_id"], "title": t["title"]}
                       for t in list_threads(self.runs_dir)]
        return {"id": bid, "status": rec["status"], "error": rec.get("error", ""),
                "files": [{"name": f["name"], "size": f["size"]} for f in rec["files"]],
                "assignments": rec.get("assignments", []), "threads": threads}

    def batch_launch(self, bid: str, assignments: list) -> dict:
        rec = self.batches.get(bid)
        if not rec:
            return {"error": "batch not found"}
        cmap = {f["name"]: f["content"] for f in rec["files"]}
        launched, skipped = [], []
        for a in assignments:
            fn = (a or {}).get("file")
            rid = (a or {}).get("run_id")
            if not fn or not rid or fn not in cmap:
                skipped.append(fn)
                continue
            r = self.continue_run(rid, cmap[fn])
            if r.get("ok"):
                launched.append({"file": fn, "run_id": rid})
            else:
                skipped.append(f"{fn} ({r.get('error', '?')})")
        rec["status"] = "launched"
        return {"ok": True, "launched": len(launched), "items": launched,
                "skipped": [s for s in skipped if s]}

    # -- venue scout + calendar ------------------------------------------------
    def scout(self, run_id: str) -> dict:
        root = self._run_root(run_id)
        if root is None:
            return {"error": "run not found"}
        if not (root / "workspace" / "paper_draft.tex").exists() and not (root / "PRD.md").exists():
            return {"error": "no paper yet — scout after a draft exists"}
        sentinel = root / ".scouting"
        if sentinel.exists():
            return {"ok": True, "started": True, "note": "scout already running"}
        sentinel.write_text(str(time.time()))
        scout = VenueScout(load_config(self.cfg_path))

        def work():
            try:
                scout.scout(root)
            except Exception:  # noqa: BLE001
                pass
            finally:
                sentinel.unlink(missing_ok=True)
        threading.Thread(target=work, name=f"scout-{run_id}", daemon=True).start()
        return {"ok": True, "started": True,
                "note": "venue scout is browsing the web for fitting venues (1-5 min)"}

    def venues(self, run_id: str) -> dict:
        root = self._run_root(run_id)
        if root is None:
            return {"error": "run not found"}
        data = load_venues(root)
        data["scouting"] = (root / ".scouting").exists()
        try:
            picks = json.loads((root / "picks.json").read_text()).get("picked", [])
        except (OSError, json.JSONDecodeError):
            picks = []
        data["picked"] = picks
        return data

    def set_picks(self, run_id: str, picked: list[str]) -> dict:
        root = self._run_root(run_id)
        if root is None:
            return {"error": "run not found"}
        (root / "picks.json").write_text(json.dumps({"picked": picked}))
        self._regen_ics(run_id)
        self._regen_aggregate_ics()
        return {"ok": True, "picked": picked,
                "ics": f"/files/{run_id}/deadlines.ics"}

    def _run_events(self, run_id: str, root: Path) -> list[dict]:
        venues = load_venues(root).get("venues", [])
        try:
            picked = set(json.loads((root / "picks.json").read_text()).get("picked", []))
        except (OSError, json.JSONDecodeError):
            picked = set()
        title = root.name
        idea = root / "idea.md"
        if idea.exists():
            title = title_from_idea(idea.read_text(errors="replace"), fallback=root.name)
        events = []
        for i, v in enumerate(venues):
            if v.get("name") not in picked:
                continue
            ev = venue_event(title, v, uid=f"{run_id}-{i}")
            if ev:
                events.append(ev)
        return events

    def _regen_ics(self, run_id: str) -> None:
        root = self._run_root(run_id)
        if root is None:
            return
        events = self._run_events(run_id, root)
        (root / "deadlines.ics").write_text(
            build_ics(events, cal_name=f"Deadlines — {root.name}", stamp=utc_stamp()))

    def _regen_aggregate_ics(self) -> None:
        events = []
        for d in self.runs_dir.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            events += self._run_events(d.name, d)
        (self.runs_dir / "all_deadlines.ics").write_text(
            build_ics(events, cal_name="Autoscience — all picked deadlines", stamp=utc_stamp()))

    def calendar(self) -> dict:
        """Aggregate picked venues across all runs for the dashboard calendar view."""
        items = []
        for d in self.runs_dir.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            venues = load_venues(d).get("venues", [])
            try:
                picked = set(json.loads((d / "picks.json").read_text()).get("picked", []))
            except (OSError, json.JSONDecodeError):
                picked = set()
            if not picked:
                continue
            title = d.name
            idea = d / "idea.md"
            if idea.exists():
                title = title_from_idea(idea.read_text(errors="replace"), fallback=d.name)
            for v in venues:
                if v.get("name") not in picked:
                    continue
                dt = parse_deadline(v.get("deadline", ""))
                items.append({
                    "run_id": d.name, "paper": title, "venue": v.get("name"),
                    "tier": v.get("tier"), "kind": v.get("kind"),
                    "archival": v.get("archival"), "deadline": v.get("deadline"),
                    "date": dt.isoformat() if dt else None,
                    "deadline_verified": v.get("deadline_verified"),
                    "accept_odds": v.get("accept_odds"), "fit_score": v.get("fit_score"),
                    "cfp_url": v.get("cfp_url") or v.get("url", ""),
                })
        items.sort(key=lambda x: (x["date"] is None, x["date"] or "9999"))
        return {"items": items, "ics": "/calendar.ics"}

    def _patch_summary_terminal(self, root: Path, terminal: str, reason: str) -> None:
        p = root / "run_summary.md"
        if p.exists():
            t = p.read_text(errors="replace")
            t = re.sub(r"(\*\*Terminal state:\*\* `)[^`]*(`)", rf"\g<1>{terminal}\g<2>", t, count=1)
            t = re.sub(r"(\*\*Reason:\*\* ).*", rf"\g<1>{reason}", t, count=1)
            p.write_text(t)
        else:
            p.write_text(f"# Run summary — {root.name}\n\n- **Terminal state:** `{terminal}`\n"
                         f"- **Reason:** {reason}\n")

    def submit_anyway(self, run_id: str) -> dict:
        """Compile a held (or otherwise stalled) draft into a submittable PDF and
        mark the run DONE. HOLD implies Criterion 1 already passed, so this is safe;
        for other states it compiles the draft AS-IS without a fresh judge sign-off."""
        root = self._run_root(run_id)
        if root is None:
            return {"error": "run not found"}
        tex = root / "workspace" / "paper_draft.tex"
        if not tex.exists():
            return {"error": "no paper_draft.tex to compile for this run"}
        cfg = load_config(self.cfg_path)
        from pipeline import compile as compile_mod
        version = pdf_versions.push_count(root)
        out_pdf = pdf_versions.version_pdf_path(root, version)
        cres = compile_mod.compile_pdf(
            tex, out_pdf,
            engine_pref=cfg.get("compile", "engine", default="auto"),
            max_passes=int(cfg.get("compile", "max_passes", default=3)))
        if not cres.ok:
            return {"error": "LaTeX did not compile: " + (cres.log[-300:] or "unknown")}
        venue = (cfg.venues or [{}])[0]
        pc, within = compile_mod.check_page_limit(out_pdf, venue.get("page_limit"))
        pdf_versions.record_version(root, version, pdf_name=out_pdf.name, pages=pc,
                                    retained=True, note="submit-anyway")
        reason = (f"submit-anyway: held draft compiled to a submittable PDF v{version} "
                  f"({pc}/{venue.get('page_limit')} pages). Human decides whether to submit.")
        with self.lock:
            rec = self.records.get(run_id)
            if rec:
                rec["terminal_state"] = "DONE"
                rec["status"] = "finished"
                rec["reason"] = reason
                rec["finished_ts"] = rec.get("finished_ts") or time.time()
        self._patch_summary_terminal(root, "DONE", reason)
        return {"ok": True, "pdf": f"/files/{run_id}/final/paper.pdf",
                "pages": pc, "within_limit": within}

    def reveal_in_finder(self, run_id: str) -> dict:
        root = self._run_root(run_id)
        if root is None:
            return {"error": "run not found"}
        try:
            r = subprocess.run(["open", str(root)], capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return {"ok": True, "path": str(root)}
            return {"error": r.stderr.strip() or "could not open Finder", "path": str(root)}
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            return {"error": str(e), "path": str(root)}

    def migrate_all_pdfs(self) -> int:
        """One-time: migrate every run's legacy final/paper.pdf to the versioned,
        thread-named scheme. A rename, never a delete. Returns count migrated."""
        n = 0
        for d in self.runs_dir.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            try:
                if pdf_versions.migrate_legacy(d):
                    n += 1
            except Exception:  # noqa: BLE001
                pass
        return n

    def delete_run(self, run_id: str) -> dict:
        root = self._run_root(run_id)
        if root is None:
            return {"error": "run not found"}
        with self.lock:
            rec = self.records.get(run_id)
            if rec and rec.get("status") == "running":
                return {"error": "run is still active — abort it first"}
            self.records.pop(run_id, None)
        import shutil as _sh
        _sh.rmtree(root, ignore_errors=True)
        self._regen_aggregate_ics()
        return {"ok": True}

    # -- state reading ---------------------------------------------------------
    def _run_root(self, run_id: str) -> Path | None:
        if "/" in run_id or ".." in run_id or not run_id.strip():
            return None
        root = (self.runs_dir / run_id).resolve()
        if not str(root).startswith(str(self.runs_dir.resolve())) or not root.exists():
            return None
        return root

    def _live_state(self, root: Path) -> dict:
        log = root / "logs" / "orchestrator.log"
        phases: list[str] = []
        verdicts: list[dict] = []
        if log.exists():
            text = log.read_text(errors="replace")
            phases = PHASE_RE.findall(text)
            verdicts = [{"iteration": int(i), "verdict": v}
                        for i, v in VERDICT_RE.findall(text)]
        current = phases[-1] if phases else "QUEUED"
        terminal = None
        if current.startswith("TERMINAL:"):
            terminal = current.split(":", 1)[1].strip()
        iters = [int(n) for p in phases for n in ITER_RE.findall(p)]
        return {
            "phase": current,
            "terminal_from_log": terminal,
            "iteration": max(iters) if iters else 0,
            "verdicts": verdicts,
        }

    def _summary_fields(self, root: Path) -> dict:
        out = {"terminal_state": None, "reason": ""}
        summ = root / "run_summary.md"
        if summ.exists():
            text = summ.read_text(errors="replace")
            m = SUMMARY_STATE_RE.search(text)
            if m:
                out["terminal_state"] = m.group(1)
            m = SUMMARY_REASON_RE.search(text)
            if m:
                out["reason"] = m.group(1).strip()
        return out

    def _public(self, rec: dict) -> dict:
        root = Path(rec["root"])
        live = self._live_state(root) if root.exists() else {
            "phase": "QUEUED", "terminal_from_log": None, "iteration": 0, "verdicts": []}
        terminal = rec.get("terminal_state") or live["terminal_from_log"]
        elapsed = ((rec.get("finished_ts") or time.time()) - rec["started_ts"]
                   if rec.get("started_ts") else None)
        return {
            "id": rec["id"],
            "title": rec["title"],
            "status": rec["status"],
            "terminal_state": terminal,
            "reason": rec.get("reason", ""),
            "phase": live["phase"],
            "iteration": live["iteration"],
            "max_iterations": rec.get("max_iterations") or 5,
            "verdicts": live["verdicts"],
            "mock": rec.get("mock", False),
            "elapsed_s": elapsed,
            "started_ts": rec.get("started_ts"),
            **_pdf_fields(root),
        }

    def list_runs(self) -> list[dict]:
        with self.lock:
            recs = {rid: dict(r) for rid, r in self.records.items()}
        out = [self._public(r) for r in recs.values()]
        # Merge historical runs found on disk (from previous server sessions).
        seen = set(recs.keys())
        try:
            disk = sorted((d for d in self.runs_dir.iterdir() if d.is_dir()),
                          key=lambda d: d.stat().st_mtime, reverse=True)[:100]
        except OSError:
            disk = []
        for d in disk:
            if d.name in seen or d.name.startswith("."):
                continue
            live = self._live_state(d)
            summ = self._summary_fields(d)
            idea = d / "idea.md"
            title = d.name
            if idea.exists():
                title = title_from_idea(idea.read_text(errors="replace"), fallback=d.name)
            terminal = summ["terminal_state"] or live["terminal_from_log"]
            # A disk run with no terminal state may still be running (e.g. launched
            # from the CLI, or by a prior server). Use log freshness as a liveness
            # signal before calling it INTERRUPTED.
            status = "finished"
            if terminal is None:
                log = d / "logs" / "orchestrator.log"
                fresh = log.exists() and (time.time() - log.stat().st_mtime) < 90
                if fresh:
                    status = "running"
                else:
                    terminal = "INTERRUPTED"
            out.append({
                "id": d.name, "title": title, "status": status,
                "terminal_state": terminal, "reason": summ["reason"],
                "phase": live["phase"], "iteration": live["iteration"],
                "max_iterations": 5, "verdicts": live["verdicts"],
                "mock": None, "elapsed_s": None,
                "started_ts": d.stat().st_mtime,
                **_pdf_fields(d),
            })
        out.sort(key=lambda r: ((r["status"] != "running"), -(r["started_ts"] or 0)))
        return out

    def detail(self, run_id: str) -> dict | None:
        root = self._run_root(run_id)
        with self.lock:
            rec = self.records.get(run_id)
        if root is None and rec is None:
            return None
        if rec is not None:
            base = self._public(rec)
        else:
            base = next((r for r in self.list_runs() if r["id"] == run_id), None)
            if base is None:
                return None
        if root is None:
            base.update(log_tail="", prd="", idea="", summary="", reviews=[])
            return base

        def read(p: Path, max_chars: int = 200_000) -> str:
            try:
                return p.read_text(errors="replace")[-max_chars:] if p.exists() else ""
            except OSError:
                return ""

        log_text = read(root / "logs" / "orchestrator.log")
        log_tail = "\n".join(log_text.splitlines()[-300:])
        reviews = []
        for rp in sorted((root / "reviews").glob("JUDGE_REVIEW_iter*.md")):
            reviews.append({"name": rp.name, "content": read(rp, 40_000)})
        from pipeline.concierge import list_sessions
        sessions = list_sessions(root)
        base.update(
            log_tail=log_tail,
            prd=read(root / "PRD.md", 60_000),
            idea=read(root / "idea.md", 30_000),
            summary=read(root / "run_summary.md", 40_000),
            build_log=read(root / "workspace" / "BUILD_LOG.md", 40_000),
            reviews=reviews,
            sessions=sessions,
            has_coder_session=any(s.get("role") == "coder" and s.get("session_id")
                                  for s in sessions),
            chat=load_chat(root),
            venues=load_venues(root).get("venues", []),
            venues_summary=load_venues(root).get("summary", ""),
            scouting=(root / ".scouting").exists(),
            picked=_safe_picks(root),
            has_paper=(root / "workspace" / "paper_draft.tex").exists(),
            pdf_versions=pdf_versions.public_versions(root),
        )
        return base


def _safe_picks(root: Path) -> list:
    try:
        return json.loads((root / "picks.json").read_text()).get("picked", [])
    except (OSError, json.JSONDecodeError):
        return []


MANAGER: RunManager | None = None
INDEX_HTML = ROOT / "web" / "index.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "autoscience-control/1.0"

    def log_message(self, *args):  # quiet
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._bytes(INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
                return
            if path == "/api/runs":
                self._json(MANAGER.list_runs())
                return
            if path == "/api/calendar":
                self._json(MANAGER.calendar())
                return
            if path == "/calendar.ics":
                ics = MANAGER.runs_dir / "all_deadlines.ics"
                if not ics.exists():
                    MANAGER._regen_aggregate_ics()
                data = ics.read_bytes() if ics.exists() else b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=autoscience_deadlines.ics")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            m = re.match(r"^/api/runs/([^/]+)$", path)
            if m:
                d = MANAGER.detail(m.group(1))
                if d is None:
                    self._json({"error": "not found"}, 404)
                else:
                    self._json(d)
                return
            m = re.match(r"^/api/runs/([^/]+)/chat$", path)
            if m:
                self._json(MANAGER.chat_history(m.group(1)))
                return
            m = re.match(r"^/api/runs/([^/]+)/sessions$", path)
            if m:
                self._json(MANAGER.sessions(m.group(1)))
                return
            m = re.match(r"^/api/batch/([^/]+)$", path)
            if m:
                self._json(MANAGER.batch_get(m.group(1)))
                return
            m = re.match(r"^/api/runs/([^/]+)/terminal-command$", path)
            if m:
                from urllib.parse import parse_qs
                role = (parse_qs(urlparse(self.path).query).get("role") or ["coder"])[0]
                self._json(MANAGER.terminal_command(m.group(1), role))
                return
            m = re.match(r"^/api/runs/([^/]+)/venues$", path)
            if m:
                self._json(MANAGER.venues(m.group(1)))
                return
            m = re.match(r"^/api/push/(\d+)/pdfs\.zip$", path)
            if m:
                self._serve_push_zip(int(m.group(1)))
                return
            m = re.match(r"^/files/([^/]+)/(.+)$", path)
            if m:
                self._serve_file(m.group(1), m.group(2))
                return
            self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._json({"error": repr(e)}, 500)
            except (BrokenPipeError, OSError):
                pass

    def _serve_push_zip(self, push: int) -> None:
        """Download the push-N PDF of every thread CURRENTLY on push N — i.e. whose
        latest kept PDF is v{N}. So 'Push 2' yields only the v2 PDFs from threads that
        reached a second push, not all PDFs. Each file keeps its thread-named name."""
        import io
        import zipfile
        runs_dir = MANAGER.runs_dir
        files: list[Path] = []
        for d in runs_dir.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            fields = _pdf_fields(d)
            if fields.get("version") == push and fields.get("has_pdf"):
                lp = pdf_versions.latest_pdf(d)
                if lp and lp.is_file():
                    files.append(lp)
        if not files:
            self._json({"error": f"no threads currently on push {push}"}, 404)
            return
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files:
                z.write(p, arcname=p.name)
        data = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f'attachment; filename="push-{push}-pdfs.zip"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, run_id: str, rel: str) -> None:
        root = MANAGER._run_root(run_id)
        if root is None:
            self._json({"error": "not found"}, 404)
            return
        target = (root / rel).resolve()
        if not str(target).startswith(str(root.resolve())) or not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._bytes(target.read_bytes(), ctype)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "bad JSON body"}, 400)
            return
        try:
            if path == "/api/launch":
                content = body.get("content", "")
                if not str(content).strip():
                    self._json({"error": "empty idea content"}, 400)
                    return
                mi = body.get("max_iterations")
                rec = MANAGER.launch(
                    filename=str(body.get("filename") or "idea.md"),
                    content=str(content),
                    mock=bool(body.get("mock", False)),
                    max_iterations=int(mi) if mi else None,
                )
                self._json({"ok": True, "id": rec["id"], "title": rec["title"]})
                return
            m = re.match(r"^/api/runs/([^/]+)/abort$", path)
            if m:
                ok = MANAGER.abort(m.group(1))
                self._json({"ok": ok}, 200 if ok else 404)
                return
            m = re.match(r"^/api/runs/([^/]+)/chat$", path)
            if m:
                msg = str(body.get("message") or "").strip()
                if not msg:
                    self._json({"error": "empty message"}, 400)
                    return
                self._json(MANAGER.chat(m.group(1), msg))
                return
            m = re.match(r"^/api/runs/([^/]+)/open-terminal$", path)
            if m:
                role = str(body.get("role") or "coder")
                self._json(MANAGER.open_terminal(m.group(1), role))
                return
            m = re.match(r"^/api/runs/([^/]+)/inject$", path)
            if m:
                guidance = str(body.get("guidance") or "").strip()
                role = str(body.get("role") or "coder")
                if not guidance:
                    self._json({"error": "empty guidance"}, 400)
                    return
                self._json(MANAGER.inject(m.group(1), role, guidance))
                return
            m = re.match(r"^/api/runs/([^/]+)/scout$", path)
            if m:
                self._json(MANAGER.scout(m.group(1)))
                return
            m = re.match(r"^/api/runs/([^/]+)/picks$", path)
            if m:
                picked = body.get("picked")
                if not isinstance(picked, list):
                    self._json({"error": "picked must be a list of venue names"}, 400)
                    return
                self._json(MANAGER.set_picks(m.group(1), [str(p) for p in picked]))
                return
            m = re.match(r"^/api/runs/([^/]+)/continue$", path)
            if m:
                content = str(body.get("content") or "")
                if not content.strip():
                    self._json({"error": "empty change request"}, 400)
                    return
                self._json(MANAGER.continue_run(m.group(1), content))
                return
            if path == "/api/batch/upload":
                content = str(body.get("content") or "")
                if not content:
                    self._json({"error": "no file content"}, 400)
                    return
                self._json(MANAGER.batch_upload(str(body.get("filename") or "batch.zip"), content))
                return
            m = re.match(r"^/api/batch/([^/]+)/launch$", path)
            if m:
                assigns = body.get("assignments")
                if not isinstance(assigns, list):
                    self._json({"error": "assignments must be a list"}, 400)
                    return
                self._json(MANAGER.batch_launch(m.group(1), assigns))
                return
            m = re.match(r"^/api/runs/([^/]+)/submit-anyway$", path)
            if m:
                self._json(MANAGER.submit_anyway(m.group(1)))
                return
            m = re.match(r"^/api/runs/([^/]+)/reveal$", path)
            if m:
                self._json(MANAGER.reveal_in_finder(m.group(1)))
                return
            m = re.match(r"^/api/runs/([^/]+)/delete$", path)
            if m:
                self._json(MANAGER.delete_run(m.group(1)))
                return
            self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._json({"error": repr(e)}, 500)
            except (BrokenPipeError, OSError):
                pass


def main() -> int:
    global MANAGER
    p = argparse.ArgumentParser(description="autoscience mission control")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()

    MANAGER = RunManager(ROOT)
    migrated = MANAGER.migrate_all_pdfs()
    if migrated:
        print(f"[control] migrated {migrated} run(s) to versioned thread-named PDFs", flush=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)

    def shutdown(signum, frame):  # noqa: ARG001
        n = MANAGER.abort_all_active()
        print(f"\n[control] shutdown: ABORT sentinel dropped into {n} active run(s); "
              "waiting for clean teardown...", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"[control] autoscience mission control on http://{args.host}:{args.port}", flush=True)
    print(f"[control] runs dir: {MANAGER.runs_dir}", flush=True)
    server.serve_forever()
    MANAGER.join_all(timeout_s=25)
    print("[control] all runs torn down; bye", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
