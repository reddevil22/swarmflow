"""Wave-run integration: regression gating, abort path, mixed-project refusal."""

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from swarmflow import cli, pipeline, procs, runstate
from swarmflow.config import load_config
from swarmflow.ledger import Ledger
from swarmflow.workers import WorkerRunner


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


GIT = _git_available()


class FakeRunner:
    """Replaces WorkerRunner: marks queued tasks delivered without spawning agents."""

    def __init__(self, ledger):
        self.ledger = ledger

    def run_wave(self, wave, concurrency=None):
        results = []
        for task in self.ledger.list_tasks(status="queued", wave=wave):
            self.ledger.set_status(task["id"], "delivered")
            results.append({"task_id": task["id"], "outcome": "delivered",
                            "missing": [], "scan": {"turns": 1, "out_tokens": 10}})
        return results


def _config(tmp_path, regression_command=None):
    config = {"paths": {"ledger": str(tmp_path / "ledger.db"),
                        "state_dir": str(tmp_path / "state")}}
    if regression_command:
        config["regression"] = {"command": regression_command}
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _greenfield_plan(tmp_path, project, task_ids):
    plan = {"project_name": "app", "project": str(project),
            "tasks": [{"id": task_id, "module": "app.py",
                       "owner_files": [f"{task_id}.py"], "spec": "do", "wave": 1}
                      for task_id in task_ids]}
    path = tmp_path / f"plan_{len(task_ids)}.yaml"
    path.write_text(yaml.safe_dump(plan), encoding="utf-8")
    return path


def _check_script(tmp_path, body):
    script = tmp_path / "check.py"
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def test_wave_run_regression_gate_and_skip(tmp_path, monkeypatch):
    project = tmp_path / "app"
    check = _check_script(tmp_path, "print('3 passed')\n")
    config = _config(tmp_path, regression_command=check)
    plan = _greenfield_plan(tmp_path, project, ["T1"])

    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))

    # wave 1: baseline recorded (green), regression re-run (green) -> rc 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0
    run_state = json.loads((project / ".swarmflow" / "run.json").read_text(encoding="utf-8"))
    assert run_state["regression"]["baseline"]["rc"] == 0
    assert "output" not in run_state["regression"]["baseline"]
    compare_path = project / ".swarmflow" / "evidence" / "wave1.compare.json"
    assert json.loads(compare_path.read_text(encoding="utf-8"))["version"] == 1

    # wave-run for T2 with a failing suite -> regression detected, rc 1
    plan2 = _greenfield_plan(tmp_path, project, ["T2"])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan2)]) == 0
    _check_script(tmp_path, "print('1 failed, 2 passed')\nraise SystemExit(1)\n")
    rc = cli.main(["--config", str(config), "wave-run", "--wave", "1"])
    assert rc == 1
    ledger = Ledger(str(tmp_path / "ledger.db"))
    kinds = [event["kind"] for event in ledger.events("T2", limit=20)]
    ledger.close()
    assert "regression" in kinds

    # --skip-regression bypasses the gate for T3
    plan3 = _greenfield_plan(tmp_path, project, ["T3"])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan3)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1",
                     "--skip-regression"]) == 0


def test_wave_run_refuses_mixed_projects(tmp_path):
    config = _config(tmp_path)
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.add_task("A", str(tmp_path / "proj_a"), wave=9)
    ledger.add_task("B", str(tmp_path / "proj_b"), wave=9)
    ledger.close()
    assert cli.main(["--config", str(config), "wave-run", "--wave", "9"]) == 2


def _red_script(tmp_path, failing_node):
    script = tmp_path / "check.py"
    script.write_text(f"print('FAILED {failing_node} - AssertionError: boom')\n"
                      "print('1 failed, 2 passed in 0.10s')\n"
                      "raise SystemExit(1)\n", encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def test_wave_run_detects_changed_failing_test(tmp_path, monkeypatch):
    project = tmp_path / "app"
    config = _config(tmp_path, regression_command=_red_script(
        tmp_path, "tests/test_a.py::test_one"))
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))

    plan = _greenfield_plan(tmp_path, project, ["T1"])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0
    # baseline is red (test_one); the same failing set on the wave -> clean pass
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0
    run_state = json.loads((project / ".swarmflow" / "run.json").read_text(encoding="utf-8"))
    assert run_state["regression"]["baseline"]["fingerprints"] == \
        ["tests/test_a.py::test_one"]

    # wave 2: a DIFFERENT test fails at the same count -> regression
    _red_script(tmp_path, "tests/test_b.py::test_two")
    plan2 = _greenfield_plan(tmp_path, project, ["T2"])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan2)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 1
    ledger = Ledger(str(tmp_path / "ledger.db"))
    events = ledger.events("T2", limit=20)
    ledger.close()
    assert "regression" in [event["kind"] for event in events]
    assert any("tests/test_b.py::test_two" in event["detail"] for event in events
               if event["kind"] == "regression")
    comparison = json.loads((project / ".swarmflow" / "evidence"
                             / "wave1.compare.json").read_text(encoding="utf-8"))
    assert comparison["new_failures"] == ["tests/test_b.py::test_two"]


def test_wave_run_detects_suite_shrink_while_green(tmp_path, monkeypatch):
    project = tmp_path / "app"
    check = tmp_path / "check.py"
    check.write_text("print('5 passed in 0.10s')\n", encoding="utf-8")
    config = _config(tmp_path, regression_command=f'"{sys.executable}" "{check}"')
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))

    plan = _greenfield_plan(tmp_path, project, ["T1"])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0

    # two tests vanish but the suite still exits green -> the gate must fail
    check.write_text("print('3 passed in 0.10s')\n", encoding="utf-8")
    plan2 = _greenfield_plan(tmp_path, project, ["T2"])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan2)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 1
    comparison = json.loads((project / ".swarmflow" / "evidence"
                             / "wave1.compare.json").read_text(encoding="utf-8"))
    assert comparison["regressed"] is True
    assert comparison["suite_delta"] == -2


def test_wave_run_rebaseline_re_records_and_skips_comparison(tmp_path, monkeypatch):
    project = tmp_path / "app"
    config = _config(tmp_path, regression_command=_red_script(
        tmp_path, "tests/test_a.py::test_one"))
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))

    plan = _greenfield_plan(tmp_path, project, ["T1"])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0

    _red_script(tmp_path, "tests/test_b.py::test_two")
    plan2 = _greenfield_plan(tmp_path, project, ["T2"])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan2)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1",
                     "--rebaseline"]) == 0
    run_state = json.loads((project / ".swarmflow" / "run.json").read_text(encoding="utf-8"))
    assert run_state["regression"]["baseline"]["fingerprints"] == \
        ["tests/test_b.py::test_two"]
    ledger = Ledger(str(tmp_path / "ledger.db"))
    kinds = [event["kind"] for event in ledger.events("T2", limit=20)]
    ledger.close()
    assert "rebaseline" in kinds


