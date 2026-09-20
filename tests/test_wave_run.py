"""Wave-run integration: regression gating, abort path, mixed-project refusal."""

import json
import subprocess
import sys
import time

import pytest
import yaml

from swarmflow import cli, procs, runstate
from swarmflow.ledger import Ledger


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
    monkeypatch.setattr(cli, "_runner", lambda config, ledger, root: FakeRunner(ledger))

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
    monkeypatch.setattr(cli, "_runner", lambda config, ledger, root: FakeRunner(ledger))

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
    monkeypatch.setattr(cli, "_runner", lambda config, ledger, root: FakeRunner(ledger))

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
    monkeypatch.setattr(cli, "_runner", lambda config, ledger, root: FakeRunner(ledger))

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
    monkeypatch.setattr(cli, "_runner", lambda config, ledger, root: FakeRunner(ledger))

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
    server_pid = None
    for _ in range(25):
        time.sleep(0.2)
        for pid, entry in procs.snapshot().items():
            if "http.server" in entry["cmdline"] and str(port) in entry["cmdline"] \
                    and entry["name"].lower().startswith("python"):
                server_pid = pid
                break
        if server_pid:
            break
    assert server_pid, "stale listener was not started"
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
        time.sleep(1)
        assert not procs.is_alive(server_pid)
    finally:
        if server_pid and procs.is_alive(server_pid):
            procs.kill_tree(server_pid, server_pid)


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
        for _ in range(25):
            time.sleep(0.2)
            hit = [pid for pid, entry in procs.snapshot().items()
                   if str(script) in entry["cmdline"] and pid not in leaked]
            if hit:
                leaked.append(hit[0])
                return

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

    monkeypatch.setattr(cli, "_runner",
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
        assert not procs.is_alive(leaked[1])
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
    monkeypatch.setattr(cli, "_runner", lambda config, ledger, root: FakeRunner(ledger))

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
    monkeypatch.setattr(cli, "_runner", lambda config, ledger, root: FakeRunner(ledger))
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
    state, result = cli._run_audit(str(project), ledger, "T1", brownfield=True)
    skipped, _ = cli._run_audit(str(project), ledger, "T1", brownfield=False)
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
    monkeypatch.setattr(cli, "_runner", lambda config, ledger, root: FakeRunner(ledger))
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

    monkeypatch.setattr(cli, "_runner",
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
    monkeypatch.setattr(cli, "_runner", lambda config, ledger, root: FakeRunner(ledger))

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
    monkeypatch.setattr(cli, "_runner", lambda config, ledger, root: FakeRunner(ledger))

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
    monkeypatch.setattr(cli, "_runner", lambda config, ledger, root: FakeRunner(ledger))

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

    monkeypatch.setattr(cli, "_runner", lambda config, ledger, root: StrayRunner(ledger))
    assert cli.main(["--config", str(config), "plan-load", "--plan", str(plan_path)]) == 0

    assert cli.main(["--config", str(config), "wave-run", "--wave", "1"]) == 1
    ledger = Ledger(str(tmp_path / "ledger.db"))
    events = [event for event in ledger.events("T1", limit=30)
              if event["kind"] == "scope-violation"]
    ledger.close()
    assert any("stray.txt" in event["detail"] for event in events)


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
