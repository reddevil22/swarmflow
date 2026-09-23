"""Worker supervision: concurrent deadlines, bounded kills, spawn failures."""

import io
import json
import os
import subprocess
import sys
import time

import pytest

from swarmflow import procs
from swarmflow.ledger import Ledger
from swarmflow.workers import WaveAborted, WorkerRunner

BODY_SLEEP = "import sys, time\nsys.stdin.read()\ntime.sleep(60)\n"
BODY_TURNS = ("import sys, time\nsys.stdin.read()\n"
              "print('{\"type\": \"turn_end\"}')\n"
              "print('{\"type\": \"turn_end\"}')\n"
              "print('{\"type\": \"turn_end\"}')\n"
              "sys.stdout.flush()\n"
              "time.sleep(60)\n")


def _fake_pi(tmp_path, body: str) -> str:
    script = tmp_path / "fake_pi.py"
    script.write_text(body, encoding="utf-8")
    if os.name == "nt":
        shim = tmp_path / "fake_pi.cmd"
        shim.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        return str(shim)
    shim = tmp_path / "fake_pi.sh"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                    encoding="utf-8")
    os.chmod(shim, 0o755)
    return str(shim)


def _config(tmp_path, **overrides):
    worker = {"model": "fake/model", "pi_cli": "", "node": "node", "thinking": "high",
              "retry_thinking": "medium", "timeout_s": 10, "max_turns": 10,
              "max_output_tokens": 32768, "poll_s": 0.2, "kill_grace_s": 0.3}
    worker.update(overrides)
    return {"worker": worker,
            "swarm": {"concurrency": 8, "stagger_s": 0.0, "metrics_url": "",
                      "backpressure_waiting": 1, "backpressure_kv": 1.0},
            "paths": {"logs_dir": "logs"}, "audit": {"ignore_extra": []},
            "regression": {"enabled": False}}


def _runner(tmp_path, config, task_ids):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    ledger = Ledger(str(tmp_path / "l.db"))
    for task_id in task_ids:
        ledger.add_task(task_id, str(project), wave=1, owner_files=[])
    runner = WorkerRunner(config, ledger, str(project))
    return runner, ledger


def _verdict(ledger, task_id):
    return json.loads(ledger.get(task_id)["verdict"])


def test_supervise_kills_every_handle_on_its_own_deadline(tmp_path):
    config = _config(tmp_path, pi_cli=_fake_pi(tmp_path, BODY_SLEEP), timeout_s=4)
    runner, ledger = _runner(tmp_path, config, ["T1", "T2"])
    started = time.monotonic()
    results = runner.run_wave(1, concurrency=2)
    elapsed = time.monotonic() - started
    verdicts = [_verdict(ledger, task_id) for task_id in ("T1", "T2")]
    ledger.close()

    assert len(results) == 2
    # sequential supervision would take 2 x timeout (8s); the bound leaves CI slack
    assert elapsed < 7.0, f"handles were supervised sequentially ({elapsed:.1f}s)"
    assert [verdict["killed_for"] for verdict in verdicts] == ["timeout", "timeout"]


def test_expired_handles_are_finalized_in_one_pass(tmp_path, monkeypatch):
    """Both handles pass their deadline in the same sweep, so both are finalized in it.
    A supervisor that finalized one handle per pass would either sleep while the other
    was still due (sentinel below) or never finalize it at all (KeyError on the way
    out): either way this test fails."""
    project = tmp_path / "proj"
    project.mkdir()
    trace = tmp_path / "t.jsonl"
    trace.write_text('{"type": "agent_end"}\n', encoding="utf-8")
    config = _config(tmp_path, timeout_s=1, poll_s=30)
    runner, ledger = _runner(tmp_path, config, ["T1", "T2"])

    class LiveProc:
        pid = 999999

        def poll(self):
            return None                    # still running: the deadline decides

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    class Sink:
        def close(self):
            pass

    def sentinel(seconds):
        raise AssertionError(f"the supervisor slept between handles ({seconds}s)")

    monkeypatch.setattr("swarmflow.workers.kill_tree", lambda pid, pgid=None: True)
    monkeypatch.setattr("swarmflow.workers.time.sleep", sentinel)
    handles = [{"proc": LiveProc(), "out": Sink(), "trace": str(trace),
                "task": ledger.get(task_id), "thinking": "high", "before": {},
                "pgid": None, "started": time.monotonic() - 10}
               for task_id in ("T1", "T2")]

    results = runner._supervise(handles, timeout_s=1)

    assert [result["task_id"] for result in results] == ["T1", "T2"]
    assert [_verdict(ledger, task_id)["killed_for"] for task_id in ("T1", "T2")] == \
        ["timeout", "timeout"]
    ledger.close()


def test_turn_cap_kills_the_session(tmp_path):
    config = _config(tmp_path, pi_cli=_fake_pi(tmp_path, BODY_TURNS), max_turns=2,
                     timeout_s=30)
    runner, ledger = _runner(tmp_path, config, ["T1"])
    results = runner.run_wave(1, concurrency=1)
    verdict = _verdict(ledger, "T1")
    ledger.close()

    assert results[0]["outcome"] == "turn_cap"
    assert verdict["killed_for"] == "turn_cap"