@pytest.mark.skipif(not GIT, reason="git not available")
def test_brownfield_wave_run_reports_discrimination(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n",
                                                encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "baseline")

    command = f'"{sys.executable}" -m pytest -q tests'
    task = {"id": "T1", "module": "app.py",
            "owner_files": ["app.py", "tests/test_app.py"],
            "spec": "do", "wave": 1, "test_command": command}
    plan = {"project_name": "demo", "project": str(repo), "mode": "brownfield",
            "tasks": [task]}
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")
    config = _config(tmp_path)

    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan_path)]) == 0
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))

    # warn mode (default): the vacuous test is recorded as evidence, the wave passes
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0
    data = json.loads((repo / ".swarmflow" / "evidence"
                       / "wave1.discrimination.json").read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["counts"]["passes_at_parent"] == 1
    ledger = Ledger(str(tmp_path / "ledger.db"))
    kinds = [event["kind"] for event in ledger.events("T1", limit=20)]
    ledger.close()
    assert "discrimination" in kinds

    # enforce mode: the same evidence fails the wave
    enforce_config = tmp_path / "cfg_enforce.yaml"
    enforce_config.write_text(yaml.safe_dump({
        "paths": {"ledger": str(tmp_path / "ledger.db"),
                  "state_dir": str(tmp_path / "state")},
        "discrimination": {"mode": "enforce"}}), encoding="utf-8")
    task2 = dict(task, id="T2")
    plan2 = dict(plan, tasks=[task2])
    plan_path2 = tmp_path / "plan2.yaml"
    plan_path2.write_text(yaml.safe_dump(plan2), encoding="utf-8")
    assert cli.main(["--config", str(enforce_config), "plan-load",
                     "--plan", str(plan_path2)]) == 0
    assert cli.main(["--config", str(enforce_config), "wave-run", "--wave", "1"]) == 1


@pytest.mark.skipif(not GIT, reason="git not available")
def test_plan_load_warns_about_stale_listener_and_can_kill_it(tmp_path, capsys):
    import socket

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo,
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo,
                   capture_output=True, check=True)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo,
                   capture_output=True, check=True)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    starter = ("import subprocess, sys\n"
               f"subprocess.Popen([sys.executable, '-m', 'http.server', '{port}', "
               "'--bind', '127.0.0.1'], "
               f"cwd=r'{repo}', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n")
    subprocess.run([sys.executable, "-c", starter], check=True)

    def listener_pid():
        for pid, entry in procs.snapshot().items():
            if "http.server" in entry["cmdline"] and str(port) in entry["cmdline"] \
                    and entry["name"].lower().startswith("python"):
                return pid
        return None

    assert _wait_until(lambda: listener_pid() is not None), \
        "stale listener was not started"
    server_pid = listener_pid()
    config = _config(tmp_path)
    try:
        plan = {"project_name": "demo", "project": str(repo), "mode": "brownfield",
                "tasks": [{"id": "T1", "module": "app.py", "owner_files": ["app.py"],
                           "spec": "do", "wave": 1}]}
        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")
        assert cli.main(["--config", str(config), "plan-load",
                         "--plan", str(plan_path)]) == 0
        output = capsys.readouterr().out
        assert "project-attributed listener already running" in output
        assert procs.is_alive(server_pid)

        plan2 = dict(plan, tasks=[dict(plan["tasks"][0], id="T2")])
        plan_path2 = tmp_path / "plan2.yaml"
        plan_path2.write_text(yaml.safe_dump(plan2), encoding="utf-8")
        assert cli.main(["--config", str(config), "plan-load", "--kill-stale",
                         "--plan", str(plan_path2)]) == 0
        assert "--kill-stale terminated" in capsys.readouterr().out
        assert _wait_dead(server_pid), "--kill-stale did not terminate the listener"
    finally:
        if server_pid and procs.is_alive(server_pid):
            procs.kill_tree(server_pid, server_pid)


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    """Bounded wait: returns as soon as the predicate holds (no fixed sleeps)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _wait_dead(pid, timeout: float = 8.0) -> bool:
    """Bounded wait for a process to disappear (kills land asynchronously)."""
    return _wait_until(lambda: not procs.is_alive(pid), timeout)


@pytest.mark.skipif(not GIT, reason="git not available")
def test_brownfield_wave_run_sweeps_leaked_processes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n",
                                                encoding="utf-8")
    script = repo / "serve.py"
    script.write_text("import time\n\ntime.sleep(60)\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "baseline")

    command = f'"{sys.executable}" -m pytest -q tests'
    task = {"id": "T1", "module": "app.py",
            "owner_files": ["app.py", "tests/test_app.py"],
            "spec": "do", "wave": 1, "test_command": command}
    plan = {"project_name": "demo", "project": str(repo), "mode": "brownfield",
            "tasks": [task]}
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")
    config = _config(tmp_path)

    leaked = []

    def leak():
        starter = ("import subprocess, sys\n"
                   f"subprocess.Popen([sys.executable, r'{script}'], "
                   "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n")
        subprocess.run([sys.executable, "-c", starter], check=True, cwd=repo)

        def new_leak():
            return [pid for pid, entry in procs.snapshot().items()
                    if str(script) in entry["cmdline"] and pid not in leaked]

        _wait_until(lambda: bool(new_leak()))
        hit = new_leak()
        if hit:
            leaked.append(hit[0])

    class LeakyRunner:
        def __init__(self, ledger):
            self.ledger = ledger

        def run_wave(self, wave, concurrency=None):
            results = []
            for task_row in self.ledger.list_tasks(status="queued", wave=wave):
                self.ledger.set_status(task_row["id"], "delivered")
                results.append({"task_id": task_row["id"], "outcome": "delivered",
                                "missing": [], "scan": {"turns": 1, "out_tokens": 10},
                                "server_launches": []})
            leak()
            return results

    monkeypatch.setattr(pipeline, "_runner",
                        lambda config, ledger, root: LeakyRunner(ledger))
    try:
        assert cli.main(["--config", str(config), "plan-load",
                         "--plan", str(plan_path)]) == 0
        # wave 1 (warn): the leak is reported and left alive
        assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0
        data = json.loads((repo / ".swarmflow" / "evidence"
                           / "wave1.sweep.json").read_text(encoding="utf-8"))
        assert data["counts"]["orphans"] >= 1
        assert data["killed"] == []
        assert procs.is_alive(leaked[0])
        ledger = Ledger(str(tmp_path / "ledger.db"))
        kinds = [event["kind"] for event in ledger.events("T1", limit=20)]
        ledger.close()
        assert "sweep" in kinds

        # wave 2 (kill, port requirement off for the python sleeper): the new leak dies,
        # the first one is pre-existing and stays untouched
        kill_config = tmp_path / "cfg_kill.yaml"
        kill_config.write_text(yaml.safe_dump({
            "paths": {"ledger": str(tmp_path / "ledger.db"),
                      "state_dir": str(tmp_path / "state")},
            "sweep": {"mode": "kill", "kill_requires_port": False}}), encoding="utf-8")
        plan2 = dict(plan, tasks=[dict(task, id="T2")])
        plan_path2 = tmp_path / "plan2.yaml"
        plan_path2.write_text(yaml.safe_dump(plan2), encoding="utf-8")
        assert cli.main(["--config", str(kill_config), "plan-load",
                         "--plan", str(plan_path2)]) == 0
        assert cli.main(["--config", str(kill_config), "wave-run", "--wave", "1"]) == 0
        data = json.loads((repo / ".swarmflow" / "evidence"
                           / "wave1.sweep.json").read_text(encoding="utf-8"))
        assert data["counts"]["killed"] >= 1
        assert _wait_dead(leaked[1]), "the leak survived the kill-mode sweep"
        assert procs.is_alive(leaked[0])
    finally:
        for pid in leaked:
            if procs.is_alive(pid):
                procs.kill_tree(pid, pid)


@pytest.mark.skipif(not GIT, reason="git not available")
def test_recon_rewrite_cannot_change_the_gate_command(tmp_path, monkeypatch, capsys):
    """The gate command is frozen at plan time; a worker-writable recon.json rewrite
    must not be able to choose what the control plane executes."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "baseline")

    command = f'"{sys.executable}" -m pytest -q --version'
    config = _config(tmp_path, regression_command=command)
    plan = {"project_name": "demo", "project": str(repo), "mode": "brownfield",
            "tasks": [{"id": "T1", "module": "app.py", "owner_files": ["app.py"],
                       "spec": "do", "wave": 1}]}
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))

    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan_path)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0

    # a hostile wave rewrites the project-side recon.json before the next wave
    (repo / ".swarmflow" / "recon.json").write_text(json.dumps({
        "commands": {"regression": {"command": "python -c \"print('pwned')\"",
                                    "evidence": "hostile"}}}), encoding="utf-8")
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.add_task("T2", str(repo), wave=2)
    ledger.close()
    capsys.readouterr()

    assert cli.main(["--config", str(config), "wave-run", "--wave", "2"]) == 0
    evidence = (repo / ".swarmflow" / "evidence" / "wave2.txt").read_text(encoding="utf-8")
    assert command in evidence
    assert "pwned" not in evidence


