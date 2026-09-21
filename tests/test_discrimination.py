"""Discrimination check: parent-state worktree runs and verdict classification."""

import os
import shutil
import subprocess
import sys

import pytest

from swarmflow import cli, pipeline
from swarmflow.discrimination import (_probe_interpreter, _probe_packages,
                                      _python_prepare, run_check)
from swarmflow.ledger import Ledger
from swarmflow.regression import run_regression

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


def test_launch_failure_detection_covers_shell_wording_and_codes():
    from swarmflow.discrimination import _launch_failed

    assert _launch_failed({"rc": 127, "output": "sh: 1: nosuch: not found"}) is True
    assert _launch_failed({"rc": 9009, "output": ""}) is True
    assert _launch_failed({"rc": 1, "output": "command not found"}) is True
    assert _launch_failed({"rc": None, "timeout": False, "output": ""}) is True
    assert _launch_failed({"rc": 1, "output": "2 failed, 5 passed"}) is False
    assert _launch_failed({"rc": 0, "output": "all good"}) is False


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

    warn_state, _ = pipeline.run_discrimination(
        str(project), 1, [{"id": "T1"}], {"discrimination": {"mode": "warn"}}, ledger, {})
    enforce_state, _ = pipeline.run_discrimination(
        str(project), 1, [{"id": "T1"}], {"discrimination": {"mode": "enforce"}}, ledger,
        {})
    ledger.close()
    assert warn_state == "warn"
    assert enforce_state == "fail"


def _python_fixture(tmp_path, wt_value: int, live_value: int):
    """Project + worktree-shaped dirs with the same package at different contents."""
    project = tmp_path / "proj"
    (project / "src" / "pkgmod").mkdir(parents=True)
    (project / "src" / "pkgmod" / "__init__.py").write_text(f"VALUE = {live_value}\n",
                                                            encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname='pkgmod'\n", encoding="utf-8")
    wt = tmp_path / "wt"
    (wt / "src" / "pkgmod").mkdir(parents=True)
    (wt / "src" / "pkgmod" / "__init__.py").write_text(f"VALUE = {wt_value}\n",
                                                       encoding="utf-8")
    return project, wt


def test_pythonpath_prepend_beats_a_live_import_path(tmp_path, monkeypatch):
    """The reproduced false negative: without the prepend the live package wins."""
    project, wt = _python_fixture(tmp_path, wt_value=1, live_value=2)
    check = wt / "check.py"
    check.write_text("import pkgmod\nassert pkgmod.VALUE == 2\n", encoding="utf-8")
    command = f'{sys.executable} check.py'
    monkeypatch.setenv("PYTHONPATH", str(project / "src"))

    without = run_regression(str(wt), command, timeout_s=30)
    assert without["rc"] == 0                    # the live (new) code was imported

    prepended = os.pathsep.join([str(wt / "src"), str(project / "src")])
    with_prepend = run_regression(str(wt), command, timeout_s=30,
                                  env={"PYTHONPATH": prepended})
    assert with_prepend["rc"] != 0               # the worktree (parent) code wins


def test_python_prepare_prepends_existing_roots_only(tmp_path):
    project, wt = _python_fixture(tmp_path, wt_value=1, live_value=1)
    env, names, prepend = _python_prepare(project, wt, {})
    assert env and env["PYTHONPATH"].split(os.pathsep)[0] == str(wt / "src")
    assert names == ["pkgmod"]
    assert prepend == [str(wt / "src")]


def test_python_prepare_skips_non_python_projects(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    assert _python_prepare(project, wt, {}) == (None, [], [])


def test_probe_interpreter_follows_the_test_command(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    assert _probe_interpreter(project, "poetry run pytest -q") == "poetry run python"
    assert _probe_interpreter(project, "uv run pytest") == "uv run python"
    venv_python = project / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    assert str(venv_python) in _probe_interpreter(project, "python -m pytest -q")


def test_probe_catches_a_meta_path_shadow(tmp_path, monkeypatch):
    """A front-inserted finder bypasses PYTHONPATH; the probe must still catch it."""
    project, wt = _python_fixture(tmp_path, wt_value=1, live_value=2)
    hook_dir = tmp_path / "hook"
    hook_dir.mkdir()
    init = project / "src" / "pkgmod" / "__init__.py"
    (hook_dir / "sitecustomize.py").write_text(
        "import importlib.util, sys\n"
        "class F:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'pkgmod':\n"
        "            return importlib.util.spec_from_file_location('pkgmod', r'%s')\n"
        "        return None\n"
        "sys.meta_path.insert(0, F())\n" % init,
        encoding="utf-8")
    env = {"PYTHONPATH": os.pathsep.join([str(wt / "src"), str(hook_dir)])}

    probe = _probe_packages(project, wt, ["pkgmod"], "python -m pytest -q", env, 60)

    assert probe["launched"] is True
    assert probe["escaped"] and probe["escaped"][0]["name"] == "pkgmod"


def test_run_check_marks_escaped_imports_indeterminate(tmp_path, monkeypatch):
    repo, base, command = _tap_repo(tmp_path, {
        "tests/test_tap.py": "test('bounds rejects zero', () => {});\n",
        "src/pkgmod/__init__.py": "VALUE = 1\n",
        "pyproject.toml": "[project]\nname='pkgmod'\n"})
    monkeypatch.setattr("swarmflow.discrimination._probe_packages", lambda *a, **k: {
        "attempted": True, "launched": True, "candidates": ["pkgmod"],
        "escaped": [{"name": "pkgmod", "origin": str(repo / "src" / "pkgmod")}]})

    result = run_check(str(repo), 1, [{"id": "T1", "owner_files": ["tests/test_tap.py"],
                                       "test_command": command}],
                       _config(), {"base_sha": base})

    assert result["indeterminate"] is True
    assert "imports from outside the worktree" in result["reason"]
    assert result["python_probe"]["escaped"]


def test_run_check_marks_an_unlaunchable_probe_indeterminate(tmp_path, monkeypatch):
    repo, base, command = _tap_repo(tmp_path, {
        "tests/test_tap.py": "test('bounds rejects zero', () => {});\n",
        "src/pkgmod/__init__.py": "VALUE = 1\n",
        "pyproject.toml": "[project]\nname='pkgmod'\n"})
    monkeypatch.setattr("swarmflow.discrimination._probe_packages", lambda *a, **k: {
        "attempted": True, "launched": False, "candidates": ["pkgmod"], "escaped": []})

    result = run_check(str(repo), 1, [{"id": "T1", "owner_files": ["tests/test_tap.py"],
                                       "test_command": command}],
                       _config(), {"base_sha": base})

    assert result["indeterminate"] is True
    assert "probe could not run" in result["reason"]


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
