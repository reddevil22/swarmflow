"""Brownfield audit tests: git-based semantics, per-wave owner scoping."""

import subprocess

import pytest

from swarmflow import runstate
from swarmflow.audit import audit, freeze, seal


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


GIT = _git_available()

# every test here builds a real git repo, guarded once at module level
pytestmark = pytest.mark.skipif(not GIT, reason="git not available")


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
    assert sealed == {"sealed": 1, "dropped": 0, "added": 0, "capped": 0}

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


def test_untracked_baseline_exempts_preexisting_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "base")
    (repo / "notes.md").write_text("pre-existing note", encoding="utf-8")
    freeze(str(repo), {"T1": ["owned.py"]}, mode="brownfield")

    assert audit(str(repo))["violations"], "without a baseline the file violates"
    result = audit(str(repo), untracked_baseline=["notes.md"])
    assert result["ok"] is True

    # a file appearing after the preflight still violates
    (repo / "stray.txt").write_text("created during the run", encoding="utf-8")
    result = audit(str(repo), untracked_baseline=["notes.md"])
    kinds = {(violation["kind"], violation["path"]) for violation in result["violations"]}
    assert ("added_unowned", "stray.txt") in kinds
    assert not any(path == "notes.md" for _, path in kinds)


def test_untracked_baseline_reads_runstate_and_matches_literally(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "base")
    (repo / "notes[1].md").write_text("brackets are a glob class", encoding="utf-8")
    freeze(str(repo), {"T1": ["owned.py"]}, mode="brownfield")

    runstate.save_run(str(repo), {"untracked_baseline": ["notes[1].md"]})
    assert audit(str(repo))["ok"] is True          # exact match, not a glob
    (repo / "notes1.md").write_text("a glob would have matched this", encoding="utf-8")
    kinds = {violation["path"] for violation in audit(str(repo))["violations"]}
    assert kinds == {"notes1.md"}


def _sealed_feature(tmp_path, content="created by the wave\n"):
    """Repo whose W1 created an untracked owned file that seal promotes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "base")
    freeze(str(repo), {"W1": ["feature.py"]}, mode="brownfield")
    (repo / "feature.py").write_text(content, encoding="utf-8")
    return repo


def test_seal_adds_wave_created_files_to_the_baseline(tmp_path):
    repo = _sealed_feature(tmp_path)
    info = seal(str(repo), {"W1": ["feature.py"]})
    assert info == {"sealed": 0, "dropped": 0, "added": 1, "capped": 0}
    assert "feature.py" in runstate.load_frozen(str(repo))["files"]

    # wave 2, different owners, no exemption: clean instead of added_unowned
    assert audit(str(repo), untracked_baseline=[])["ok"] is True


def test_sealed_files_are_hash_protected_immediately(tmp_path):
    repo = _sealed_feature(tmp_path, content="v1\n")
    seal(str(repo), {"W1": ["feature.py"]})

    (repo / "feature.py").write_text("v2\n", encoding="utf-8")
    kinds = {(v["kind"], v["path"]) for v in audit(str(repo))["violations"]}
    assert ("modified_frozen", "feature.py") in kinds     # the owner map was popped

    (repo / "feature.py").unlink()
    kinds = {(v["kind"], v["path"]) for v in audit(str(repo))["violations"]}
    assert ("deleted_frozen", "feature.py") in kinds


def test_seal_does_not_absorb_ignored_unowned_or_exempt_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "base")
    runstate.save_run(str(repo), {"untracked_baseline": ["notes.md"]})
    freeze(str(repo), {"W1": ["dist/out.js", "notes.md", "feature.py"]},
           mode="brownfield")
    (repo / "dist").mkdir()
    (repo / "dist" / "out.js").write_text("ignored", encoding="utf-8")
    (repo / "notes.md").write_text("operator note", encoding="utf-8")
    (repo / "feature.py").write_text("owned new file", encoding="utf-8")
    (repo / "stray.txt").write_text("unowned", encoding="utf-8")

    info = seal(str(repo), {"W1": ["dist/out.js", "notes.md", "feature.py"]})
    assert info["added"] == 1                      # only feature.py
    files = runstate.load_frozen(str(repo))["files"]
    assert "feature.py" in files
    assert "dist/out.js" not in files
    assert "notes.md" not in files
    assert "stray.txt" not in files


def test_carry_over_refreshes_and_carries_sealed_entries(tmp_path):
    repo = _sealed_feature(tmp_path, content="v1\n")
    seal(str(repo), {"W1": ["feature.py"]})

    refreshed = freeze(str(repo), {"W2": ["feature.py"]}, mode="brownfield",
                       carry_over=True)
    assert refreshed["refreshed"] >= 1             # owned now: re-read
    carried = freeze(str(repo), {}, mode="brownfield", carry_over=True)
    assert carried["carried"] >= 1                 # unowned now: carried

    (repo / "feature.py").unlink()
    freeze(str(repo), {}, mode="brownfield", carry_over=True)
    kinds = {(v["kind"], v["path"]) for v in audit(str(repo))["violations"]}
    assert ("deleted_frozen", "feature.py") in kinds


def test_manual_freeze_keeps_sealed_entries_but_not_strays(tmp_path):
    repo = _sealed_feature(tmp_path, content="v1\n")
    seal(str(repo), {"W1": ["feature.py"]})
    (repo / "stray.txt").write_text("stray", encoding="utf-8")

    freeze(str(repo), {}, mode="brownfield")       # the manual re-baseline path

    files = runstate.load_frozen(str(repo))["files"]
    assert "feature.py" in files
    assert "stray.txt" not in files
    kinds = {(v["kind"], v["path"]) for v in audit(str(repo))["violations"]}
    assert ("added_unowned", "stray.txt") in kinds


def test_full_bake_ignores_a_greenfield_previous_baseline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "base")
    runstate.save_frozen(str(repo), {"mode": "greenfield",
                                     "files": {"ghost.py": "deadbeef"},
                                     "ignores": [], "owners": {}})

    freeze(str(repo), {}, mode="brownfield")

    files = runstate.load_frozen(str(repo))["files"]
    assert "ghost.py" not in files                 # a walk snapshot is never unioned
    assert "tracked.py" in files


def test_seal_cap_leaves_the_overflow_unprotected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "base")
    owned = ["a.py", "b.py", "c.py"]
    freeze(str(repo), {"W1": owned}, mode="brownfield")
    for rel in owned:
        (repo / rel).write_text(f"{rel} content\n", encoding="utf-8")

    info = seal(str(repo), {"W1": owned}, cap=2)
    assert info["added"] == 2
    assert info["capped"] == 1

    kinds = {(v["kind"], v["path"]) for v in audit(str(repo))["violations"]}
    assert ("added_unowned", "c.py") in kinds      # the overflow keeps violating


def test_rename_of_a_sealed_file_double_reports(tmp_path):
    repo = _sealed_feature(tmp_path, content="v1\n")
    seal(str(repo), {"W1": ["feature.py"]})

    (repo / "feature.py").rename(repo / "renamed.py")

    kinds = {(v["kind"], v["path"]) for v in audit(str(repo))["violations"]}
    assert ("deleted_frozen", "feature.py") in kinds
    assert ("added_unowned", "renamed.py") in kinds
