"""Session trace analysis: what a worker actually did, read from its JSONL trace.

Everything here is artifact-based evidence - turns and output tokens, the final report
text, forbidden dependency commands, and server/watcher launches - so no gate has to
trust an exit code or a worker's own summary.
"""

import json
import re
from pathlib import Path


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
    if not trace.is_file():
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
                            scan["last_text"] = item["text"][-12000:]
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
    if not trace.is_file():
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