@pytest.mark.skipif(not GIT, reason="git not available")
def test_config_override_wins_over_the_frozen_command(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "baseline")

    first = f'"{sys.executable}" -m pytest -q --version'
    config = _config(tmp_path, regression_command=first)
    plan = {"project_name": "demo", "project": str(repo), "mode": "brownfield",
            "tasks": [{"id": "T1", "module": "app.py", "owner_files": ["app.py"],
                       "spec": "do", "wave": 1}]}
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan_path)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0

    # the operator changes the command; wave-run must use it and say so
    second = f'"{sys.executable}" -m pytest -q --help'
    updated = tmp_path / "cfg2.yaml"
    updated.write_text(yaml.safe_dump({
        "paths": {"ledger": str(tmp_path / "ledger.db"),
                  "state_dir": str(tmp_path / "state")},
        "regression": {"command": second}}), encoding="utf-8")
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.add_task("T2", str(repo), wave=2)
    ledger.close()
    capsys.readouterr()

    assert cli.main(["--config", str(updated), "wave-run", "--wave", "2"]) == 0
    output = capsys.readouterr().out
    assert "overridden by config" in output
    evidence = (repo / ".swarmflow" / "evidence" / "wave2.txt").read_text(encoding="utf-8")
    assert second in evidence


def test_missing_store_baseline_fails_a_brownfield_audit(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    runstate.save_run(str(project), {"mode": "brownfield"})
    ledger = Ledger(str(tmp_path / "ledger.db"))
    state, result = pipeline.run_audit(str(project), ledger, "T1", brownfield=True)
    skipped, _ = pipeline.run_audit(str(project), ledger, "T1", brownfield=False)
    ledger.close()
    assert state == "fail"
    assert result["violations"][0]["kind"] == "no_baseline"
    assert skipped == "skipped"


def test_corrupt_store_baseline_is_a_structured_failure(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    runstate.frozen_path(str(project)).parent.mkdir(parents=True, exist_ok=True)
    runstate.frozen_path(str(project)).write_text("{broken", encoding="utf-8")
    config = _config(tmp_path)
    rc = cli.main(["--config", str(config), "audit", "--project", str(project)])
    output = capsys.readouterr().out
    assert rc == 1
    assert "corrupt_baseline" in output


@pytest.mark.skipif(not GIT, reason="git not available")
def test_project_side_state_cannot_forge_the_audit(tmp_path, monkeypatch):
    """A worker rewriting .swarmflow/run.json (ignore-all) and touching a frozen file
    must still fail the wave - the store is authoritative."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / ".gitignore").write_text(".swarmflow/\nlogs/\n", encoding="utf-8")
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "baseline")
    config = _config(tmp_path)
    plan = {"project_name": "demo", "project": str(repo), "mode": "brownfield",
            "tasks": [{"id": "T1", "module": "tracked.py",
                       "owner_files": ["tracked.py"], "spec": "do", "wave": 1}]}
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan_path)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0

    class ForgingRunner:
        """Modifies a frozen file during the wave and forges the project-side state."""

        def __init__(self, ledger):
            self.ledger = ledger

        def run_wave(self, wave, concurrency=None):
            (repo / ".swarmflow" / "run.json").write_text(json.dumps({
                "mode": "brownfield", "audit_ignores": ["*"]}), encoding="utf-8")
            (repo / "tracked.py").write_text("x = 2\n", encoding="utf-8")
            results = []
            for task in self.ledger.list_tasks(status="queued", wave=wave):
                self.ledger.set_status(task["id"], "delivered")
                results.append({"task_id": task["id"], "outcome": "delivered",
                                "missing": [], "scan": {"turns": 1, "out_tokens": 10},
                                "server_launches": []})
            return results

    monkeypatch.setattr(pipeline, "_runner",
                        lambda config, ledger, root: ForgingRunner(ledger))
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.add_task("T2", str(repo), wave=2)
    ledger.close()

    assert cli.main(["--config", str(config), "wave-run", "--wave", "2"]) == 1


def _brownfield_repo(tmp_path, files, name="repo"):
    repo = tmp_path / name
    repo.mkdir()

    def run(*args):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / ".gitignore").write_text(".swarmflow/\nlogs/\n", encoding="utf-8")
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "baseline")
    return repo


def _git_out(root, *args) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          check=True).stdout.strip()


def _delivering_runner(contents):
    """Runner that writes each queued task's owner files and reports what changed."""

    class DeliveringRunner:
        def __init__(self, ledger, root):
            self.ledger = ledger
            self.root = root

        def run_wave(self, wave, concurrency=None):
            results = []
            for task in self.ledger.list_tasks(status="queued", wave=wave):
                changed = []
                for rel in task["owner_files"]:
                    path = self.root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(contents.get(task["id"], "written\n"), encoding="utf-8")
                    changed.append(rel)
                self.ledger.set_status(task["id"], "delivered")
                results.append({"task_id": task["id"], "outcome": "delivered",
                                "missing": [], "scan": {"turns": 1, "out_tokens": 10},
                                "changed": changed, "server_launches": []})
            return results

    return DeliveringRunner


def _brownfield_plan_with(tmp_path, project, tasks, name="plan.yaml",
                          project_name="demo"):
    plan = {"project_name": project_name, "project": str(project), "mode": "brownfield",
            "tasks": tasks}
    path = tmp_path / name
    path.write_text(yaml.safe_dump(plan), encoding="utf-8")
    return path


def _task(task_id, files, spec="do the thing"):
    return {"id": task_id, "module": files[0], "owner_files": list(files), "spec": spec,
            "wave": 1}


@pytest.mark.skipif(not GIT, reason="git not available")
def test_worktree_plan_load_leaves_the_project_checkout_alone(tmp_path, monkeypatch):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    (repo / "app.py").write_text("x = 1\ndirty = True\n", encoding="utf-8")
    (repo / "wip.txt").write_text("staged work in progress\n", encoding="utf-8")
    _git_out(repo, "add", "wip.txt")
    config = _config(tmp_path)
    plan = _brownfield_plan_with(tmp_path, repo, [_task("T1", ["feature.py"])])
    branch_before = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    head_before = _git_out(repo, "rev-parse", "HEAD")
    before_status = _git_out(repo, "status", "--porcelain")

    rc = cli.main(["--config", str(config), "plan-load", "--plan", str(plan),
                   "--worktree"])

    assert rc == 0
    worktree = repo / ".swarmflow" / "worktrees" / "demo"
    assert worktree.is_dir()
    assert _git_out(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "swarmflow/demo"
    # the project kept its branch, its dirty file, and its staged work
    assert _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
    assert _git_out(repo, "rev-parse", "HEAD") == head_before
    assert (repo / "app.py").read_text(encoding="utf-8") == "x = 1\ndirty = True\n"
    assert "wip.txt" in _git_out(repo, "diff", "--cached", "--name-only").split()
    # ...and the run's root is the worktree, recorded on both stores
    ledger = Ledger(str(tmp_path / "ledger.db"))
    tasks = ledger.list_tasks()
    ledger.close()
    assert Path(tasks[0]["project"]).resolve() == worktree.resolve()
    assert runstate.load_run(str(repo))["worktree"] == str(worktree.resolve())
    assert runstate.load_run(str(worktree))["source_project"] == str(repo.resolve())
    assert "worktree" not in runstate.load_run(str(worktree))
    assert runstate.resolve_project(str(repo)) == worktree.resolve()
    # the project checkout is untouched: same branch, same dirty file, same staged work
    assert _git_out(repo, "status", "--porcelain") == before_status

    # the wave runs there, and its commit lands on the run branch, not in the project
    runner = _delivering_runner({"T1": "feature\n"})
    monkeypatch.setattr(pipeline, "_runner",
                        lambda config, ledger, root: runner(ledger, Path(root)))
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0
    assert (worktree / "feature.py").read_text(encoding="utf-8") == "feature\n"
    assert "swarmflow T1" in _git_out(worktree, "log", "--format=%s")
    assert (repo / "feature.py").exists() is False
    assert _git_out(repo, "log", "-1", "--format=%s") == "baseline"
    assert _git_out(repo, "status", "--porcelain") == before_status

    # a command given the project path reads the run's tree
    assert cli.main(["--config", str(config), "evidence", "--project", str(repo)]) == 0
    assert (worktree / ".swarmflow" / "evidence" / "bundle.md").exists()
    assert not (repo / ".swarmflow" / "evidence").exists()

    # a run rooted at the project itself supersedes the pointer to the worktree
    second = _brownfield_plan_with(tmp_path, repo, [_task("T2", ["other.py"])],
                                   name="plan2.yaml", project_name="second")
    assert cli.main(["--config", str(config), "plan-load", "--allow-dirty",
                     "--plan", str(second)]) == 0
    assert "worktree" not in runstate.load_run(str(repo))
    assert runstate.resolve_project(str(repo)) == repo.resolve()


@pytest.mark.skipif(not GIT, reason="git not available")
def test_worktree_run_gets_the_pinned_prd(tmp_path):
    """`.swarmflow/` is gitignored, so the run root must be handed the PRD explicitly."""
    from swarmflow.roles import _prd_section

    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    prd = tmp_path / "PRD.md"
    prd.write_text("# Requirements\n\nShip the thing.\n", encoding="utf-8")
    stored = repo / ".swarmflow" / "PRD.md"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text(prd.read_text(encoding="utf-8"), encoding="utf-8")
    runstate.save_run(str(repo), {"prd_path": str(stored),
                                  "prd_sha256": hashlib.sha256(
                                      prd.read_text(encoding="utf-8")
                                      .encode("utf-8")).hexdigest()})
    worktree = repo / ".swarmflow" / "worktrees" / "demo"
    worktree.mkdir(parents=True)

    assert pipeline.copy_pinned_prd(repo, worktree) is True

    section = _prd_section(str(worktree))
    assert section is not None and section[0] == "PRD (frozen input)"
    assert "Ship the thing." in section[1]
    assert runstate.load_run(str(worktree))["prd_sha256"]


@pytest.mark.skipif(not GIT, reason="git not available")
def test_worktree_preflight_reports_project_checkout_listeners(tmp_path, capsys):
    """A server the operator runs from the project checkout is reported, never killed."""
    import socket

    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    worktree = repo / ".swarmflow" / "worktrees" / "demo"
    worktree.mkdir(parents=True)
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()
    starter = ("import subprocess, sys\n"
               f"popen = subprocess.Popen([sys.executable, '-m', 'http.server', "
               f"'{port}', '--bind', '127.0.0.1'], stdout=subprocess.DEVNULL, "
               f"stderr=subprocess.DEVNULL)\n")
    subprocess.run([sys.executable, "-c", starter], cwd=repo, check=True)

    def listener_pid():
        for candidate, entry in procs.snapshot().items():
            if "http.server" in entry["cmdline"] and str(port) in entry["cmdline"]:
                return candidate
        return None

    assert _wait_until(lambda: listener_pid() is not None), \
        "the checkout listener was not started"
    pid = listener_pid()
    try:
        plan = {"project_name": "demo", "project": str(repo), "mode": "brownfield",
                "tasks": [_task("T1", ["feature.py"])]}
        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")
        from swarmflow.config import load_config
        config = load_config(str(_config(tmp_path)))
        assert pipeline.brownfield_preflight(plan, worktree, str(plan_path), False, False,
                                             config, source_root=repo) == 0
        out = capsys.readouterr().out
        assert "run from the project checkout" in out
        assert str(port) in out
        assert procs.is_alive(pid)
    finally:
        if pid and procs.is_alive(pid):
            procs.kill_tree(pid, pid)


@pytest.mark.skipif(not GIT, reason="git not available")
def test_wave_run_commits_each_delivered_task(tmp_path, monkeypatch):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    config = _config(tmp_path)
    plan = _brownfield_plan_with(tmp_path, repo, [_task("T1", ["a.py"], "add a"),
                                                  _task("T2", ["b.py"], "add b")])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0
    # the operator keeps working while the wave runs: staged work must stay untouchable
    (repo / "wip.txt").write_text("operator work\n", encoding="utf-8")
    _git_out(repo, "add", "wip.txt")
    runner = _delivering_runner({"T1": "a = 1\n", "T2": "b = 2\n"})
    monkeypatch.setattr(pipeline, "_runner",
                        lambda config, ledger, root: runner(ledger, Path(root)))

    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0

    subjects = _git_out(repo, "log", "--format=%s").splitlines()
    assert subjects[0].startswith("swarmflow T2") and subjects[1].startswith("swarmflow T1")
    assert subjects[2] == "baseline"
    assert _git_out(repo, "show", "--name-only", "--format=", "HEAD") == "b.py"
    assert _git_out(repo, "show", "--name-only", "--format=", "HEAD~1") == "a.py"
    # both commits are in the tree, clean
    assert (repo / "a.py").exists() and (repo / "b.py").exists()
    assert "a.py" not in _git_out(repo, "status", "--porcelain")
    # the operator's staged file is still staged and was never committed
    assert "wip.txt" in _git_out(repo, "diff", "--cached", "--name-only").split()
    assert "wip.txt" not in _git_out(repo, "log", "--format=", "--name-only").split()
    ledger = Ledger(str(tmp_path / "ledger.db"))
    commits = [event for event in ledger.events("T1", limit=20) if event["kind"] == "commit"]
    ledger.close()
    assert len(commits) == 1


@pytest.mark.skipif(not GIT, reason="git not available")
def test_worktree_plan_load_starts_on_a_repo_that_ignores_nothing(tmp_path):
    """Adding ignore entries must not make the fresh worktree look dirty to itself."""
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    (repo / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    _git_out(repo, "add", ".gitignore")
    _git_out(repo, "commit", "-qm", "own gitignore")
    config = _config(tmp_path)
    plan = _brownfield_plan_with(tmp_path, repo, [_task("T1", ["feature.py"])])

    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan),
                     "--worktree"]) == 0

    worktree = repo / ".swarmflow" / "worktrees" / "demo"
    assert (worktree / ".swarmflow").exists()
    ignored = (worktree / ".gitignore").read_text(encoding="utf-8")
    assert ".swarmflow/" in ignored and "logs/" in ignored
    # the appended entries are the run root's only modification, and its own artifacts
    # (.swarmflow/, logs/) never show up as untracked
    assert _git_out(worktree, "status", "--porcelain").splitlines() == ["M .gitignore"]
    # only the operator's .gitignore changed in the project checkout
    changed = set(_git_out(repo, "status", "--porcelain").splitlines())
    assert changed == {"M .gitignore"}


@pytest.mark.skipif(not GIT, reason="git not available")
def test_plan_load_refuses_a_branch_another_worktree_holds(tmp_path):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    holder = tmp_path / "holder"
    _git_out(repo, "worktree", "add", "--quiet", "-b", "swarmflow/demo", str(holder))
    config = _config(tmp_path)
    plan = _brownfield_plan_with(tmp_path, repo, [_task("T1", ["a.py"])])

    rc = cli.main(["--config", str(config), "plan-load", "--plan", str(plan)])

    assert rc == 2
    assert _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") != "swarmflow/demo"


@pytest.mark.skipif(not GIT, reason="git not available")
def test_worktree_gate_command_falls_back_to_the_project_checkout_recon(tmp_path):
    """A `recon --regression-command` run against the project must still gate the run."""
    from swarmflow.recon import recon as run_recon

    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    run_recon(str(repo), regression_command="custom-gate --run")
    worktree = repo / ".swarmflow" / "worktrees" / "demo"
    worktree.mkdir(parents=True)

    pipeline.adopt_source_gate_command(worktree, repo)

    state = runstate.load_run(str(worktree))
    assert state["regression_command"] == "custom-gate --run"
    assert state["regression_source"] == "recon (project checkout)"


@pytest.mark.skipif(not GIT, reason="git not available")
def test_commit_covers_the_whole_task_not_one_attempt(tmp_path, monkeypatch):
    """A retried task's earlier attempt must be in the commit, not left dirty."""
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n", "a.py": "attempt one\n"})
    config = _config(tmp_path)
    plan = _brownfield_plan_with(tmp_path, repo, [_task("T1", ["a.py", "b.py"])])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0
    # a.py differs from HEAD as an earlier, non-delivering attempt would have left it
    (repo / "a.py").write_text("attempt one, edited\n", encoding="utf-8")
    runner = _delivering_runner({"T1": "b = 2\n"})
    monkeypatch.setattr(pipeline, "_runner",
                        lambda config, ledger, root: runner(ledger, Path(root)))

    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0

    committed = _git_out(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == ["a.py", "b.py"]
    assert _git_out(repo, "status", "--porcelain") == ""


@pytest.mark.skipif(not GIT, reason="git not available")
def test_commit_skips_gitignored_owner_paths(tmp_path, monkeypatch, capsys):
    """An ignored owner path cannot be committed; the rest of the wave still lands."""
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    config = _config(tmp_path)
    plan = _brownfield_plan_with(tmp_path, repo, [_task("T1", ["logs/run.log"])])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0

    class LoggingRunner:
        def __init__(self, ledger, root):
            self.ledger = ledger
            self.root = root

        def run_wave(self, wave, concurrency=None):
            results = []
            path = self.root / "logs" / "run.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("noise\n", encoding="utf-8")
            for task in self.ledger.list_tasks(status="queued", wave=wave):
                self.ledger.set_status(task["id"], "delivered")
                results.append({"task_id": task["id"], "outcome": "delivered",
                                "missing": [], "scan": {"turns": 1, "out_tokens": 10},
                                "changed": ["logs/run.log"], "server_launches": []})
            return results

    monkeypatch.setattr(pipeline, "_runner",
                        lambda config, ledger, root: LoggingRunner(ledger, Path(root)))
    rc = cli.main(["--config", str(config), "wave-run", "--wave", "1"])
    out = capsys.readouterr().out
    ledger = Ledger(str(tmp_path / "ledger.db"))
    kinds = [event["kind"] for event in ledger.events("T1", limit=20)]
    ledger.close()

    assert rc in (0, 1)                       # the audit decides; the commit does not
    assert "only gitignored owner paths changed" in out
    assert "commit-skipped" in kinds
    assert _git_out(repo, "log", "--format=%s").splitlines() == ["baseline"]
    assert _git_out(repo, "diff", "--cached", "--name-only") == ""


@pytest.mark.skipif(not GIT, reason="git not available")
def test_commit_message_names_the_task_module_and_attempt(tmp_path, monkeypatch):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    config = _config(tmp_path)
    plan = _brownfield_plan_with(tmp_path, repo, [_task("T1", ["a.py"])])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0
    runner = _delivering_runner({"T1": "a = 1\n"})
    monkeypatch.setattr(pipeline, "_runner",
                        lambda config, ledger, root: runner(ledger, Path(root)))

    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0

    body = _git_out(repo, "log", "-1", "--format=%B")
    assert body.splitlines()[0] == "swarmflow T1: a.py"
    assert "attempt 1" in body
    assert "scope audit: ok" in body


@pytest.mark.skipif(not GIT, reason="git not available")
def test_commit_failure_is_recorded_and_does_not_fail_the_wave(tmp_path, monkeypatch,
                                                               capsys):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    config = _config(tmp_path)
    plan = _brownfield_plan_with(tmp_path, repo, [_task("T1", ["a.py"])])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0
    runner = _delivering_runner({"T1": "a = 1\n"})
    monkeypatch.setattr(pipeline, "_runner",
                        lambda config, ledger, root: runner(ledger, Path(root)))

    rc = cli.main(["--config", str(config), "wave-run", "--wave", "1"])
    out = capsys.readouterr().out
    ledger = Ledger(str(tmp_path / "ledger.db"))
    kinds = [event["kind"] for event in ledger.events("T1", limit=20)]
    ledger.close()

    assert rc == 0, out
    assert "commit failed for T1" in out
    assert "commit-failed" in kinds
    assert _git_out(repo, "log", "--format=%s").splitlines() == ["baseline"]
    assert _git_out(repo, "diff", "--cached", "--name-only") == ""
    assert (repo / "a.py").exists()             # the work is still in the tree


@pytest.mark.skipif(not GIT, reason="git not available")
def test_wave_run_commits_can_be_turned_off_in_config(tmp_path, monkeypatch):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    config = _config(tmp_path)
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["git"] = {"commit_tasks": False}
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    plan = _brownfield_plan_with(tmp_path, repo, [_task("T1", ["a.py"])])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0
    runner = _delivering_runner({"T1": "a = 1\n"})
    monkeypatch.setattr(pipeline, "_runner",
                        lambda config, ledger, root: runner(ledger, Path(root)))

    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0

    assert _git_out(repo, "log", "--format=%s").splitlines() == ["baseline"]
    assert "a.py" in _git_out(repo, "status", "--porcelain")


@pytest.mark.skipif(not GIT, reason="git not available")
def test_failed_scope_audit_commits_nothing(tmp_path, monkeypatch, capsys):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    config = _config(tmp_path)
    plan = _brownfield_plan_with(tmp_path, repo, [_task("T1", ["a.py"])])
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0

    class StrayingRunner:
        def __init__(self, ledger, root):
            self.ledger = ledger
            self.root = root

        def run_wave(self, wave, concurrency=None):
            results = []
            (self.root / "stray.txt").write_text("unowned\n", encoding="utf-8")
            for task in self.ledger.list_tasks(status="queued", wave=wave):
                path = self.root / task["owner_files"][0]
                path.write_text("a = 1\n", encoding="utf-8")
                self.ledger.set_status(task["id"], "delivered")
                results.append({"task_id": task["id"], "outcome": "delivered",
                                "missing": [], "scan": {"turns": 1, "out_tokens": 10},
                                "changed": list(task["owner_files"]),
                                "server_launches": []})
            return results

    monkeypatch.setattr(pipeline, "_runner",
                        lambda config, ledger, root: StrayingRunner(ledger, Path(root)))
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 1
    out = capsys.readouterr().out
    assert "not cleanly attributable" in out
    assert _git_out(repo, "log", "--format=%s").splitlines() == ["baseline"]


def _brownfield_plan(tmp_path, repo, name="plan.yaml"):
    plan = {"project_name": "demo", "project": str(repo), "mode": "brownfield",
            "tasks": [{"id": "T1", "module": "owned.py", "owner_files": ["owned.py"],
                       "spec": "do", "wave": 1}]}
    path = tmp_path / name
    path.write_text(yaml.safe_dump(plan), encoding="utf-8")
    return path


@pytest.mark.skipif(not GIT, reason="git not available")
def test_between_wave_edit_of_a_frozen_file_stays_visible(tmp_path, monkeypatch, capsys):
    """The reported hole: wave N+1's per-wave freeze must not re-bless an edit that
    happened between waves."""
    repo = _brownfield_repo(tmp_path, {"frozen.py": "x = 1\n"})
    config = _config(tmp_path)
    plan_path = _brownfield_plan(tmp_path, repo)
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))

    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan_path)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0

    (repo / "frozen.py").write_text("x = 2\n", encoding="utf-8")   # between waves
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.add_task("T2", str(repo), wave=2, owner_files=["owned.py"])
    ledger.close()
    capsys.readouterr()

    assert cli.main(["--config", str(config), "wave-run", "--wave", "2"]) == 1
    ledger = Ledger(str(tmp_path / "ledger.db"))
    events = [event for event in ledger.events("T2", limit=30)
              if event["kind"] == "scope-violation"]
    ledger.close()
    assert any("modified_frozen" in event["detail"] and "frozen.py" in event["detail"]
               for event in events)


