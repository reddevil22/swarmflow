"""Brownfield plan-load tests: mode validation, scaffold safety, preflight e2e."""

import json
import subprocess

import pytest
import yaml

from swarmflow import cli
from swarmflow.config import REPO_ROOT
from swarmflow.plan import scaffold, validate_plan


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


GIT = _git_available()


def _task(task_id="T1", files=("app.py",)):
    return {"id": task_id, "module": files[0], "owner_files": list(files),
            "spec": "implement", "wave": 1}


def _write_plan(tmp_path, project, mode="brownfield"):
    plan = {"project_name": "demo", "project": str(project), "mode": mode,
            "tasks": [_task()]}
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(plan), encoding="utf-8")
    return path


def _write_config(tmp_path):
    config = {"paths": {"ledger": str(tmp_path / "ledger.db")}}
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _init_repo(root):
    def run(*args):
        subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
    run("init", "-q")
    run("config", "user.email", "swarmflow-tests@example.com")
    run("config", "user.name", "Swarmflow Tests")
    (root / "existing.py").write_text("x = 1\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "baseline")


def test_mode_validation_rejects_unknown_mode():
    errors = validate_plan({"mode": "wat", "tasks": [_task()]})
    assert any("mode must be" in error for error in errors)


def test_files_to_read_must_be_string_list():
    task = _task()
    task["files_to_read"] = "app.py"
    errors = validate_plan({"tasks": [task]})
    assert any("files_to_read" in error for error in errors)


def test_scaffold_greenfield_unchanged_shape(tmp_path):
    plan = {"project_name": "g", "tasks": [_task()]}
    info = scaffold(plan, tmp_path / "green", REPO_ROOT, mode="greenfield")
    assert (tmp_path / "green" / "AGENTS.md").exists()
    assert (tmp_path / "green" / "SPEC.md").exists()
    assert (tmp_path / "green" / "specs" / "T1.md").exists()
    assert info["artifacts_dir"] == ""


def test_scaffold_brownfield_never_touches_user_files(tmp_path):
    project = tmp_path / "brown"
    project.mkdir()
    (project / "AGENTS.md").write_text("MY RULES\n", encoding="utf-8")
    plan = {"project_name": "b", "tasks": [_task()]}
    info = scaffold(plan, project, REPO_ROOT, mode="brownfield")
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "MY RULES\n"
    assert not (project / "SPEC.md").exists()
    assert (project / ".swarmflow" / "SPEC.md").exists()
    assert (project / ".swarmflow" / "specs" / "T1.md").exists()
    assert not (project / ".git").exists()
    assert info["git"] == "missing"


@pytest.mark.skipif(not GIT, reason="git not available")
def test_brownfield_plan_load_end_to_end(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    _init_repo(project)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project,
                          capture_output=True, text=True, check=True).stdout.strip()
    plan_path = _write_plan(tmp_path, project)
    config_path = _write_config(tmp_path)

    rc = cli.main(["--config", str(config_path), "plan-load", "--plan", str(plan_path)])
    assert rc == 0
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project,
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch == "swarmflow/demo"
    run_state = json.loads((project / ".swarmflow" / "run.json").read_text(encoding="utf-8"))
    assert run_state["base_sha"] == head
    assert run_state["mode"] == "brownfield"
    assert ".swarmflow/" in (project / ".gitignore").read_text(encoding="utf-8")
    assert (project / ".swarmflow" / "recon.json").exists()

    # second load: reuses the branch, keeps base_sha (first-write-wins)
    rc = cli.main(["--config", str(config_path), "plan-load", "--plan", str(plan_path)])
    assert rc == 0
    run_state2 = json.loads((project / ".swarmflow" / "run.json").read_text(encoding="utf-8"))
    assert run_state2["base_sha"] == head

    # modified tracked file -> refusal, then --allow-dirty override
    (project / "existing.py").write_text("x = 2\n", encoding="utf-8")
    rc = cli.main(["--config", str(config_path), "plan-load", "--plan", str(plan_path)])
    assert rc == 2
    rc = cli.main(["--config", str(config_path), "plan-load", "--plan", str(plan_path),
                   "--allow-dirty"])
    assert rc == 0
