"""Local worker sessions: spawn Pi + the local model, run tasks, audit artifacts.

Success is judged by artifacts (trace contents + owned files on disk), never by exit
codes: the stress test showed rc=0 alongside a completely empty deliverable.
"""

import json
import subprocess
import time
import urllib.request
from pathlib import Path

from .config import build_cli_command, resolve_executable


def scan_trace(path: str, max_output_tokens: int = 32768) -> dict:
    """Analyze a Pi --mode json trace file. Returns a summary used for classification."""
    scan = {
        "exists": False,
        "turns": 0,
        "tool_calls": 0,
        "out_tokens": 0,
        "has_agent_end": False,
        "last_text": "",
        "spiral": False,
    }
    trace = Path(path)
    if not trace.exists():
        return scan
    scan["exists"] = True
    events = []
    for line in trace.open(encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    for event in events:
        etype = event.get("type")
        if etype == "turn_end":
            scan["turns"] += 1
        elif etype == "message_end":
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            scan["out_tokens"] += usage.get("output") or 0
            if message.get("role") == "assistant":
                for item in message.get("content") or []:
                    if isinstance(item, dict):
                        if item.get("name"):
                            scan["tool_calls"] += 1
                        if item.get("type") == "text" and item.get("text"):
                            scan["last_text"] = item["text"][-4000:]
        elif etype == "agent_end":
            scan["has_agent_end"] = True
    scan["spiral"] = scan["out_tokens"] >= 0.9 * max_output_tokens and not scan["last_text"].strip()
    return scan


def classify(scan: dict, missing_files: list) -> str:
    """Map a scan + artifact audit to an outcome label."""
    if scan["spiral"]:
        return "spiral"
    if not scan["has_agent_end"]:
        return "no_agent_end"
    if missing_files:
        return "missing_artifacts"
    return "delivered"


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


FORBIDDEN_ACTIONS = [
    "npm install", "npm ci", "npm i ", "npm add", "npm update",
    "pnpm", "yarn add", "yarn install", "bun install",
    "rm -rf node_modules", "rmdir /s", "npm audit fix",
]


def scan_forbidden(trace_path: str) -> list[str]:
    """Return worker bash commands that violate the dependency envelope."""
    hits = []
    trace = Path(trace_path)
    if not trace.exists():
        return hits
    for line in trace.open(encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "message_end":
            continue
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            continue
        for item in message.get("content") or []:
            if isinstance(item, dict) and item.get("name") == "bash":
                args = item.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                cmd = str((args or {}).get("command", ""))
                lowered = cmd.lower()
                for pattern in FORBIDDEN_ACTIONS:
                    if pattern in lowered:
                        hits.append(cmd[:200])
                        break
    return hits


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
        proc = subprocess.Popen(
            cmd, cwd=str(self.project_root), stdin=subprocess.PIPE,
            stdout=out, stderr=subprocess.STDOUT,
        )
        return {"proc": proc, "out": out, "trace": str(trace_path), "task": task,
                "thinking": thinking}

    def _finalize(self, handle: dict, timeout_s: float) -> dict:
        proc, out, task = handle["proc"], handle["out"], handle["task"]
        cap = self.config["worker"]["max_output_tokens"]
        turn_cap = int(self.config["worker"].get("max_turns", 45))
        killed_for = None
        deadline = time.time() + timeout_s
        while proc.poll() is None:
            if time.time() > deadline:
                killed_for = "timeout"
                proc.kill()
                break
            time.sleep(15)
            if scan_trace(handle["trace"], cap)["turns"] > turn_cap:
                killed_for = "turn_cap"
                proc.kill()
                break
        proc.wait()
        out.close()
        scan = scan_trace(handle["trace"], cap)
        forbidden = scan_forbidden(handle["trace"])
        missing = [f for f in task.get("owner_files", [])
                   if not (self.project_root / f).exists()]
        if killed_for == "turn_cap":
            outcome = "turn_cap"
        elif forbidden:
            outcome = "scope_violation"
        else:
            outcome = classify(scan, missing)
        status = "delivered" if outcome == "delivered" else "failed"
        self.ledger.set_status(
            task["id"], status,
            worker_trace=handle["trace"],
            artifacts=[f for f in task.get("owner_files", []) if (self.project_root / f).exists()],
            verdict=json.dumps({"outcome": outcome, "missing": missing,
                                "turns": scan["turns"], "out_tokens": scan["out_tokens"],
                                "killed_for": killed_for, "forbidden": forbidden[:3]}),
        )
        return {"task_id": task["id"], "outcome": outcome, "missing": missing, "scan": scan}

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

    def build_prompt(self, task: dict, attempt_context: str = "") -> str:
        template = (self.repo_root / "prompts" / "task_brief.md").read_text(encoding="utf-8")
        spec_path = task.get("spec_path")
        spec = "(see SPEC.md)"
        if spec_path and Path(spec_path).exists():
            spec = Path(spec_path).read_text(encoding="utf-8")
        acceptance = "\n".join(f"- {a}" for a in task.get("acceptance") or []) or "- (see SPEC.md)"
        test_command = task.get("test_command") or \
            "your test file(s), e.g. `npx jest <your-test-file>`"
        prompt = template.format(
            task_id=task["id"],
            project_root=str(self.project_root),
            owner_files="\n".join(f"- {f}" for f in task.get("owner_files") or []) or "(none listed)",
            spec=spec,
            acceptance=acceptance,
            test_command=test_command,
            must_keep_working="Run the project test suite if one exists.",
        )
        return prompt + attempt_context

    def run_task(self, task_id: str, thinking: str | None = None,
                 attempt_context: str = "") -> dict:
        task = self.ledger.get(task_id)
        if not task:
            raise ValueError(f"unknown task: {task_id}")
        attempt = self.ledger.bump_attempts(task_id)
        self.ledger.set_status(task_id, "running")
        prompt = self.build_prompt(task, attempt_context)
        handle = self._spawn(task, thinking or task["thinking"], attempt)
        self._send_prompt(handle, prompt)
        return self._finalize(handle, self.config["worker"]["timeout_s"])

    def run_wave(self, wave: int, concurrency: int | None = None) -> list[dict]:
        concurrency = concurrency or self.config["swarm"]["concurrency"]
        tasks = self.ledger.list_tasks(status="queued", wave=wave)
        if not tasks:
            return []
        if (self.project_root / "package.json").exists() and \
                not (self.project_root / "node_modules").exists():
            self.ledger.record_event(tasks[0]["id"], "wave-abort",
                                     "node_modules missing; refusing to dispatch")
            return []
        self._wait_ready()
        results = []
        for start in range(0, len(tasks), concurrency):
            chunk = tasks[start:start + concurrency]
            handles = []
            for task in chunk:
                attempt = self.ledger.bump_attempts(task["id"])
                self.ledger.set_status(task["id"], "running")
                prompt = self.build_prompt(task)
                handle = self._spawn(task, task["thinking"], attempt)
                self._send_prompt(handle, prompt)
                handles.append(handle)
                time.sleep(self.config["swarm"]["stagger_s"])
            for handle in handles:
                results.append(self._finalize(handle, self.config["worker"]["timeout_s"]))
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
        handle = self._spawn(task, thinking, attempt=0)
        self._send_prompt(handle, prompt)
        return self._finalize(handle, timeout_s)