@pytest.mark.skipif(not GIT, reason="git not available")
def test_missing_frozen_baseline_refuses_the_wave(tmp_path, monkeypatch, capsys):
    repo = _brownfield_repo(tmp_path, {"frozen.py": "x = 1\n"})
    config = _config(tmp_path)
    plan_path = _brownfield_plan(tmp_path, repo)
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))

    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan_path)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0
    runstate.frozen_path(str(repo)).unlink()

    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.add_task("T2", str(repo), wave=2, owner_files=["owned.py"])
    ledger.close()
    capsys.readouterr()

    assert cli.main(["--config", str(config), "wave-run", "--wave", "2"]) == 2
    assert "REFUSING" in capsys.readouterr().out


@pytest.mark.skipif(not GIT, reason="git not available")
def test_preexisting_untracked_files_do_not_fail_the_wave(tmp_path, monkeypatch, capsys):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    (repo / "notes.md").write_text("pre-existing operator note", encoding="utf-8")
    config = _config(tmp_path)
    plan_path = _brownfield_plan(tmp_path, repo)
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))

    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan_path)]) == 0
    assert "exempt from the scope audit" in capsys.readouterr().out
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0


@pytest.mark.skipif(not GIT, reason="git not available")
def test_new_untracked_file_during_the_wave_still_fails(tmp_path, monkeypatch):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    (repo / "notes.md").write_text("pre-existing operator note", encoding="utf-8")
    config = _config(tmp_path)
    plan_path = _brownfield_plan(tmp_path, repo)

    class StrayRunner:
        def __init__(self, ledger):
            self.ledger = ledger

        def run_wave(self, wave, concurrency=None):
            (repo / "stray.txt").write_text("created during the wave", encoding="utf-8")
            results = []
            for task in self.ledger.list_tasks(status="queued", wave=wave):
                self.ledger.set_status(task["id"], "delivered")
                results.append({"task_id": task["id"], "outcome": "delivered",
                                "missing": [], "scan": {"turns": 1, "out_tokens": 10},
                                "server_launches": []})
            return results

    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: StrayRunner(ledger))
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan_path)]) == 0

    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 1
    ledger = Ledger(str(tmp_path / "ledger.db"))
    events = [event for event in ledger.events("T1", limit=30)
              if event["kind"] == "scope-violation"]
    ledger.close()
    assert any("stray.txt" in event["detail"] for event in events)


