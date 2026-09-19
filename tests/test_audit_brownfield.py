"""Brownfield audit tests: git-based semantics, per-wave owner scoping."""

import subprocess

import pytest

from swarmflow.audit import audit, freeze


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


def _commit_all(root, message="snapshot"):
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=root, capture_output=True, check=True)


@pytest.mark.skipif(not GIT, reason="git not available")
def test_brownfield_audit_git_semantics(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / ".gitignore").write_text(".swarmflow/\nlogs/\nnode_modules/\n",
                                     encoding="utf-8")
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "junk.js").write_text("noise", encoding="utf-8")
    _commit_all(repo, "baseline")

    freeze(str(repo), {"T1": ["owned.py"]}, mode="brownfield")
    assert audit(str(repo))["ok"] is True          # gitignored junk is invisible

    (repo / "tracked.py").write_text("x = 2\n", encoding="utf-8")
    result = audit(str(repo))
    assert any(v["kind"] == "modified_frozen" and v["path"] == "tracked.py"
               for v in result["violations"])

    subprocess.run(["git", "checkout", "--", "tracked.py"], cwd=repo,
                   capture_output=True, check=True)
    (repo / "stray.txt").write_text("oops", encoding="utf-8")
    (repo / "owned.py").write_text("new file", encoding="utf-8")
    result = audit(str(repo))
    kinds = {(v["kind"], v["path"]) for v in result["violations"]}
    assert ("added_unowned", "stray.txt") in kinds
    assert not any(path == "owned.py" for _, path in kinds)


@pytest.mark.skipif(not GIT, reason="git not available")
def test_brownfield_per_wave_owner_scoping(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / ".gitignore").write_text(".swarmflow/\nlogs/\n", encoding="utf-8")
    (repo / "manifest.json").write_text("{}\n", encoding="utf-8")
    _commit_all(repo, "baseline")

    freeze(str(repo), {"W1": ["a.py"]}, mode="brownfield")
    (repo / "manifest.json").write_text('{"x": 1}\n', encoding="utf-8")
    result = audit(str(repo))
    assert any(v["kind"] == "modified_frozen" for v in result["violations"])

    # re-freeze for wave 2 where the manifest is owned: mutation is allowed
    freeze(str(repo), {"W2": ["manifest.json"]}, mode="brownfield")
    (repo / "manifest.json").write_text('{"x": 2}\n', encoding="utf-8")
    assert audit(str(repo))["ok"] is True


def test_greenfield_baseline_stores_ignores_snapshot(tmp_path):
    (tmp_path / "skipme.txt").write_text("v1", encoding="utf-8")
    (tmp_path / "other.txt").write_text("v1", encoding="utf-8")
    freeze(str(tmp_path), {}, ignores=["skipme.txt"])
    (tmp_path / "skipme.txt").write_text("v2", encoding="utf-8")
    assert audit(str(tmp_path))["ok"] is True      # snapshot ignores still apply
    (tmp_path / "other.txt").write_text("v2", encoding="utf-8")
    assert audit(str(tmp_path))["ok"] is False
