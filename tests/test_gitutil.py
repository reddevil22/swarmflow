"""Git helpers for per-task commits: literal pathspecs, ignored paths, the changed set."""

import subprocess

import pytest

from swarmflow.gitutil import changed_against_head, commit_paths, is_ignored


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
    (root / ".gitignore").write_text("logs/\n", encoding="utf-8")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    if commit:
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "baseline")
    return root


@pytest.mark.skipif(not GIT, reason="git not available")
def test_changed_against_head_covers_modified_deleted_and_untracked(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("new\n", encoding="utf-8")
    (repo / "gone.py").write_text("bye\n", encoding="utf-8")
    _git(repo, "add", "gone.py")
    _git(repo, "commit", "-qm", "add gone")
    (repo / "gone.py").unlink()

    changed = changed_against_head(repo, ["app.py", "new.py", "gone.py", "untouched.py"])

    assert sorted(changed) == ["app.py", "gone.py", "new.py"]


@pytest.mark.skipif(not GIT, reason="git not available")
def test_is_ignored_reads_the_ignore_rules(tmp_path):
    repo = _repo(tmp_path / "repo")

    assert is_ignored(repo, "logs/run.log") is True
    assert is_ignored(repo, "app.py") is False


@pytest.mark.skipif(not GIT, reason="git not available")
def test_commit_paths_refuses_to_read_a_path_as_a_pathspec(tmp_path):
    """`:/` or a glob in a plan must never sweep the tree into a task's commit."""
    repo = _repo(tmp_path / "repo")
    (repo / "unrelated.py").write_text("not this task's work\n", encoding="utf-8")
    (repo / "a.py").write_text("task work\n", encoding="utf-8")

    assert changed_against_head(repo, [":/", "*.py"]) == []

    ok, detail = commit_paths(repo, [":/"], "task commit", "swarmflow", "s@example.com")

    assert ok is False
    assert "did not match" in detail
    assert _git(repo, "log", "--format=%s").splitlines() == ["baseline"]
    assert "unrelated.py" in _git(repo, "status", "--porcelain")


@pytest.mark.skipif(not GIT, reason="git not available")
def test_commit_paths_refuses_an_ignored_path_and_leaves_the_index_alone(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "logs").mkdir()
    (repo / "logs" / "run.log").write_text("log\n", encoding="utf-8")
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "logs/run.log", "-f")
    _git(repo, "commit", "-qm", "tracked log")

    ok, detail = commit_paths(repo, ["logs/run.log"], "task commit", "swarmflow",
                              "s@example.com")

    assert ok is False
    assert "ignored" in detail.lower()
    assert _git(repo, "diff", "--cached", "--name-only") == ""


@pytest.mark.skipif(not GIT, reason="git not available")
def test_commit_paths_does_not_stage_anything_when_add_fails(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")

    ok, _ = commit_paths(repo, ["a.py", "logs/nope/missing.log"], "task commit",
                         "swarmflow", "s@example.com")

    assert ok is False
    assert _git(repo, "diff", "--cached", "--name-only") == ""


@pytest.mark.skipif(not GIT, reason="git not available")
def test_commit_paths_makes_a_root_commit_in_a_repository_without_history(tmp_path):
    repo = _repo(tmp_path / "repo", commit=False)
    (repo / "a.py").write_text("first\n", encoding="utf-8")

    ok, sha = commit_paths(repo, ["a.py"], "task commit", "swarmflow", "s@example.com")

    assert ok is True and sha
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"


@pytest.mark.skipif(not GIT, reason="git not available")
def test_commit_paths_records_a_deletion(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "doomed.py").write_text("bye\n", encoding="utf-8")
    _git(repo, "add", "doomed.py")
    _git(repo, "commit", "-qm", "add doomed")
    (repo / "doomed.py").unlink()

    ok, sha = commit_paths(repo, ["doomed.py"], "task deletes a file", "swarmflow",
                           "s@example.com")

    assert ok is True and sha
    assert "doomed.py" in _git(repo, "show", "--name-status", "--format=", "HEAD")


@pytest.mark.skipif(not GIT, reason="git not available")
def test_commit_paths_leaves_the_operators_staged_work_alone(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "wip.py").write_text("operator\n", encoding="utf-8")
    _git(repo, "add", "wip.py")
    (repo / "a.py").write_text("task work\n", encoding="utf-8")

    ok, _ = commit_paths(repo, ["a.py"], "task commit", "swarmflow", "s@example.com")

    assert ok is True
    assert _git(repo, "diff", "--cached", "--name-only") == "wip.py"
    assert _git(repo, "show", "--name-only", "--format=", "HEAD") == "a.py"