def _writing_runner(repo, content):
    """Runner that writes an owned file during the wave (simulates a worker)."""

    class WritingRunner:
        def __init__(self, ledger):
            self.ledger = ledger

        def run_wave(self, wave, concurrency=None):
            (repo / "feature.py").write_text(content, encoding="utf-8")
            results = []
            for task in self.ledger.list_tasks(status="queued", wave=wave):
                self.ledger.set_status(task["id"], "delivered")
                results.append({"task_id": task["id"], "outcome": "delivered",
                                "missing": [], "scan": {"turns": 1, "out_tokens": 10},
                                "server_launches": []})
            return results

    return WritingRunner


@pytest.mark.skipif(not GIT, reason="git not available")
def test_wave_created_files_are_sealed_for_the_next_wave(tmp_path, monkeypatch):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    config = _config(tmp_path)
    plan = {"project_name": "demo", "project": str(repo), "mode": "brownfield",
            "tasks": [{"id": "T1", "module": "feature.py",
                       "owner_files": ["feature.py"], "spec": "do", "wave": 1}]}
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")

    runner = _writing_runner(repo, "created by the wave\n")
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: runner(ledger))
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan_path)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0
    assert "feature.py" in runstate.load_frozen(str(repo.resolve()))["files"]

    # wave 2 with an unrelated task: without sealing this fails as added_unowned
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.add_task("T2", str(repo), wave=2, owner_files=["other.py"])
    ledger.close()
    assert cli.main(["--config", str(config), "wave-run", "--wave", "2"]) == 0


