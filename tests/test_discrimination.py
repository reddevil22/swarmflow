"""Discrimination check: parent-state worktree runs and verdict classification."""

import shutil
import subprocess
import sys

import pytest

from swarmflow import cli
from swarmflow.discrimination import run_check
from swarmflow.ledger import Ledger

GIT = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not GIT, reason="git not available")


def _config(**overrides):
    settings = {"enabled": True, "mode": "warn", "link_dirs": [], "timeout_s": 120,
                "max_test_files": 20, "skip_patterns": []}
    settings.update(overrides)
    return {"discrimination": settings}


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _commit(repo):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def _base_repo(tmp_path):
    """A brownfield repo whose `double()` is buggy at the base commit."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "conftest.py").write_text("", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef double(x):\n    return x + x + 1\n",
        encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8")
    (repo / "tests" / "test_old.py").write_text("def test_old():\n    assert True\n",
                                                encoding="utf-8")
    return repo


def _fix_wave(repo):
    (repo / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef double(x):\n    return x * 2\n",
        encoding="utf-8")
    (repo / "tests" / "test_discriminating.py").write_text(
        "from src.calc import double\n\n\ndef test_double():\n    assert double(3) == 6\n",
        encoding="utf-8")
    (repo / "tests" / "test_vacuous.py").write_text(
        "def test_trivial():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    (repo / "tests" / "test_old.py").unlink()


def _tasks(**overrides):
    task = {"id": "T1",
            "owner_files": ["src/calc.py", "tests/test_discriminating.py",
                            "tests/test_vacuous.py", "tests/test_old.py"],
            "test_command": f'"{sys.executable}" -m pytest -q tests'}
    task.update(overrides)
    return [task]


def test_classifies_discriminating_vacuous_and_deleted(tmp_path):
    repo = _base_repo(tmp_path)
    base = _commit(repo)
    _fix_wave(repo)
    snapshots = {rel: (repo / rel).read_bytes() for rel in
                 ("src/calc.py", "tests/test_discriminating.py",
                  "tests/test_vacuous.py")}

    result = run_check(str(repo), 1, _tasks(), _config(link_dirs=["node_modules"]),
                       {"base_sha": base})

    verdicts = {entry["path"]: entry["verdict"] for entry in result["files"]}
    assert verdicts["tests/test_discriminating.py"] == "fails_at_parent"
    assert verdicts["tests/test_vacuous.py"] == "passes_at_parent"
    assert verdicts["tests/test_old.py"] == "deleted_in_wave"
    assert result["counts"]["fails_at_parent"] == 1
    assert result["counts"]["passes_at_parent"] == 1
    assert result["counts"]["deleted_in_wave"] == 1
    assert result["indeterminate"] is False
    assert result["red_parent"] is False
    assert result["base_sha_short"] == base[:10]
    assert len(result["copied"]) == 2   # the two test files; sources stay at the parent

    for rel, data in snapshots.items():
        assert (repo / rel).read_bytes() == data
    listing = subprocess.run(["git", "worktree", "list"], cwd=repo,
                             capture_output=True, text=True).stdout
    assert len([line for line in listing.splitlines() if line.strip()]) == 1
    assert not (repo / ".swarmflow" / "discrimination" / "wt-wave1").exists()


def test_preexisting_failure_is_not_discrimination(tmp_path):
    repo = _base_repo(tmp_path)
    (repo / "tests" / "test_broken.py").write_text(
        "def test_broken():\n    assert False\n", encoding="utf-8")
    base = _commit(repo)
    (repo / "tests" / "test_vacuous.py").write_text(
        "def test_trivial():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    tasks = _tasks(owner_files=["tests/test_broken.py", "tests/test_vacuous.py"])

    result = run_check(str(repo), 2, tasks, _config(),
                       {"base_sha": base,
                        "regression": {"baseline": {
                            "rc": 1, "fingerprints": ["tests/test_broken.py::test_broken"]}}})

    verdicts = {entry["path"]: entry["verdict"] for entry in result["files"]}
    assert verdicts["tests/test_broken.py"] == "preexisting_at_parent"
    assert verdicts["tests/test_vacuous.py"] == "passes_at_parent"
    assert result["counts"]["fails_at_parent"] == 0
    assert result["red_parent"] is True


def test_new_module_test_is_error_at_parent(tmp_path):
    repo = _base_repo(tmp_path)
    base = _commit(repo)
    (repo / "src" / "newmod.py").write_text("def thing():\n    return 1\n",
                                            encoding="utf-8")
    (repo / "tests" / "test_newmod.py").write_text(
        "from src.newmod import thing\n\n\ndef test_thing():\n    assert thing() == 1\n",
        encoding="utf-8")
    tasks = _tasks(owner_files=["src/newmod.py", "tests/test_newmod.py"])

    result = run_check(str(repo), 1, tasks, _config(), {"base_sha": base})

    verdicts = {entry["path"]: entry["verdict"] for entry in result["files"]}
    assert verdicts["tests/test_newmod.py"] == "error_at_parent"
    assert result["counts"]["fails_at_parent"] == 0


def test_skip_patterns_exclude_files(tmp_path):
    repo = _base_repo(tmp_path)
    base = _commit(repo)
    (repo / "tests" / "test_vacuous.py").write_text(
        "def test_trivial():\n    assert 1 + 1 == 2\n", encoding="utf-8")

    result = run_check(str(repo), 1,
                       _tasks(owner_files=["tests/test_vacuous.py"]),
                       _config(skip_patterns=["tests/test_vacuous.py"]),
                       {"base_sha": base})

    assert result.get("skipped") == "no owned test-shaped files"


def test_unlaunchable_command_is_indeterminate(tmp_path):
    repo = _base_repo(tmp_path)
    base = _commit(repo)
    (repo / "tests" / "test_vacuous.py").write_text(
        "def test_trivial():\n    assert 1 + 1 == 2\n", encoding="utf-8")

    result = run_check(str(repo), 1, _tasks(
        owner_files=["tests/test_vacuous.py"],
        test_command="swarmflow-no-such-tool --run"), _config(), {"base_sha": base})

    assert result["indeterminate"] is True
    assert result["files"][0]["verdict"] == "not_observed"
    assert not (repo / ".swarmflow" / "discrimination" / "wt-wave1").exists()


def test_skips_without_base_sha_or_settings(tmp_path):
    repo = _base_repo(tmp_path)
    base = _commit(repo)
    (repo / "tests" / "test_vacuous.py").write_text(
        "def test_trivial():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    tasks = _tasks(owner_files=["tests/test_vacuous.py"])

    assert run_check(str(repo), 1, tasks, _config(), {})["skipped"].startswith("no base_sha")
    assert run_check(str(repo), 1, tasks, _config(enabled=False),
                     {"base_sha": base})["skipped"] == "disabled in config"
    assert run_check(str(repo), 1, _tasks(owner_files=["src/calc.py"]), _config(),
                     {"base_sha": base})["skipped"] == "no owned test-shaped files"


TAP_RUNNER = ("import sys\n"
              "print('TAP version 13')\n"
              "print('not ok 1 - bounds rejects zero')\n"
              "print('# tests 2')\n"
              "print('# pass 1')\n"
              "print('# fail 1')\n"
              "sys.exit(1)\n")


def _tap_repo(tmp_path, files):
    """Repo at base + a TAP-printing runner standing in for the project's command."""
    repo = _base_repo(tmp_path)
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    runner = tmp_path / "tap_runner.py"
    runner.write_text(TAP_RUNNER, encoding="utf-8")
    base = _commit(repo)
    return repo, base, f'"{sys.executable}" "{runner}"'


