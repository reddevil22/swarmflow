"""Local worker sessions: spawn Pi + the local model, run tasks, audit artifacts.

Success is judged by artifacts (trace contents + owned files on disk), never by exit
codes: the stress test showed rc=0 alongside a completely empty deliverable.
"""

import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from .audit import _hash_file
from .config import build_cli_command, resolve_executable
from .procs import kill_tree, spawn_flags
from .recon import load_recon


class WaveAborted(Exception):
    """Raised when wave preconditions are not met and nothing was dispatched."""


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


STACK_RULES = {
    "node": [
        "- This project uses ONLY npm and npx. Never use pnpm, yarn, or bun.",
        "- Test-runner cold starts are slow: run the verification command once per fix "
        "cycle, never in a loop.",
        "- NEVER read, grep, list, or explore node_modules.",
        "- NEVER modify package.json, package-lock.json, or tool configs (tsconfig, "
        "jest configs, bundler configs).",
    ],
    "python": [
        "- Run tests only through the verification command above (the project's own "
        "runner).",
        "- NEVER run pip/pip3/python -m pip install, poetry, or uv: environments are "
        "managed by the operator.",
        "- NEVER modify pyproject.toml, requirements*.txt, setup.py/cfg, or lock files.",
    ],
    "go": [
        "- Run tests only through the verification command above.",
        "- NEVER run go get or go mod tidy; do not touch go.mod or go.sum.",
    ],
    "rust": [
        "- Run tests only through the verification command above.",
        "- NEVER run cargo add or cargo update; do not touch Cargo.toml or Cargo.lock.",
    ],
    "_generic": [
        "- Use only the verification command above to run tests.",
        "- NEVER install or upgrade tools or dependencies.",
    ],
}

STACK_RULES_COMMON = [
    "- NEVER modify files you do not own, including tool and dependency configs.",
    "- Do not create scratch/temporary files; delete anything you create by accident.",
    "- Never start long-running servers or watchers (dev servers, preview servers, "
    "--watch). A test server must come from the injected verification command.",
    "- If the SAME failure persists after 3 fix attempts, stop immediately and report a "
    "BLOCKED section with the exact command, the exact output, and what you tried.",
    "- Stay under ~40 tool calls. Reading your own code beats shell experimentation.",
]


def stack_rules_block(stacks: list, has_package_json: bool = False) -> str:
    """Compose the environment-rules block for the detected stacks."""
    chosen = [stack for stack in stacks if stack in STACK_RULES]
    if not chosen:
        chosen = ["node"] if has_package_json else ["_generic"]
    lines = ["## ENVIRONMENT RULES (violations cause rejection)", ""]
    for stack in chosen:
        lines.extend(STACK_RULES[stack])
    lines.extend(STACK_RULES_COMMON)
    return "\n".join(lines)


def delivery_changed(project_root: Path, before: dict) -> bool:
    """True if any owned file was created, deleted, or modified since the snapshot."""
    for rel, digest in (before or {}).items():
        path = Path(project_root) / rel
        now = _hash_file(path) if path.exists() else None
        if now != digest:
            return True
    return False


FORBIDDEN_ACTIONS = [
    "npm install", "npm ci", "npm i ", "npm add", "npm update",
    "pnpm", "yarn add", "yarn install", "bun install",
    "rm -rf node_modules", "rmdir /s", "npm audit fix",
    "pip install", "pip3 install", "python -m pip install",
    "poetry add", "poetry install", "uv add", "uv pip install",
    "go get", "cargo add", "cargo update",
]


def _iter_bash_commands(trace_path: str):
    """Yield the bash commands a session issued (assistant tool calls only)."""
    trace = Path(trace_path)
    if not trace.exists():
        return
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
            if not isinstance(item, dict) or item.get("name") != "bash":
                continue
            args = item.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            command = str((args or {}).get("command", ""))
            if command:
                yield command


SERVER_LAUNCH_RE = re.compile(
    r"(npm run dev|npm start|yarn dev|pnpm dev|next dev|webpack serve|nodemon|ts-node|"
    r"uvicorn|flask run|gunicorn|python -m http\.server|\bvite(?!st)\b|--watch)")


def scan_server_launches(trace_path: str) -> list:
    """Bash commands that start long-running servers/watchers. Evidence, not a failure:
    the sweep (and the operator) need to know what was left behind."""
    launches = []
    for command in _iter_bash_commands(trace_path):
        if SERVER_LAUNCH_RE.search(command.lower()):
            launches.append(command[:200])
    return launches


