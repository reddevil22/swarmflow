"""Process primitives: snapshots, listening ports, tree-safe kills."""

import os
import subprocess
import sys
import time

import pytest

from swarmflow import procs


def _spawn_sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


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
        time.sleep(2)
        data = procs.snapshot()
        grandchildren = [pid for pid, entry in data.items()
                         if entry["ppid"] == proc.pid and pid != proc.pid]
        assert grandchildren, "grandchild sleeper was not started"
        assert procs.kill_tree(proc.pid, proc.pid) is True
        time.sleep(1)
        assert not procs.is_alive(proc.pid)
        assert all(not procs.is_alive(pid) for pid in grandchildren)
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
        found = False
        for _ in range(25):
            time.sleep(0.2)
            if port in procs.listening_ports().get(proc.pid, []):
                found = True
                break
        assert found, f"port {port} was not attributed to pid {proc.pid}"
    finally:
        proc.kill()
        proc.wait()
