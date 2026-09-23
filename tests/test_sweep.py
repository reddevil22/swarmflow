"""Process sweep: attribution, born-in-wave diffing, kill policy."""

import os
import subprocess
import sys
import time

from swarmflow import procs
from swarmflow.sweep import find_orphans, find_stale, run_sweep


def _entry(ppid, name, cmdline, cwd="", create_time=0.0):
    return {"ppid": ppid, "name": name, "cmdline": cmdline, "cwd": cwd,
            "create_time": create_time}


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    """Bounded wait: returns as soon as the predicate holds (no fixed sleeps)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_find_orphans_splits_born_and_preexisting(tmp_path):
    root = str(tmp_path)
    before = {111: _entry(1, "node", f"node {root}/old.js", create_time=1.0)}
    after = {
        111: _entry(1, "node", f"node {root}/old.js", create_time=1.0),
        222: _entry(1, "node", "node server.js", cwd=root, create_time=99.0),
        333: _entry(1, "node", "node unrelated.js", cwd="/elsewhere", create_time=99.0),
    }
    found = find_orphans(before, after, root, wave_start=50.0, self_pid=999)
    assert [record["pid"] for record in found["orphans"]] == [222]
    assert [record["pid"] for record in found["pre_existing"]] == [111]


def test_pid_reuse_counts_as_born(tmp_path):
    root = str(tmp_path)
    before = {222: _entry(1, "node", f"node {root}/a.js", create_time=1.0)}
    after = {222: _entry(1, "node", f"node {root}/a.js", create_time=99.0)}
    found = find_orphans(before, after, root, wave_start=50.0, self_pid=999)
    assert [record["pid"] for record in found["orphans"]] == [222]


def test_ignore_names_and_windows_paths(tmp_path):
    root = str(tmp_path)
    after = {
        1: _entry(1, "node", f"node {root}\\server.js", create_time=99.0),
        2: _entry(1, "myserver", f"myserver {root}/x", create_time=99.0),
    }
    found = find_orphans({}, after, root, wave_start=0.0,
                         settings={"ignore_names": ["myserver"]}, self_pid=999)
    assert [record["pid"] for record in found["orphans"]] == [1]


def test_own_descendants_are_excluded(tmp_path):
    root = str(tmp_path)
    after = {5: _entry(999, "node", f"node {root}/helper.js", create_time=99.0)}
    found = find_orphans({}, after, root, wave_start=0.0, self_pid=999)
    assert found["orphans"] == []


def test_find_stale_reports_listeners_only(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr("swarmflow.sweep.listening_ports", lambda: {7: [3000], 8: []})
    current = {7: _entry(1, "node", f"node {root}/server.js", create_time=1.0),
               8: _entry(1, "node", f"node {root}/other.js", create_time=1.0)}
    stale = find_stale(root, current, {}, self_pid=999)
    assert [record["pid"] for record in stale] == [7]
    assert stale[0]["ports"] == [3000]


def test_run_sweep_kill_requires_port(tmp_path, monkeypatch):
    root = str(tmp_path)
    killed = []
    monkeypatch.setattr("swarmflow.sweep.listening_ports", lambda: {7: [3000]})
    monkeypatch.setattr("swarmflow.sweep.kill_tree",
                        lambda pid, pgid=None: killed.append(pid) or True)
    after = {7: _entry(1, "node", f"node {root}/server.js", create_time=99.0),
             9: _entry(1, "unknownthing", f"unknownthing {root}/x", create_time=99.0)}
    result = run_sweep(root, 1, {}, after, wave_start=50.0,
                       config={"sweep": {"mode": "kill"}})
    assert result["killed"] == [7]
    assert result["counts"] == {"orphans": 2, "pre_existing": 0, "killed": 1}


def test_run_sweep_warn_mode_leaves_processes_alone(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr("swarmflow.sweep.listening_ports", lambda: {7: [3000]})
    after = {7: _entry(1, "node", f"node {root}/server.js", create_time=99.0)}
    result = run_sweep(root, 1, {}, after, wave_start=50.0,
                       config={"sweep": {"mode": "warn"}})
    assert result["killed"] == []
    assert result["counts"]["orphans"] == 1


def test_run_sweep_without_snapshot_is_indeterminate(tmp_path):
    result = run_sweep(str(tmp_path), 1, None, None, 0.0, {"sweep": {}})
    assert result["indeterminate"] is True


def test_run_sweep_kill_terminates_a_real_orphan(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    script = root / "serve.py"
    script.write_text("import time; time.sleep(60)\n", encoding="utf-8")
    starter = ("import subprocess, sys\n"
               f"subprocess.Popen([sys.executable, r'{script}'], stdout=subprocess.DEVNULL, "
               "stderr=subprocess.DEVNULL)\n")
    subprocess.run([sys.executable, "-c", starter], check=True)

    def orphan_pid():
        found = [pid for pid, entry in procs.snapshot().items()
                 if str(script) in entry["cmdline"]]
        return found[0] if found else None

    assert _wait_until(lambda: orphan_pid() is not None), "orphan was not started"
    pid = orphan_pid()
    try:
        after = procs.snapshot()
        result = run_sweep(str(root), 1, {}, after, wave_start=0.0,
                           config={"sweep": {"mode": "kill",
                                             "kill_requires_port": False}})
        assert pid in result["killed"]
        assert _wait_until(lambda: not procs.is_alive(pid))
    finally:
        if procs.is_alive(pid):
            procs.kill_tree(pid, pid)


def test_sweep_never_reports_the_calling_process(tmp_path):
    result = run_sweep(str(tmp_path), 1, {}, procs.snapshot(), wave_start=0.0,
                       config={"sweep": {"mode": "warn"}})
    reported = [record["pid"] for record in
                result["orphans"] + result["pre_existing"]]
    assert os.getpid() not in reported
