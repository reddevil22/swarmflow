"""Post-wave process sweep.

Diffs process snapshots taken before and after a wave, attributes processes to the
project by command line and working directory, attaches listening ports, and reports
(optionally terminates) this wave's leaks. Pre-existing processes are reported but never
killed by a wave sweep - they may be the operator's own servers.
"""

import os
from pathlib import Path

from .procs import available, kill_tree, listening_ports, snapshot

DEFAULT_SERVER_NAMES = ["node", "vite", "ts-node", "npm", "npx", "webpack", "next"]
MAX_CHAIN = 50


def _normalize(text: str) -> str:
    return (text or "").replace("\\", "/").lower()


def _attributable(entry: dict, root: str) -> bool:
    normalized_root = _normalize(root)
    if normalized_root and normalized_root in _normalize(entry.get("cmdline", "")):
        return True
    cwd = _normalize(entry.get("cwd", ""))
    return bool(cwd) and (cwd == normalized_root or cwd.startswith(normalized_root + "/"))


def _ancestors(entries: dict, pid: int) -> set:
    """Walk the snapshot's ppid links (dangling parents end the chain)."""
    chain = set()
    current = pid
    for _ in range(MAX_CHAIN):
        entry = entries.get(current)
        if not entry:
            break
        parent = entry.get("ppid") or 0
        if not parent or parent in chain:
            break
        chain.add(parent)
        current = parent
    return chain


def _candidates(after: dict, project_root: str, settings: dict, self_pid: int):
    """Yield (pid, entry, ports) for project-attributable processes we do not own."""
    root = str(Path(project_root).resolve())
    ignore_names = {name.lower() for name in (settings.get("ignore_names") or [])}
    ignore_ports = {int(port) for port in (settings.get("ignore_ports") or [])}
    excluded = {self_pid} | _ancestors(after, self_pid)
    ports_by_pid = listening_ports()
    for pid, entry in sorted(after.items()):
        if pid in excluded:
            continue
        name = (entry.get("name") or "").lower()
        if name in ignore_names or name == "swarmflow":
            continue
        if not _attributable(entry, root):
            continue
        if _ancestors(after, pid) & excluded:
            continue
        held = [port for port in ports_by_pid.get(pid, []) if port not in ignore_ports]
        yield pid, entry, held


def _record(pid: int, entry: dict, held: list, server_names: set) -> dict:
    name = entry.get("name", "")
    return {"pid": pid, "name": name, "cmd": (entry.get("cmdline") or "")[:200],
            "cwd": entry.get("cwd", ""), "ports": held,
            "serverish": name.lower() in server_names}


def find_orphans(before: dict, after: dict, project_root: str, wave_start: float,
                 settings: dict | None = None, self_pid: int | None = None) -> dict:
    """Split attributable processes into this wave's orphans and pre-existing ones."""
    settings = settings or {}
    self_pid = self_pid or os.getpid()
    server_names = {name.lower() for name in
                    (settings.get("server_names") or DEFAULT_SERVER_NAMES)}
    orphans, pre_existing = [], []
    for pid, entry, held in _candidates(after, project_root, settings, self_pid):
        record = _record(pid, entry, held, server_names)
        born = pid not in before or (entry.get("create_time") or 0) > wave_start
        (orphans if born else pre_existing).append(record)
    return {"orphans": orphans, "pre_existing": pre_existing}


def find_stale(project_root: str, current: dict, settings: dict | None = None,
               self_pid: int | None = None) -> list:
    """Attributable processes already listening (start-of-run listener check)."""
    settings = settings or {}
    self_pid = self_pid or os.getpid()
    server_names = {name.lower() for name in
                    (settings.get("server_names") or DEFAULT_SERVER_NAMES)}
    return [_record(pid, entry, held, server_names)
            for pid, entry, held in _candidates(current, project_root, settings, self_pid)
            if held]


def _skipped(wave: int, reason: str) -> dict:
    return {"version": 1, "wave": wave, "skipped": reason, "indeterminate": False,
            "orphans": [], "pre_existing": [], "killed": [], "counts": {}}


def run_sweep(project_root: str, wave: int, before: dict, after: dict, wave_start: float,
              config: dict, mode: str | None = None) -> dict:
    settings = config.get("sweep") or {}
    if not settings.get("enabled", True):
        return _skipped(wave, "disabled in config")
    if before is None or after is None or not available():
        return {"version": 1, "wave": wave, "indeterminate": True,
                "reason": "process snapshot unavailable (psutil missing or blocked)",
                "orphans": [], "pre_existing": [], "killed": [], "counts": {}}
    mode = (mode or str(settings.get("mode", "warn"))).lower()
    found = find_orphans(before, after, project_root, wave_start, settings)
    orphans, pre_existing = found["orphans"], found["pre_existing"]
    killed = []
    if mode == "kill":
        require_port = bool(settings.get("kill_requires_port", True))
        for record in orphans:
            if require_port and not record["ports"] and not record["serverish"]:
                continue
            if kill_tree(record["pid"]):
                killed.append(record["pid"])
    counts = {"orphans": len(orphans), "pre_existing": len(pre_existing),
              "killed": len(killed)}
    return {"version": 1, "wave": wave, "indeterminate": False, "reason": "",
            "mode": mode, "orphans": orphans, "pre_existing": pre_existing,
            "killed": killed, "counts": counts}