def test_kill_failure_is_recorded_and_bounded(tmp_path, monkeypatch):
    config = _config(tmp_path, pi_cli=_fake_pi(tmp_path, BODY_SLEEP), timeout_s=1,
                     kill_grace_s=0.2)
    handles = []
    original_spawn = WorkerRunner._spawn

    def spawn(self, task, thinking, attempt):
        handle = original_spawn(self, task, thinking, attempt)
        handle["proc"].kill = lambda: None       # simulate an unkillable child
        handles.append(handle)
        return handle

    monkeypatch.setattr(WorkerRunner, "_spawn", spawn)
    monkeypatch.setattr("swarmflow.workers.kill_tree", lambda pid, pgid=None: False)
    runner, ledger = _runner(tmp_path, config, ["T1"])
    started = time.monotonic()
    try:
        results = runner.run_wave(1, concurrency=1)
        elapsed = time.monotonic() - started
        verdict = _verdict(ledger, "T1")
    finally:
        for handle in handles:
            procs.kill_tree(handle["proc"].pid, handle.get("pgid"))
    ledger.close()

    assert elapsed < 20.0, "the supervisor waited unbounded for an unkillable child"
    assert results[0]["outcome"] == "kill_failed"
    assert verdict["killed_for"] == "kill_failed"


def test_missing_model_aborts_the_wave(tmp_path):
    config = _config(tmp_path, model="")
    runner, ledger = _runner(tmp_path, config, ["T1"])
    with pytest.raises(WaveAborted):
        runner.run_wave(1, concurrency=1)
    task = ledger.get("T1")
    kinds = [event["kind"] for event in ledger.events("T1", limit=20)]
    ledger.close()

    assert task["status"] == "failed"
    assert json.loads(task["verdict"])["outcome"] == "spawn_failed"
    assert "spawn-failed" in kinds


def test_unlaunchable_worker_fails_the_task_not_the_wave(tmp_path):
    bad = tmp_path / "not_executable.txt"
    bad.write_text("nope", encoding="utf-8")
    config = _config(tmp_path, pi_cli=str(bad))
    runner, ledger = _runner(tmp_path, config, ["T1"])
    results = runner.run_wave(1, concurrency=1)
    task = ledger.get("T1")
    ledger.close()

    assert results == []
    assert task["status"] == "failed"
    assert json.loads(task["verdict"])["outcome"] == "spawn_failed"


def test_delivery_reports_the_files_the_task_changed(tmp_path):
    """The delivery scan hands the per-task file list to the caller (used for commits)."""
    body = ("import sys\n"
            "sys.stdin.read()\n"
            "open('feature.py', 'w', encoding='utf-8').write('delivered\\n')\n"
            "print('{\"type\": \"agent_end\"}')\n"
            "sys.stdout.flush()\n")
    config = _config(tmp_path, pi_cli=_fake_pi(tmp_path, body), timeout_s=15)
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.add_task("T1", str(project), wave=1, owner_files=["feature.py"])
    runner = WorkerRunner(config, ledger, str(project))
    result = runner.run_task("T1")
    ledger.close()
    assert result["outcome"] == "delivered"
    assert result["changed"] == ["feature.py"]


def test_broken_pipe_marks_the_task_failed(tmp_path, monkeypatch):
    class ExplodingStdin:
        def write(self, data):
            raise BrokenPipeError("child already gone")

        def close(self):
            pass

    class FakeProc:
        pid = 999999
        stdin = ExplodingStdin()

        def poll(self):
            return 1

    def spawn(self, task, thinking, attempt):
        return {"proc": FakeProc(), "out": io.BytesIO(),
                "trace": str(tmp_path / "t.jsonl"), "task": task, "thinking": thinking,
                "before": {}, "pgid": None, "started": time.monotonic()}

    monkeypatch.setattr(WorkerRunner, "_spawn", spawn)
    config = _config(tmp_path)
    runner, ledger = _runner(tmp_path, config, ["T1"])
    result = runner.run_task("T1")
    task = ledger.get("T1")
    ledger.close()

    assert result["outcome"] == "spawn_failed"
    assert task["status"] == "failed"


def test_interrupt_tears_down_live_workers(tmp_path, monkeypatch):
    pids = []
    original_spawn = WorkerRunner._spawn

    def spawn(self, task, thinking, attempt):
        handle = original_spawn(self, task, thinking, attempt)
        pids.append(handle["proc"].pid)
        return handle

    def boom(self, handles, timeout_s):
        raise KeyboardInterrupt

    monkeypatch.setattr(WorkerRunner, "_spawn", spawn)
    monkeypatch.setattr(WorkerRunner, "_supervise", boom)
    config = _config(tmp_path, pi_cli=_fake_pi(tmp_path, BODY_SLEEP))
    runner, ledger = _runner(tmp_path, config, ["T1"])
    with pytest.raises(KeyboardInterrupt):
        runner.run_wave(1, concurrency=1)
    ledger.close()

    time.sleep(1)
    try:
        assert pids, "no worker was spawned"
        assert all(not procs.is_alive(pid) for pid in pids)
    finally:
        for pid in pids:
            if procs.is_alive(pid):
                procs.kill_tree(pid, pid)
