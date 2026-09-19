"""Input validation: plan ids/paths/thinking, config values, link paths."""

import os
from pathlib import Path

import pytest
import yaml

from swarmflow import discrimination
from swarmflow.config import load_config, validate_config
from swarmflow.plan import validate_plan


def _task(**overrides):
    task = {"id": "T1", "module": "app.py", "owner_files": ["app.py"], "spec": "do"}
    task.update(overrides)
    return task


def test_plan_ids_paths_and_thinking_are_validated():
    plan = {"project_name": "demo", "tasks": [
        _task(id="..\\..\\evil"),
        _task(id="bad id"),
        _task(id="T2", owner_files=["../outside.py"]),
        _task(id="T3", owner_files=["/abs.py"]),
        _task(id="T4", owner_files=[r"C:\abs.py"]),
        _task(id="T5", thinking="off&calc"),
        _task(id="T6", acceptance="not-a-list"),
        _task(id="T7", module=3),
        _task(id="T8", files_to_read=["ok.py", "../up.py"]),
    ]}
    errors = validate_plan(plan)
    text = "\n".join(errors)

    assert "id must match" in text
    assert "must not contain '..' segments" in text
    assert "must be relative to the project root" in text
    assert "thinking must be one of" in text
    assert "acceptance must be a list" in text
    assert "module must be a string" in text


def test_plan_project_name_is_validated():
    plan = {"project_name": "bad name", "tasks": [_task()]}
    assert any("project_name" in error for error in validate_plan(plan))


def test_existing_plans_still_validate():
    root = Path(__file__).resolve().parent.parent
    plans = sorted(root.glob("pilot/*.yaml")) + sorted(root.glob("examples/*.yaml"))
    assert plans, "no plan files found"
    for path in plans:
        plan = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert validate_plan(plan) == [], f"{path.name} should validate"


def _write_config(tmp_path, data):
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


def test_empty_defaults_load(tmp_path):
    config = load_config(_write_config(tmp_path, {}))
    assert config["worker"]["model"] == ""
    assert config["frontier"]["effort"] is None


def test_unsafe_values_are_rejected(tmp_path):
    with pytest.raises(ValueError) as exc:
        load_config(_write_config(tmp_path, {"worker": {"model": "evil & calc"}}))
    assert "worker.model" in str(exc.value)
    with pytest.raises(ValueError):
        load_config(_write_config(tmp_path, {"worker": {"thinking": "high; rm -rf /"}}))


def test_safe_values_pass(tmp_path):
    config = load_config(_write_config(tmp_path, {
        "worker": {"model": "vllm-79/qwen36:latest"},
        "frontier": {"effort": "high"},
        "discrimination": {"link_dirs": ["node_modules"]},
        "sweep": {"ignore_names": ["vite"]}}))
    assert config["worker"]["model"] == "vllm-79/qwen36:latest"


def test_cmd_routed_paths_with_spaces_are_rejected():
    problems = validate_config({"worker": {"pi_cli": r"C:\Program Files\pi.cmd"},
                                "frontier": {}, "discrimination": {}, "sweep": {}})
    assert any("cmd.exe" in problem for problem in problems)
    assert validate_config({"worker": {"pi_cli": r"C:\tools\pi.cmd"}, "frontier": {},
                            "discrimination": {}, "sweep": {}}) == []


def test_link_entries_are_validated():
    problems = validate_config({"worker": {}, "frontier": {},
                                "discrimination": {"link_dirs": ["node_modules & calc"]},
                                "sweep": {"ignore_names": ["ok", "bad name"]}})
    assert any("link_dirs" in problem for problem in problems)
    assert any("ignore_names" in problem for problem in problems)


@pytest.mark.skipif(os.name != "nt", reason="junction creation is Windows-only")
def test_mklink_paths_with_metacharacters_are_refused(tmp_path):
    source = tmp_path / "a&b"
    source.mkdir()
    target = tmp_path / "target"
    assert discrimination._link_dir(source, target) is False
    assert not target.exists()
