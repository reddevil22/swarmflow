"""Wave-run integration: regression gating, abort path, mixed-project refusal."""

import json
import subprocess
import sys

import pytest
import yaml

from swarmflow import cli
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
    config = {"paths": {"ledger": str(tmp_path / "ledger.db")}}
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
        "paths": {"ledger": str(tmp_path / "ledger.db")},
        "discrimination": {"mode": "enforce"}}), encoding="utf-8")
    task2 = dict(task, id="T2")
    plan2 = dict(plan, tasks=[task2])
    plan_path2 = tmp_path / "plan2.yaml"
    plan_path2.write_text(yaml.safe_dump(plan2), encoding="utf-8")
    assert cli.main(["--config", str(enforce_config), "plan-load",
                     "--plan", str(plan_path2)]) == 0
    assert cli.main(["--config", str(enforce_config), "wave-run", "--wave", "1"]) == 1


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