@pytest.mark.skipif(not GIT, reason="git not available")
def test_unowned_edit_of_a_sealed_file_fails_the_next_wave(tmp_path, monkeypatch):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    config = _config(tmp_path)
    plan = {"project_name": "demo", "project": str(repo), "mode": "brownfield",
            "tasks": [{"id": "T1", "module": "feature.py",
                       "owner_files": ["feature.py"], "spec": "do", "wave": 1}]}
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")

    creator = _writing_runner(repo, "created by the wave\n")
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: creator(ledger))
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan_path)]) == 0
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0

    tamperer = _writing_runner(repo, "tampered by an unowned task\n")
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: tamperer(ledger))
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.add_task("T2", str(repo), wave=2, owner_files=["other.py"])
    ledger.close()

    assert cli.main(["--config", str(config), "wave-run", "--wave", "2"]) == 1
    ledger = Ledger(str(tmp_path / "ledger.db"))
    events = [event for event in ledger.events("T2", limit=30)
              if event["kind"] == "scope-violation"]
    ledger.close()
    assert any("modified_frozen" in event["detail"] and "feature.py" in event["detail"]
               for event in events)


@pytest.mark.skipif(not GIT, reason="git not available")
def test_brownfield_wave_aborts_without_node_modules(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo,
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo,
                   capture_output=True, check=True)
    (repo / "package.json").write_text('{"name": "x"}\n', encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo,
                   capture_output=True, check=True)

    plan = {"project_name": "demo", "project": str(repo), "mode": "brownfield",
            "tasks": [{"id": "T1", "module": "src/new.py",
                       "owner_files": ["src/new.py"], "spec": "do", "wave": 1}]}
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")
    config = _config(tmp_path)

    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan_path)]) == 0
    rc = cli.main(["--config", str(config), "wave-run", "--wave", "1"])
    assert rc == 1
    ledger = Ledger(str(tmp_path / "ledger.db"))
    kinds = [event["kind"] for event in ledger.events("T1", limit=20)]
    ledger.close()
    assert "wave-abort" in kinds


