"""Worker loop behaviour: delivery outcomes, the spiral retry pass, admission holding.

These tests drive `_finalize_one` / `_wait_ready` / the retry pass for real; only the
process spawn is faked (a worker that has already exited), so the outcome mapping and
the ledger writes under test actually run.
"""

import json
import time

from swarmflow.audit import _hash_file
from swarmflow.ledger import Ledger
from swarmflow.workers import WorkerRunner


class _DeadProc:
    """A worker whose process has already exited cleanly."""

    pid = 999999

    def poll(self):
        return 0


class _ClosedOut:
    def close(self):
        pass


def _runner(tmp_path, project, **worker_overrides):
    worker = {"model": "fake/model", "pi_cli": "unused", "thinking": "high",
              "retry_thinking": "medium", "timeout_s": 10, "max_turns": 45,
              "max_output_tokens": 32768, "poll_s": 0.05, "kill_grace_s": 0.2}
    worker.update(worker_overrides)
    config = {"worker": worker,
              "swarm": {"concurrency": 2, "stagger_s": 0.0, "metrics_url": "",
                        "backpressure_waiting": 1, "backpressure_kv": 0.55},
              "paths": {"logs_dir": "logs"}}
    ledger = Ledger(str(tmp_path / "l.db"))
    return WorkerRunner(config, ledger, str(project)), ledger


def _handle(trace, task, before):
    return {"proc": _DeadProc(), "out": _ClosedOut(), "trace": str(trace), "task": task,
            "thinking": "high", "before": before, "pgid": None, "started": time.monotonic()}


def _worker_exited(monkeypatch, trace, before):
    """Fake only the spawn: a worker that has exited after writing its trace."""
    monkeypatch.setattr("swarmflow.workers.kill_tree", lambda pid, pgid=None: False)
    monkeypatch.setattr(WorkerRunner, "_spawn",
                        lambda self, task, thinking, attempt: _handle(trace, task, before))
    monkeypatch.setattr(WorkerRunner, "_send_prompt", lambda self, handle, prompt: None)


def test_delivered_but_unchanged_is_no_changes(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "feature.py").write_text("already here\n", encoding="utf-8")
    trace = tmp_path / "t.jsonl"
    trace.write_text('{"type": "agent_end"}\n', encoding="utf-8")
    runner, ledger = _runner(tmp_path, project)
    ledger.add_task("T1", str(project), wave=1, owner_files=["feature.py"])
    _worker_exited(monkeypatch, trace,
                   {"feature.py": _hash_file(project / "feature.py")})

    result = runner.run_task("T1")

    assert result["outcome"] == "no_changes"
    assert result["changed"] == []
    assert ledger.get("T1")["status"] == "failed"
    assert json.loads(ledger.get("T1")["verdict"])["outcome"] == "no_changes"
    ledger.close()