def test_path_less_failures_are_attributed_by_name(tmp_path):
    repo, base, command = _tap_repo(tmp_path, {
        "tests/test_tap.py": "test('bounds rejects zero', () => {});\n"})
    tasks = [{"id": "T1", "owner_files": ["tests/test_tap.py"],
              "test_command": command}]

    result = run_check(str(repo), 1, tasks, _config(), {"base_sha": base})

    entry = result["files"][0]
    assert entry["verdict"] == "fails_at_parent"
    assert entry["basis"] == "name_found_in_copied_file"
    assert result["unattributable"] is False


def test_ambiguous_test_name_is_not_attributed(tmp_path):
    repo, base, command = _tap_repo(tmp_path, {
        "tests/test_one.py": "test('bounds rejects zero', () => {});\n",
        "tests/test_two.py": "test('bounds rejects zero', () => {});\n"})
    tasks = [{"id": "T1", "owner_files": ["tests/test_one.py", "tests/test_two.py"],
              "test_command": command}]

    result = run_check(str(repo), 1, tasks, _config(), {"base_sha": base})

    assert {entry["verdict"] for entry in result["files"]} == {"not_observed"}
    assert all(entry["reason"] == "unattributable_failures" for entry in result["files"])
    assert result["unattributable"] is True


def test_unparseable_red_parent_never_reports_passes_at_parent(tmp_path):
    repo, base, _ = _tap_repo(tmp_path, {"tests/test_x.py": "print('hi')\n"})
    noise = tmp_path / "noise.py"
    noise.write_text("import sys\nprint('something went wrong')\nsys.exit(1)\n",
                     encoding="utf-8")
    tasks = [{"id": "T1", "owner_files": ["tests/test_x.py"],
              "test_command": f'"{sys.executable}" "{noise}"'}]

    result = run_check(str(repo), 1, tasks, _config(), {"base_sha": base})

    assert result["counts"]["passes_at_parent"] == 0
    assert result["files"][0]["verdict"] == "not_observed"
    assert result["files"][0]["reason"] == "unattributable_failures"
    assert result["unattributable"] is True


def test_unattributable_parent_is_fail_closed_under_enforce(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.add_task("T1", str(project.resolve()), owner_files=["tests/x.test.ts"])
    canned = {"version": 1, "wave": 1, "indeterminate": False, "unattributable": True,
              "unattributable_files": ["tests/x.test.ts"], "counts": {}, "files": []}
    monkeypatch.setattr("swarmflow.discrimination.run_check", lambda *a, **k: canned)

    warn_state, _ = cli._run_discrimination(
        str(project), 1, [{"id": "T1"}], {"discrimination": {"mode": "warn"}}, ledger, {})
    enforce_state, _ = cli._run_discrimination(
        str(project), 1, [{"id": "T1"}], {"discrimination": {"mode": "enforce"}}, ledger,
        {})
    ledger.close()
    assert warn_state == "warn"
    assert enforce_state == "fail"


def test_leftover_worktree_is_cleaned(tmp_path):
    repo = _base_repo(tmp_path)
    base = _commit(repo)
    (repo / "tests" / "test_vacuous.py").write_text(
        "def test_trivial():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    leftover = repo / ".swarmflow" / "discrimination" / "wt-wave1"
    leftover.mkdir(parents=True)
    (leftover / "stray.txt").write_text("x", encoding="utf-8")

    result = run_check(str(repo), 1, _tasks(owner_files=["tests/test_vacuous.py"]),
                       _config(), {"base_sha": base})

    assert result["indeterminate"] is False
    assert "leftover" in result["reason"]
    assert not leftover.exists()