class _DeadProc:
    """A worker whose process has already exited cleanly."""

    pid = 999999

    def poll(self):
        return 0


class _ClosedOut:
    def close(self):
        pass


def _fake_worker_spawn(project, trace):
    """A WorkerRunner._spawn replacement: snapshot the owned files, then deliver them."""
    from swarmflow.audit import _hash_file

    def spawn(self, task, thinking, attempt):
        before = {}
        for rel in task.get("owner_files") or []:
            path = project / rel
            before[rel] = _hash_file(path) if path.exists() else None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{rel} delivered\n", encoding="utf-8")
        trace.write_text('{"type": "agent_end"}\n', encoding="utf-8")
        return {"proc": _DeadProc(), "out": _ClosedOut(), "trace": str(trace),
                "task": task, "thinking": thinking, "before": before,
                "pgid": None, "started": time.monotonic()}

    return spawn


def test_wave_run_drives_the_real_worker_runner(tmp_path, monkeypatch):
    """The pipeline and the real runner agree on the result contract end to end."""
    project = tmp_path / "app"
    project.mkdir()
    trace = tmp_path / "trace.jsonl"
    config = load_config(str(_config(tmp_path)))
    config["worker"].update({"model": "fake/model", "pi_cli": "unused", "poll_s": 0.05,
                             "timeout_s": 10})
    config["swarm"]["stagger_s"] = 0.0
    config["git"] = {"commit_tasks": False}
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.add_task("T1", str(project), wave=1, owner_files=["feature.py"])
    monkeypatch.setattr("swarmflow.workers.kill_tree", lambda pid, pgid=None: False)
    monkeypatch.setattr(WorkerRunner, "_spawn", _fake_worker_spawn(project, trace))
    monkeypatch.setattr(WorkerRunner, "_send_prompt", lambda self, handle, prompt: None)

    rc, report = pipeline.run_wave(ledger, config, wave=1, quiet=True)

    assert rc == 0
    entry = report["results"][0]
    assert set(entry) == {"task_id", "outcome", "status", "turns", "out_tokens",
                          "server_launches"}
    assert entry["task_id"] == "T1"
    assert entry["outcome"] == "delivered"
    assert entry["status"] == "delivered"
    assert entry["turns"] == 0 and entry["out_tokens"] == 0
    assert entry["server_launches"] == 0
    assert ledger.get("T1")["status"] == "delivered"
    assert (project / "feature.py").read_text(encoding="utf-8") == "feature.py delivered\n"
    ledger.close()