def test_forbidden_command_is_a_scope_violation(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    trace = tmp_path / "t.jsonl"
    trace.write_text(json.dumps({
        "type": "message_end",
        "message": {"role": "assistant",
                    "content": [{"type": "toolCall", "name": "bash",
                                 "arguments": {"command": "npm install left-pad"}}]},
    }) + '\n{"type": "agent_end"}\n', encoding="utf-8")
    runner, ledger = _runner(tmp_path, project)
    ledger.add_task("T1", str(project), wave=1, owner_files=["feature.py"])
    _worker_exited(monkeypatch, trace, {"feature.py": None})

    result = runner.run_task("T1")

    assert result["outcome"] == "scope_violation"
    assert ledger.get("T1")["status"] == "failed"
    verdict = json.loads(ledger.get("T1")["verdict"])
    assert verdict["outcome"] == "scope_violation"
    assert verdict["forbidden"] == ["npm install left-pad"]
    ledger.close()


def test_spiral_failure_is_retried_once_at_lower_thinking(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    trace = tmp_path / "t.jsonl"
    trace.write_text('{"type": "agent_end"}\n', encoding="utf-8")
    runner, ledger = _runner(tmp_path, project)
    ledger.add_task("T1", str(project), wave=1, owner_files=["feature.py"])
    spawns, prompts, passes = [], [], []

    def spawn(self, task, thinking, attempt):
        spawns.append(thinking)
        return _handle(trace, task, {"feature.py": None})

    def supervise(self, handles, timeout_s):
        passes.append(len(handles))
        results = []
        for handle in handles:
            task_id = handle["task"]["id"]
            if len(passes) == 1:                     # first pass: the spiral failure
                self.ledger.set_status(task_id, "failed",
                                       verdict=json.dumps({"outcome": "spiral"}))
                results.append({"task_id": task_id, "outcome": "spiral", "missing": [],
                                "scan": {"turns": 1, "out_tokens": 40000},
                                "server_launches": []})
            else:                                    # the retry: a clean delivery
                (project / "feature.py").write_text("delivered\n", encoding="utf-8")
                self.ledger.set_status(task_id, "delivered")
                results.append({"task_id": task_id, "outcome": "delivered", "missing": [],
                                "scan": {"turns": 2, "out_tokens": 10},
                                "server_launches": []})
        return results

    monkeypatch.setattr("swarmflow.workers.kill_tree", lambda pid, pgid=None: False)
    monkeypatch.setattr(WorkerRunner, "_spawn", spawn)
    monkeypatch.setattr(WorkerRunner, "_send_prompt",
                        lambda self, handle, prompt: prompts.append(prompt))
    monkeypatch.setattr(WorkerRunner, "_supervise", supervise)

    results = runner.run_wave(1)

    kinds = [event["kind"] for event in ledger.events("T1", limit=20)]
    details = [event["detail"] for event in ledger.events("T1", limit=20)
               if event["kind"] == "retry"]
    assert len(results) == 2
    assert results[0]["outcome"] == "spiral" and results[1]["outcome"] == "delivered"
    assert "retry" in kinds and details == ["spiral -> lower thinking"]
    assert spawns == ["high", "medium"]              # retry_thinking, not the task's
    assert "RETRY CONTEXT" in prompts[-1]
    ledger.close()


def test_spiral_retry_stops_at_the_attempt_cap(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    trace = tmp_path / "t.jsonl"
    trace.write_text('{"type": "agent_end"}\n', encoding="utf-8")
    runner, ledger = _runner(tmp_path, project)
    ledger.add_task("T1", str(project), wave=1, owner_files=["feature.py"])
    ledger.bump_attempts("T1")                       # a retry already happened
    spawns = []

    def supervise(self, handles, timeout_s):
        results = []
        for handle in handles:
            task_id = handle["task"]["id"]
            self.ledger.set_status(task_id, "failed",
                                   verdict=json.dumps({"outcome": "spiral"}))
            results.append({"task_id": task_id, "outcome": "spiral", "missing": [],
                            "scan": {"turns": 1, "out_tokens": 40000},
                            "server_launches": []})
        return results

    monkeypatch.setattr("swarmflow.workers.kill_tree", lambda pid, pgid=None: False)
    monkeypatch.setattr(WorkerRunner, "_spawn",
                        lambda self, task, thinking, attempt:
                        spawns.append(thinking) or _handle(trace, task, {"feature.py": None}))
    monkeypatch.setattr(WorkerRunner, "_send_prompt", lambda self, handle, prompt: None)
    monkeypatch.setattr(WorkerRunner, "_supervise", supervise)

    results = runner.run_wave(1)

    kinds = [event["kind"] for event in ledger.events("T1", limit=20)]
    assert len(results) == 1 and results[0]["outcome"] == "spiral"
    assert spawns == ["high"]
    assert "retry" not in kinds
    ledger.close()


def _hold_calls(monkeypatch, reads, runner):
    """Patch the metrics reader and the backpressure sleep; return the sleep lengths."""
    sleeps = []
    queue = list(reads)
    monkeypatch.setattr("swarmflow.workers.read_server_load",
                        lambda url, timeout=5: queue.pop(0) if queue else {})
    monkeypatch.setattr("swarmflow.workers.time.sleep", lambda seconds: sleeps.append(seconds))
    runner._wait_ready()
    return sleeps


def test_admission_control_holds_until_the_server_drains(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    runner, ledger = _runner(tmp_path, project)

    # the waiting queue is over the threshold once, then drains
    assert _hold_calls(monkeypatch, [{"waiting": 5, "kv": 0.1},
                                     {"waiting": 0, "kv": 0.1}], runner) == [15]
    # the kv-cache alone can hold the wave (threshold 0.55 in this config)
    assert _hold_calls(monkeypatch, [{"waiting": 0, "kv": 0.9},
                                     {"waiting": 0, "kv": 0.1}], runner) == [15]
    # no metrics at all: dispatch immediately, never sleep
    assert _hold_calls(monkeypatch, [{}], runner) == []
    ledger.close()
