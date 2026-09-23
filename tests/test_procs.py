"""Process primitives: snapshots, listening ports, tree-safe kills."""

import os
import subprocess
import sys
import time

import psutil
import pytest

from swarmflow import procs


def _spawn_sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    """Bounded wait: returns as soon as the predicate holds (no fixed sleeps)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_snapshot_reports_spawned_process():
    proc = _spawn_sleeper()
    try:
        data = procs.snapshot()
        entry = data.get(proc.pid)
        assert entry is not None
        assert "time.sleep(60)" in entry["cmdline"]
        assert entry["create_time"] > 0
    finally:
        proc.kill()
        proc.wait()


def test_kill_tree_kills_child_and_grandchild():
    script = ("import subprocess, sys, time\n"
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
              "                 stdout=subprocess.DEVNULL)\n"
              "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        def grandchildren():
            return [pid for pid, entry in procs.snapshot().items()
                    if entry["ppid"] == proc.pid and pid != proc.pid]

        assert _wait_until(lambda: bool(grandchildren())), \
            "grandchild sleeper was not started"
        victims = grandchildren()
        assert procs.kill_tree(proc.pid, proc.pid) is True
        assert _wait_until(lambda: not procs.is_alive(proc.pid))
        assert all(not procs.is_alive(pid) for pid in victims)
        assert procs.is_alive(os.getpid())
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX-only")
def test_kill_tree_never_signals_its_own_group(monkeypatch):
    signalled = []
    monkeypatch.setattr(procs.os, "killpg", lambda pgid, sig: signalled.append(pgid))
    # a pgid that is not the pid is never signalled as a group
    assert procs.kill_tree(999999999, pgid=123) is False
    assert signalled == []
    # the caller's own group is refused even when pgid == pid
    monkeypatch.setattr(procs.os, "getpgrp", lambda: 999999999)
    assert procs.kill_tree(999999999, pgid=999999999) is False
    assert signalled == []
    # never kills itself
    assert procs.kill_tree(os.getpid()) is False
    assert procs.is_alive(os.getpid())


def _ancestors_in(snapshot, pid, limit=10):
    """Walk snapshot ppid links upward (a launcher makes the listener a child)."""
    seen = set()
    current = pid
    for _ in range(limit):
        entry = snapshot.get(current)
        if not entry:
            break
        parent = entry.get("ppid") or 0
        if not parent or parent in seen:
            break
        seen.add(parent)
        current = parent
    return seen


def test_listening_ports_maps_pid():
    import socket

    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()
    try:
        proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port),
                                 "--bind", "127.0.0.1"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pytest.skip("could not start a local http server")
    try:
        def attributed():
            owners = [pid for pid, ports in procs.listening_ports().items()
                      if port in ports]
            if not owners:
                return False
            # a venv's python.exe (or py.exe) is a launcher: the socket belongs to its
            # child, so accept any owner that descends from the spawned process
            snapshot = procs.snapshot()
            return any(pid == proc.pid or proc.pid in _ancestors_in(snapshot, pid)
                       for pid in owners)

        assert _wait_until(attributed), \
            f"port {port} was not attributed to the spawned server"
    finally:
        proc.kill()
        proc.wait()


def test_is_alive_treats_a_zombie_as_terminated():
    import signal

    if os.name == "nt":
        pytest.skip("zombies are POSIX-only")
    proc = _spawn_sleeper()
    try:
        os.kill(proc.pid, signal.SIGKILL)             # never waited: it stays a zombie
        assert _wait_until(lambda: psutil.Process(proc.pid).status()
                           == psutil.STATUS_ZOMBIE), "child never became a zombie"
        assert procs.is_alive(proc.pid) is False
    finally:
        proc.wait()


def test_is_alive_assumes_alive_when_inspection_is_denied(monkeypatch):
    def denied(pid):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(procs.psutil, "Process", denied)
    assert procs.is_alive(os.getpid()) is True


def test_is_alive_reports_a_vanished_process(monkeypatch):
    def gone(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(procs.psutil, "Process", gone)
    assert procs.is_alive(os.getpid()) is False


@pytest.mark.skipif(os.name == "nt", reason="os.kill(pid, 0) terminates on Windows")
def test_is_alive_falls_back_to_os_kill_without_psutil(monkeypatch):
    """The psutil-less fallback: os.kill(pid, 0) is an existence check on POSIX only."""
    monkeypatch.setattr(procs, "psutil", None)
    assert procs.is_alive(os.getpid()) is True
    assert procs.is_alive(999999999) is False
