"""Process primitives: snapshots, listening ports, tree-safe termination.

Workers run agents that spawn grandchildren (npx -> node -> vite); killing only the
direct child leaves servers alive to poison later probes. Everything here exists so the
control plane can see and (when told to) terminate a whole tree without ever signalling
its own process group.
"""

import os
import signal
import subprocess

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None


def available() -> bool:
    return psutil is not None


def format_ports(ports) -> str:
    """Render a port list for reports; a dash when there is none."""
    return ", ".join(str(port) for port in ports or []) or "-"


def spawn_flags() -> dict:
    """Popen kwargs that make the child a session/group leader on POSIX."""
    return {"start_new_session": True} if os.name != "nt" else {}


def snapshot() -> dict:
    """Map pid -> {ppid, name, cmdline, cwd, create_time}. Empty on failure."""
    if psutil is None:
        return {}
    processes = {}
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time"]):
        info = proc.info
        pid = info.get("pid")
        if pid is None:
            continue
        try:
            cwd = proc.cwd()
        except (psutil.Error, OSError):
            cwd = ""
        processes[pid] = {"ppid": info.get("ppid") or 0,
                          "name": info.get("name") or "",
                          "cmdline": " ".join(info.get("cmdline") or []),
                          "cwd": cwd,
                          "create_time": info.get("create_time") or 0.0}
    return processes


def listening_ports() -> dict:
    """Map pid -> sorted list of TCP ports in LISTEN state."""
    if psutil is None:
        return {}
    try:
        connections = psutil.net_connections("tcp")
    except (psutil.Error, OSError):
        return {}
    ports = {}
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or not connection.pid:
            continue
        address = connection.laddr
        port = getattr(address, "port", None) if address else None
        if port:
            ports.setdefault(connection.pid, set()).add(port)
    return {pid: sorted(values) for pid, values in ports.items()}


def is_alive(pid: int) -> bool:
    if psutil is not None:
        try:
            return psutil.pid_exists(pid)
        except psutil.Error:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid: int, pgid: int | None = None) -> bool:
    """Terminate a process tree.

    Never signals the caller's own process group: the process-group path is taken only
    when the target IS the group leader (which is how swarmflow spawns its children).
    """
    if not pid or pid == os.getpid():
        return False
    if os.name != "nt":
        target = pgid or pid
        if target == pid and target != os.getpgrp():
            try:
                os.killpg(target, signal.SIGKILL)
                return True
            except (ProcessLookupError, PermissionError, OSError):
                pass
    else:
        try:
            proc = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                  capture_output=True, text=True, timeout=60)
            if proc.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    if psutil is not None:
        try:
            parent = psutil.Process(pid)
            victims = parent.children(recursive=True) + [parent]
        except psutil.Error:
            return False
        for victim in victims:
            try:
                victim.kill()
            except psutil.Error:
                continue
        return True
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except OSError:
        return False