def scan_forbidden(trace_path: str) -> list[str]:
    """Return worker bash commands that violate the dependency envelope."""
    hits = []
    for command in _iter_bash_commands(trace_path):
        lowered = command.lower()
        for pattern in FORBIDDEN_ACTIONS:
            if pattern in lowered:
                hits.append(command[:200])
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
            stdout=out, stderr=subprocess.STDOUT, **spawn_flags(),
        )
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
                "thinking": thinking, "before": before, "pgid": pgid}

    def _finalize(self, handle: dict, timeout_s: float) -> dict:
        proc, out, task = handle["proc"], handle["out"], handle["task"]
        cap = self.config["worker"]["max_output_tokens"]
        turn_cap = int(self.config["worker"].get("max_turns", 45))
        killed_for = None
        deadline = time.time() + timeout_s
        while proc.poll() is None:
            if time.time() > deadline:
                killed_for = "timeout"
                kill_tree(proc.pid, handle.get("pgid"))
                break
            time.sleep(15)
            if scan_trace(handle["trace"], cap)["turns"] > turn_cap:
                killed_for = "turn_cap"
                kill_tree(proc.pid, handle.get("pgid"))
                break
        proc.wait()
        # a session that exits cleanly can still leave a server/watcher behind; on
        # Windows the parent link is gone by now, so the sweep covers that case instead
        # of taskkill-ing a pid that could have been reused
        if os.name != "nt":
            kill_tree(proc.pid, handle.get("pgid"))
        if proc.poll() is None:
            proc.kill()
        out.close()
        scan = scan_trace(handle["trace"], cap)
        forbidden = scan_forbidden(handle["trace"])
        launches = scan_server_launches(handle["trace"])
        missing = [f for f in task.get("owner_files", [])
                   if not (self.project_root / f).exists()]
        if killed_for == "turn_cap":
            outcome = "turn_cap"
        elif forbidden:
            outcome = "scope_violation"
        else:
            outcome = classify(scan, missing)
        if outcome == "delivered" and not delivery_changed(self.project_root,
                                                           handle.get("before")):
            outcome = "no_changes"
        status = "delivered" if outcome == "delivered" else "failed"
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
        run_state = {}
        run_path = self.project_root / ".swarmflow" / "run.json"
        if run_path.exists():
            try:
                run_state = json.loads(run_path.read_text(encoding="utf-8"))
            except ValueError:
                run_state = {}
        recon = load_recon(str(self.project_root))
        brownfield = run_state.get("mode") == "brownfield"
        spec_path = task.get("spec_path")
        spec = "(see SPEC.md)"
        if spec_path and Path(spec_path).exists():
            spec = Path(spec_path).read_text(encoding="utf-8")
        acceptance = "\n".join(f"- {a}" for a in task.get("acceptance") or []) or "- (see SPEC.md)"
        test_command = task.get("test_command") or \
            "your test file(s) with the project's own test runner"
        regression_entry = ((recon.get("commands") or {}).get("regression") or {})
        must_keep_working = regression_entry.get("command") \
            or "Run the project test suite if one exists."
        owner_files = task.get("owner_files") or []
        files_to_read = task.get("files_to_read") or owner_files
        stacks = [entry.get("stack", "") for entry in (recon.get("stacks") or [])]
        if brownfield:
            rules_text = (self.repo_root / "AGENTS.worker.md").read_text(encoding="utf-8")
            worker_rules = "\n## WORKING RULES (mandatory)\n\n" + rules_text.strip() + "\n"
            conventions = recon.get("conventions") or []
            if conventions:
                context_files = ("This is an existing repository; its own conventions "
                                 f"files apply: {', '.join(conventions)}. Read them but "
                                 "do not modify them.")
            else:
                context_files = ("This is an existing repository; respect its existing "
                                 "patterns, tests, and style.")
        else:
            worker_rules = ""
            context_files = ("Read AGENTS.md first; it defines the mandatory working "
                             "rules for this swarm.")
        stack_rules = stack_rules_block(
            stacks, has_package_json=(self.project_root / "package.json").exists())
        prompt = template.format(
            context_files=context_files,
            worker_rules=worker_rules,
            stack_rules=stack_rules,
            task_id=task["id"],
            project_root=str(self.project_root),
            owner_files="\n".join(f"- {f}" for f in owner_files) or "(none listed)",
            files_to_read="\n".join(f"- {f}" for f in files_to_read) or "(none listed)",
            spec=spec,
            acceptance=acceptance,
            test_command=test_command,
            must_keep_working=must_keep_working,
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
            raise WaveAborted("node_modules missing; install dependencies first")
        self._wait_ready()
        results = []
        live = []
        try:
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
                    live.append(handle)
                    time.sleep(self.config["swarm"]["stagger_s"])
                for handle in handles:
                    results.append(self._finalize(handle,
                                                  self.config["worker"]["timeout_s"]))
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
        handle = self._spawn(task, thinking, attempt=0)
        self._send_prompt(handle, prompt)
        return self._finalize(handle, timeout_s)
