"""Worker brief tests: both modes render, stack rules, rules injection, no_changes."""

import json
from pathlib import Path

from swarmflow.audit import _hash_file
from swarmflow.config import REPO_ROOT
from swarmflow.ledger import Ledger
from swarmflow.workers import WorkerRunner, delivery_changed, stack_rules_block


def _runner(tmp_path, project):
    ledger = Ledger(str(tmp_path / "l.db"))
    runner = WorkerRunner({"worker": {}, "paths": {"logs_dir": "logs"}, "swarm": {}},
                          ledger, str(project), REPO_ROOT)
    return runner, ledger


def _task(**overrides):
    task = {"id": "T1", "owner_files": ["a.py"], "spec_path": "", "acceptance": [],
            "test_command": "python -m pytest tests/test_a.py -q", "files_to_read": []}
    task.update(overrides)
    return task


def test_greenfield_prompt_renders_generic_rules(tmp_path):
    project = tmp_path / "g"
    project.mkdir()
    runner, ledger = _runner(tmp_path, project)
    prompt = runner.build_prompt(_task())
    assert "Read AGENTS.md first" in prompt
    assert "WORKING RULES (mandatory)" not in prompt
    assert "NEVER install or upgrade tools" in prompt      # generic stack block
    assert "python -m pytest tests/test_a.py -q" in prompt
    ledger.close()


def test_greenfield_defaults_to_node_rules_with_package_json(tmp_path):
    project = tmp_path / "g"
    project.mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")
    runner, ledger = _runner(tmp_path, project)
    prompt = runner.build_prompt(_task())
    assert "ONLY npm and npx" in prompt
    ledger.close()


def test_brownfield_prompt_injects_rules_stack_and_regression(tmp_path):
    project = tmp_path / "b"
    (project / ".swarmflow").mkdir(parents=True)
    (project / ".swarmflow" / "run.json").write_text(
        json.dumps({"mode": "brownfield"}), encoding="utf-8")
    (project / ".swarmflow" / "recon.json").write_text(json.dumps({
        "stacks": [{"stack": "python", "evidence": "pyproject.toml"}],
        "commands": {"regression": {"command": "python -m pytest -q",
                                    "evidence": "tests/ present"}},
        "conventions": ["AGENTS.md"],
    }), encoding="utf-8")
    runner, ledger = _runner(tmp_path, project)
    prompt = runner.build_prompt(_task(files_to_read=["existing.py"]))
    assert "WORKING RULES (mandatory)" in prompt
    assert "REPORT exactly these sections" in prompt        # injected from AGENTS.worker.md
    assert "poetry" in prompt                               # python stack rules
    assert "python -m pytest -q" in prompt                  # must keep working
    assert "existing.py" in prompt
    assert "its own conventions" in prompt
    assert "ONLY npm" not in prompt
    ledger.close()


def test_stack_rules_block_selection():
    assert "npm" in stack_rules_block(["node"])
    assert "poetry" in stack_rules_block(["python"])
    assert "cargo" in stack_rules_block(["rust"])
    assert "go mod tidy" in stack_rules_block(["go"])
    assert "install or upgrade" in stack_rules_block([])
    assert "npm" in stack_rules_block([], has_package_json=True)


def test_delivery_changed_detects_each_change_kind(tmp_path):
    (tmp_path / "a.py").write_text("v1", encoding="utf-8")
    before = {"a.py": _hash_file(tmp_path / "a.py"), "new.py": None}
    assert delivery_changed(tmp_path, before) is False

    (tmp_path / "a.py").write_text("v2", encoding="utf-8")
    assert delivery_changed(tmp_path, before) is True

    (tmp_path / "a.py").write_text("v1", encoding="utf-8")
    (tmp_path / "new.py").write_text("created", encoding="utf-8")
    assert delivery_changed(tmp_path, before) is True

    (tmp_path / "new.py").unlink()
    (tmp_path / "a.py").unlink()
    assert delivery_changed(tmp_path, before) is True
