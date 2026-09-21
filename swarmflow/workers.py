"""Worker supervision: spawn Pi + the local model, watch sessions, audit artifacts.

Success is judged by artifacts (trace contents + owned files on disk), never by exit
codes: the stress test showed rc=0 alongside a completely empty deliverable. Prompt
rendering lives in ``prompt.py`` and trace analysis in ``trace.py``; this module owns
the process lifecycle and the ledger writes that follow from it.
"""

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from .audit import _hash_file
from .config import build_cli_command, resolve_executable
from .procs import kill_tree, spawn_flags
from .prompt import delivery_changed, fix_context, render_brief
from .trace import classify, scan_forbidden, scan_server_launches, scan_trace


class WaveAborted(Exception):
    """Raised when wave preconditions are not met and nothing was dispatched."""


def read_server_load(metrics_url: str, timeout: float = 5) -> dict:
    """Best-effort read of vLLM gauges. Returns {} on failure (caller should not block)."""
    try:
        with urllib.request.urlopen(metrics_url, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
    except Exception:
        return {}
    load = {}
    for line in text.splitlines():
        if line.startswith("vllm:num_requests_running"):
            load["running"] = _last_float(line)
        elif line.startswith("vllm:num_requests_waiting"):
            load["waiting"] = _last_float(line)
        elif line.startswith(("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")):
            load["kv"] = _last_float(line)
    return load


def _last_float(line: str) -> float:
    try:
        return float(line.rsplit(" ", 1)[-1])
    except ValueError:
        return 0.0


class WorkerRunner:
    def __init__(self, config: dict, ledger, project_root: str, repo_root: Path):
        self.config = config
        self.ledger = ledger
        self.project_root = Path(project_root)
        self.repo_root = Path(repo_root)
        self.logs_dir = self.project_root / config["paths"]["logs_dir"]
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- spawning

    def _spawn(self, task: dict, thinking: str, attempt: int):
        worker = self.config["worker"]
        if not worker.get("model"):
            raise ValueError("worker.model is not configured; run `swarmflow init` "
                             "and edit config/swarmflow.yaml")
        cli = worker.get("pi_cli") or resolve_executable(["pi"])
        prefix = build_cli_command(cli, worker.get("node", "node"))
        trace_path = self.logs_dir / f"{task['id']}_a{attempt}.jsonl"
        cmd = prefix + [
            "-p", "--mode", "json", "--no-session",
            "--model", worker["model"], "--thinking", thinking,
        ]
        out = trace_path.open("wb")
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(self.project_root), stdin=subprocess.PIPE,
                stdout=out, stderr=subprocess.STDOUT, **spawn_flags(),
            )
        except OSError:
            out.close()
            raise
        pgid = None
        if os.name != "nt":
            try:
                pgid = os.getpgid(proc.pid)
            except OSError:
                pgid = None
        before = {}
        for rel in task.get("owner_files", []):
            path = self.project_root / rel
            try:
                before[rel] = _hash_file(path) if path.exists() else None
            except OSError:
                before[rel] = None
        return {"proc": proc, "out": out, "trace": str(trace_path), "task": task,
                "thinking": thinking, "before": before, "pgid": pgid,
                "started": time.monotonic()}

    def _kill_and_wait(self, handle: dict, grace_s: float | None = None) -> str | None:
        """Kill the handle's tree with bounded waits. Returns 'kill_failed' or None."""
        proc = handle["proc"]
        grace = grace_s if grace_s is not None else \
            float(self.config["worker"].get("kill_grace_s", 30.0))
        if proc.poll() is not None:
            return None
        kill_tree(proc.pid, handle.get("pgid"))
        try:
            proc.wait(timeout=grace)
            return None
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=grace)
            return None
        except subprocess.TimeoutExpired:
            return "kill_failed"

    def _finalize_one(self, handle: dict, killed_for: str | None) -> dict:
        proc, out, task = handle["proc"], handle["out"], handle["task"]
        cap = self.config["worker"]["max_output_tokens"]
        if proc.poll() is None:
            killed_for = killed_for or self._kill_and_wait(handle)
        # a session that exits cleanly can still leave a server/watcher behind; on
        # Windows the parent link is gone by now, so the sweep covers that case instead
        # of taskkill-ing a pid that could have been reused
        if os.name != "nt":
            kill_tree(proc.pid, handle.get("pgid"))
        try:
            out.close()
        except OSError:
            pass
        scan = scan_trace(handle["trace"], cap)
        forbidden = scan_forbidden(handle["trace"])
        launches = scan_server_launches(handle["trace"])
        missing = [f for f in task.get("owner_files", [])
                   if not (self.project_root / f).exists()]
        if killed_for == "kill_failed":
            outcome = "kill_failed"
        elif killed_for == "turn_cap":
            outcome = "turn_cap"
        elif forbidden:
            outcome = "scope_violation"
        else:
            outcome = classify(scan, missing)
        if outcome == "delivered" and not delivery_changed(self.project_root,
                                                           handle.get("before")):
            outcome = "no_changes"
        status = "delivered" if outcome == "delivered" else "failed"
        # inline smoke sessions have no task row; do not write ledger state for them
        if self.ledger.get(task["id"]) is not None:
            self.ledger.set_status(
                task["id"], status,
                worker_trace=handle["trace"],
                artifacts=[f for f in task.get("owner_files", []) if (self.project_root / f).exists()],
                verdict=json.dumps({"outcome": outcome, "missing": missing,
                                    "turns": scan["turns"], "out_tokens": scan["out_tokens"],
                                    "killed_for": killed_for, "forbidden": forbidden[:3],
                                    "server_launches": launches[:3]}),
            )
        return {"task_id": task["id"], "outcome": outcome, "missing": missing,
                "scan": scan, "server_launches": launches}

    def _supervise(self, handles: list, timeout_s: float) -> list:
        """Watch every handle at once; per-handle deadlines, bounded kills."""
        cap = self.config["worker"]["max_output_tokens"]
        turn_cap = int(self.config["worker"].get("max_turns", 45))
        poll_s = float(self.config["worker"].get("poll_s", 15.0))
        pending = list(handles)
        results = {}
        while pending:
            remaining = []
            for handle in pending:
                proc = handle["proc"]
                killed_for = None
                if proc.poll() is not None:
                    pass
                elif time.monotonic() - handle.get("started", time.monotonic()) > timeout_s:
                    killed_for = self._kill_and_wait(handle) or "timeout"
                elif scan_trace(handle["trace"], cap)["turns"] > turn_cap:
                    killed_for = self._kill_and_wait(handle) or "turn_cap"
                else:
                    remaining.append(handle)
                    continue
                results[id(handle)] = self._finalize_one(handle, killed_for)
            pending = remaining
            if pending:
                time.sleep(poll_s)
        return [results[id(handle)] for handle in handles]

    def _send_prompt(self, handle: dict, prompt: str) -> None:
        handle["proc"].stdin.write(prompt.encode("utf-8"))
        handle["proc"].stdin.close()

    def _wait_ready(self) -> None:
        swarm = self.config["swarm"]
        for _ in range(40):  # up to ~10 minutes of backpressure
            load = read_server_load(swarm["metrics_url"])
            if not load:
                return
            if load.get("waiting", 0) < swarm["backpressure_waiting"] and \
               load.get("kv", 0.0) < swarm["backpressure_kv"]:
                return
            time.sleep(15)

    # ------------------------------------------------------------------- api

    def build_prompt(self, task: dict, attempt_context: str = "",
                     fix_context: str = "") -> str:
        return render_brief(self.repo_root, self.project_root, task, attempt_context,
                            fix_context_text=fix_context)

    def _fail_spawn(self, task_id: str, error, handle: dict | None = None) -> None:
        """Record a task that could not be started (or whose child died at once)."""
        self.ledger.record_event(task_id, "spawn-failed", str(error)[:300])
        self.ledger.set_status(task_id, "failed", verdict=json.dumps(
            {"outcome": "spawn_failed", "error": str(error)[:200]}))
        if handle is not None:
            try:
                handle["out"].close()
            except OSError:
                pass
            self._kill_and_wait(handle)

    def run_task(self, task_id: str, thinking: str | None = None,
                 attempt_context: str = "") -> dict:
        task = self.ledger.get(task_id)
        if not task:
            raise ValueError(f"unknown task: {task_id}")
        handle = None
        try:
            attempt = self.ledger.bump_attempts(task_id)
            self.ledger.set_status(task_id, "running")
            prompt = self.build_prompt(
                task, attempt_context,
                fix_context(self.project_root, task) if attempt >= 2 else "")
            handle = self._spawn(task, thinking or task["thinking"], attempt)
            self._send_prompt(handle, prompt)
        except (ValueError, FileNotFoundError) as exc:
            self._fail_spawn(task_id, exc, handle)
            raise
        except (OSError, BrokenPipeError) as exc:
            self._fail_spawn(task_id, exc, handle)
            return {"task_id": task_id, "outcome": "spawn_failed", "missing": [],
                    "scan": scan_trace(""), "server_launches": []}
        return self._supervise([handle], self.config["worker"]["timeout_s"])[0]

    def run_wave(self, wave: int, concurrency: int | None = None) -> list[dict]:
        concurrency = concurrency or self.config["swarm"]["concurrency"]
        tasks = self.ledger.list_tasks(status="queued", wave=wave)
        if not tasks:
            return []
        if (self.project_root / "package.json").exists() and \
                not (self.project_root / "node_modules").exists():
            self.ledger.record_event(tasks[0]["id"], "wave-abort",
                                     "node_modules missing; refusing to dispatch")
            raise WaveAborted("node_modules missing; install dependencies first")
        self._wait_ready()
        results = []
        live = []
        try:
            for start in range(0, len(tasks), concurrency):
                chunk = tasks[start:start + concurrency]
                handles = []
                for task in chunk:
                    handle = None
                    try:
                        attempt = self.ledger.bump_attempts(task["id"])
                        self.ledger.set_status(task["id"], "running")
                        prompt = self.build_prompt(
                            task, "",
                            fix_context(self.project_root, task) if attempt >= 2 else "")
                        handle = self._spawn(task, task["thinking"], attempt)
                        self._send_prompt(handle, prompt)
                    except (ValueError, FileNotFoundError) as exc:
                        self._fail_spawn(task["id"], exc, handle)
                        raise WaveAborted(f"cannot start workers: {exc}")
                    except (OSError, BrokenPipeError) as exc:
                        self._fail_spawn(task["id"], exc, handle)
                        continue
                    handles.append(handle)
                    live.append(handle)
                    time.sleep(self.config["swarm"]["stagger_s"])
                results.extend(self._supervise(handles,
                                               self.config["worker"]["timeout_s"]))
                for handle in handles:
                    if handle in live:
                        live.remove(handle)
        finally:
            # workers run in their own session; on interrupt nothing else reaches them
            for handle in live:
                if handle["proc"].poll() is None:
                    kill_tree(handle["proc"].pid, handle.get("pgid"))
        # spiral retry pass (proven recovery: lower thinking + concise directive)
        retry_ctx = ("\n\nRETRY CONTEXT: your previous attempt exhausted the output budget "
                     "while planning. Do not restate the spec; decide quickly, write the "
                     "code first, then tests.")
        for result in list(results):
            task = self.ledger.get(result["task_id"])
            if task and task["status"] == "failed" \
                    and "spiral" in (task.get("verdict") or "") and task["attempts"] < 2:
                self.ledger.record_event(task["id"], "retry", "spiral -> lower thinking")
                results.append(self.run_task(
                    task["id"],
                    thinking=self.config["worker"]["retry_thinking"],
                    attempt_context=retry_ctx,
                ))
        return results

    def run_inline(self, prompt: str, thinking: str = "off",
                   timeout_s: float = 120) -> dict:
        """Spawn a single throwaway session (smokes/probes)."""
        task = {"id": "inline", "owner_files": [], "thinking": thinking}
        try:
            handle = self._spawn(task, thinking, attempt=0)
            self._send_prompt(handle, prompt)
        except (OSError, BrokenPipeError) as exc:
            raise ValueError(f"worker could not be started: {exc}") from exc
        return self._supervise([handle], timeout_s)[0]
