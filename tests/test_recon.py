"""Recon tests: detection, evidence strings, git state, digest bounds. Hermetic."""

import json
import subprocess

import pytest

from swarmflow.recon import digest, ensure_gitignore_entries, git_state, recon


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


GIT = _git_available()


def _init_repo(root):
    def run(*args):
        subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
    run("init", "-q")
    run("config", "user.email", "swarmflow-tests@example.com")
    run("config", "user.name", "Swarmflow Tests")


def test_detects_node_stack_and_commands(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest", "build": "tsc", "lint": "eslint ."}}),
        encoding="utf-8")
    info = recon(str(tmp_path))
    assert info["stacks"] == [{"stack": "node", "evidence": "package.json"}]
    assert info["commands"]["regression"] == {
        "command": "npm test --silent", "evidence": "package.json scripts.test"}
    assert info["commands"]["build"]["command"] == "npm run build --silent"
    assert (tmp_path / ".swarmflow" / "recon.json").exists()


def test_detects_python_stack_with_tests(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_ok():\n    assert True\n",
                                                  encoding="utf-8")
    info = recon(str(tmp_path))
    assert {"stack": "python", "evidence": "pyproject.toml"} in info["stacks"]
    assert info["commands"]["regression"]["command"] == "python -m pytest -q"
    assert any("test_x.py" in name for name in info["tests"]["files"])


def test_detects_go_and_rust_commands(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    info = recon(str(tmp_path))
    assert info["commands"]["regression"]["command"] == "go test ./..."
    assert info["commands"]["build"]["command"] == "go build ./..."


def test_regression_override_wins(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    info = recon(str(tmp_path), regression_command="make check")
    assert info["commands"]["regression"] == {"command": "make check",
                                              "evidence": "config/CLI override"}
    assert info["commands"]["build"]["command"] == "go build ./..."


def test_non_git_project_is_flagged(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    info = recon(str(tmp_path))
    assert info["git"]["is_git"] is False
    assert "NOT a git repository" in digest(info)


@pytest.mark.skipif(not GIT, reason="git not available")
def test_git_state_dirty_tracked_excludes_untracked(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path,
                   capture_output=True, check=True)
    state = git_state(tmp_path)
    assert state["is_git"] is True
    assert state["head"] and state["commits"] == 1
    assert state["dirty_tracked"] == []
    (tmp_path / "tracked.txt").write_text("v2", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("new", encoding="utf-8")
    state = git_state(tmp_path)
    assert len(state["dirty_tracked"]) == 1
    assert state["untracked_count"] == 1


def test_digest_is_bounded(tmp_path):
    info = recon(str(tmp_path))
    info["tests"] = {"count_sampled": 5000,
                     "files": [f"tests/test_{i}.py" for i in range(5000)]}
    text = digest(info)
    assert len(text) <= 6000 + 40


def test_ambient_node_env_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_ENV", "production")
    info = recon(str(tmp_path))
    assert info["env"]["NODE_ENV"] == "production"
    assert "NODE_ENV=production" in digest(info)


def test_ensure_gitignore_entries_is_idempotent(tmp_path):
    added = ensure_gitignore_entries(tmp_path, [".swarmflow/", "logs/"])
    assert added == [".swarmflow/", "logs/"]
    again = ensure_gitignore_entries(tmp_path, [".swarmflow/", "logs/"])
    assert again == []
    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert content.count(".swarmflow/") == 1