def _boom(*args, **kwargs):
    raise RuntimeError("boom")


def test_sweep_crash_is_contained(tmp_path, monkeypatch, capsys):
    project = tmp_path / "app"
    config = _config(tmp_path)
    plan = _greenfield_plan(tmp_path, project, ["T1"])
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0

    monkeypatch.setattr("swarmflow.pipeline.sweep_mod.run_sweep", _boom)
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0
    out = capsys.readouterr().out
    sweep = json.loads((project / ".swarmflow" / "evidence"
                        / "wave1.sweep.json").read_text(encoding="utf-8"))
    assert sweep["indeterminate"] is True
    assert "sweep crashed: boom" in sweep["reason"]
    assert "process sweep INDETERMINATE" in out


def test_discrimination_crash_is_indeterminate(tmp_path, monkeypatch, capsys):
    project = tmp_path / "app"
    config = _config(tmp_path)
    plan = _greenfield_plan(tmp_path, project, ["T1"])
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0

    monkeypatch.setattr("swarmflow.discrimination.run_check", _boom)
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0
    out = capsys.readouterr().out
    assert "discrimination INDETERMINATE: check crashed: boom" in out
    evidence = json.loads((project / ".swarmflow" / "evidence"
                           / "wave1.discrimination.json").read_text(encoding="utf-8"))
    assert evidence["indeterminate"] is True

    # the same evidence fails the wave when the gate is enforcing
    enforce = tmp_path / "cfg_enforce.yaml"
    enforce.write_text(yaml.safe_dump({
        "paths": {"ledger": str(tmp_path / "ledger.db"),
                  "state_dir": str(tmp_path / "state")},
        "discrimination": {"mode": "enforce"}}), encoding="utf-8")
    plan2 = _greenfield_plan(tmp_path, project, ["T2"])
    assert cli.main(["--config", str(enforce), "plan-load", "--plan", str(plan2)]) == 0
    assert cli.main(["--config", str(enforce), "wave-run", "--wave", "1"]) == 1


def test_audit_crash_fails_the_wave(tmp_path, monkeypatch, capsys):
    project = tmp_path / "app"
    config = _config(tmp_path)
    plan = _greenfield_plan(tmp_path, project, ["T1"])
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0

    monkeypatch.setattr(pipeline, "audit", _boom)
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 1
    out = capsys.readouterr().out
    ledger = Ledger(str(tmp_path / "ledger.db"))
    kinds = [event["kind"] for event in ledger.events("T1", limit=20)]
    ledger.close()
    assert "SCOPE AUDIT ERROR: boom" in out
    assert "audit-error" in kinds


@pytest.mark.skipif(not GIT, reason="git not available")
def test_seal_crash_is_contained(tmp_path, monkeypatch, capsys):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n"})
    config = _config(tmp_path)
    plan = _brownfield_plan_with(tmp_path, repo, [_task("T1", ["feature.py"])])
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0

    monkeypatch.setattr(pipeline, "seal", _boom)
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0
    out = capsys.readouterr().out
    assert "seal failed, baseline left unsealed: boom" in out
    assert not (repo / ".swarmflow" / "evidence" / "wave1.seal.json").exists()


@pytest.mark.skipif(not GIT, reason="git not available")
def test_commit_exception_is_contained(tmp_path, monkeypatch, capsys):
    repo = _brownfield_repo(tmp_path, {"app.py": "x = 1\n", "a.py": "old\n"})
    config = _config(tmp_path)
    plan = _brownfield_plan_with(tmp_path, repo, [_task("T1", ["a.py"])])
    monkeypatch.setattr(pipeline, "_runner", lambda config, ledger, root: FakeRunner(ledger))
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan)]) == 0
    (repo / "a.py").write_text("new\n", encoding="utf-8")     # a change the commit must take

    monkeypatch.setattr("swarmflow.gitutil.commit_paths", _boom)
    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 0
    out = capsys.readouterr().out
    ledger = Ledger(str(tmp_path / "ledger.db"))
    kinds = [event["kind"] for event in ledger.events("T1", limit=20)]
    ledger.close()
    assert "commit failed for T1: RuntimeError: boom" in out
    assert "commit-failed" in kinds
    assert _git_out(repo, "log", "--format=%s").splitlines() == ["baseline"]
