"""Brownfield audit tests: git-based semantics, per-wave owner scoping."""

import subprocess

import pytest

from swarmflow.audit import audit, freeze, seal


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


def test_carry_over_keeps_unowned_changes_visible(tmp_path):
    """The reported bug: the next wave's freeze must not re-bless a between-wave edit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "base")

    freeze(str(repo), {"W1": ["other.py"]}, mode="brownfield")
    (repo / "tracked.py").write_text("x = 2\n", encoding="utf-8")
    assert any(v["kind"] == "modified_frozen" for v in audit(str(repo))["violations"])

    freeze(str(repo), {"W2": ["other.py"]}, mode="brownfield", carry_over=True)
    result = audit(str(repo))
    assert any(v["kind"] == "modified_frozen" and v["path"] == "tracked.py"
               for v in result["violations"])


def test_carry_over_reports_carried_and_refreshed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.py").write_text("1", encoding="utf-8")
    (repo / "b.py").write_text("1", encoding="utf-8")
    _commit_all(repo, "base")

    first = freeze(str(repo), {"W1": ["a.py"]}, mode="brownfield")
    second = freeze(str(repo), {"W2": ["b.py"]}, mode="brownfield", carry_over=True)
    assert first["carried"] == 0 and first["entries"] == 2
    assert second["carried"] == 1 and second["refreshed"] == 1


def test_seal_bakes_owned_changes_and_stays_detectable_afterwards(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "manifest.json").write_text("{}\n", encoding="utf-8")
    (repo / "other.py").write_text("1", encoding="utf-8")
    _commit_all(repo, "base")

    freeze(str(repo), {"W1": ["manifest.json"]}, mode="brownfield")
    (repo / "manifest.json").write_text('{"x": 1}\n', encoding="utf-8")
    assert audit(str(repo))["ok"] is True          # owned: skipped by the audit
    sealed = seal(str(repo), {"W1": ["manifest.json"]})
    assert sealed == {"sealed": 1, "dropped": 0}

    freeze(str(repo), {"W2": ["other.py"]}, mode="brownfield", carry_over=True)
    assert audit(str(repo))["ok"] is True          # sealed content is the baseline

    (repo / "manifest.json").write_text('{"x": 2}\n', encoding="utf-8")
    assert any(v["kind"] == "modified_frozen" and v["path"] == "manifest.json"
               for v in audit(str(repo))["violations"])


def test_carry_over_deletions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "gone.py").write_text("1", encoding="utf-8")
    (repo / "owned_del.py").write_text("1", encoding="utf-8")
    _commit_all(repo, "base")

    freeze(str(repo), {"W1": ["owned_del.py"]}, mode="brownfield")
    (repo / "gone.py").unlink()
    (repo / "owned_del.py").unlink()
    seal(str(repo), {"W1": ["owned_del.py"]})      # an owner may remove its own file
    freeze(str(repo), {"W2": []}, mode="brownfield", carry_over=True)

    kinds = {(v["kind"], v["path"]) for v in audit(str(repo))["violations"]}
    assert ("deleted_frozen", "gone.py") in kinds
    assert ("deleted_frozen", "owned_del.py") not in kinds


def test_carry_over_without_previous_baseline_full_bakes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.py").write_text("1", encoding="utf-8")
    _commit_all(repo, "base")
    info = freeze(str(repo), {}, mode="brownfield", carry_over=True)
    assert info["carried"] == 0
    assert info["entries"] == 1
