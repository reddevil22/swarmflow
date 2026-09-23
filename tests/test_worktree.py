"""Run-worktree mechanics: create, reuse, and the cases that must refuse."""

import subprocess
from pathlib import Path

import pytest

from swarmflow.worktree import default_path, ensure, listed


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


GIT = _git_available()


def _git(root, *args) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo(root, commit: bool = True):
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    if commit:
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "baseline")
    return root


def test_default_path_sits_inside_the_projects_ignored_dir(tmp_path):
    assert default_path(tmp_path / "repo", "demo") == \
        (tmp_path / "repo" / ".swarmflow" / "worktrees" / "demo").resolve()


@pytest.mark.skipif(not GIT, reason="git not available")
def test_ensure_creates_a_checkout_on_the_run_branch(tmp_path):
    repo = _repo(tmp_path / "repo")
    target = default_path(repo, "demo")
    before = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    result = ensure(repo, target, "swarmflow/demo")

    assert result["action"] == "created"
    assert (target / "app.py").read_text(encoding="utf-8") == "x = 1\n"
    assert _git(target, "rev-parse", "--abbrev-ref", "HEAD") == "swarmflow/demo"
    assert _git(target, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")
    # the project checkout never switch branches or worktrees
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == before
    assert [Path(p).resolve() for p in listed(repo)] == [repo.resolve(), target.resolve()]


@pytest.mark.skipif(not GIT, reason="git not available")
def test_ensure_reuses_a_registered_worktree(tmp_path):
    repo = _repo(tmp_path / "repo")
    target = default_path(repo, "demo")
    assert ensure(repo, target, "swarmflow/demo")["action"] == "created"

    again = ensure(repo, target, "swarmflow/demo")

    assert again["action"] == "reused"
    assert (target / "app.py").exists()


@pytest.mark.skipif(not GIT, reason="git not available")
def test_ensure_refuses_a_repository_without_a_commit(tmp_path):
    repo = _repo(tmp_path / "repo", commit=False)

    result = ensure(repo, default_path(repo, "demo"), "swarmflow/demo")

    assert result["action"] == "failed"
    assert "no resolvable HEAD" in result["reason"]


@pytest.mark.skipif(not GIT, reason="git not available")
def test_ensure_refuses_an_occupied_path(tmp_path):
    repo = _repo(tmp_path / "repo")
    target = default_path(repo, "demo")
    target.mkdir(parents=True)

    result = ensure(repo, target, "swarmflow/demo")

    assert result["action"] == "failed"
    assert "not a worktree" in result["reason"]


@pytest.mark.skipif(not GIT, reason="git not available")
def test_ensure_refuses_a_branch_that_is_checked_out_elsewhere(tmp_path):
    repo = _repo(tmp_path / "repo")
    assert ensure(repo, default_path(repo, "one"), "swarmflow/demo")["action"] == "created"

    result = ensure(repo, default_path(repo, "two"), "swarmflow/demo")

    assert result["action"] == "failed"
    assert result["reason"]


@pytest.mark.skipif(not GIT, reason="git not available")
def test_ensure_refuses_a_worktree_on_another_branch(tmp_path):
    """Pointing a second run at the first run's path must not switch its branch."""
    repo = _repo(tmp_path / "repo")
    target = default_path(repo, "one")
    assert ensure(repo, target, "swarmflow/one")["action"] == "created"

    result = ensure(repo, target, "swarmflow/two")

    assert result["action"] == "failed"
    assert "swarmflow/one" in result["reason"]
    assert _git(target, "rev-parse", "--abbrev-ref", "HEAD") == "swarmflow/one"


@pytest.mark.skipif(not GIT, reason="git not available")
def test_ensure_refuses_a_project_that_is_not_the_repository_toplevel(tmp_path):
    repo = _repo(tmp_path / "repo")
    nested = repo / "packages" / "app"
    nested.mkdir(parents=True)

    result = ensure(nested, default_path(nested, "demo"), "swarmflow/demo")

    assert result["action"] == "failed"
    assert "toplevel" in result["reason"]


@pytest.mark.skipif(not GIT, reason="git not available")
def test_ensure_refuses_a_directory_that_is_not_a_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    result = ensure(plain, default_path(plain, "demo"), "swarmflow/demo")

    assert result["action"] == "failed"
    assert "not a git repository" in result["reason"]


@pytest.mark.skipif(not GIT, reason="git not available")
def test_ensure_refuses_the_project_checkout_as_the_worktree_path(tmp_path):
    repo = _repo(tmp_path / "repo")

    result = ensure(repo, repo, "swarmflow/demo")

    assert result["action"] == "failed"
    assert "cannot be the project checkout" in result["reason"]


@pytest.mark.skipif(not GIT, reason="git not available")
def test_ensure_refuses_a_registered_worktree_that_is_gone(tmp_path):
    import shutil

    repo = _repo(tmp_path / "repo")
    target = default_path(repo, "demo")
    assert ensure(repo, target, "swarmflow/demo")["action"] == "created"
    shutil.rmtree(target)

    result = ensure(repo, target, "swarmflow/demo")

    assert result["action"] == "failed"
    assert "worktree prune" in result["reason"]
