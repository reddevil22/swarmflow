"""Evidence bundle tests: sections, report extraction, sensitive-path diffs."""

import json
import subprocess

import pytest

from swarmflow.evidence import bundle, clip, write_bundle
from swarmflow.ledger import Ledger
from swarmflow import runstate


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


GIT = _git_available()


def test_clip_keeps_head_and_tail_with_a_marker():
    text = "HEAD" + ("x" * 9000) + "TAIL"
    clipped = clip(text, head=10, tail=10)
    assert clipped.startswith("HEAD") and clipped.endswith("TAIL")
    assert "chars omitted" in clipped
    assert clip("short", head=10, tail=10) == "short"


def test_bundle_puts_verification_before_the_narratives(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.add_task("T1", str(project), wave=1, owner_files=["a.py"], acceptance=["x"])
    trace = project / ".swarmflow" / "T1_a1.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(json.dumps({"type": "message_end", "message": {
        "role": "assistant",
        "content": [{"type": "text",
                     "text": "CHANGES-MARKER" + ("x" * 11000) + "VERIFICATION-MARKER"}],
        "usage": {"output": 5}}}) + "\n", encoding="utf-8")
    ledger.set_status("T1", "delivered", worker_trace=str(trace), verdict="{}")
    evidence = project / ".swarmflow" / "evidence"
    evidence.mkdir()
    (evidence / "verify_T1.json").write_text(json.dumps({
        "ok": True, "verdict": {"task_id": "T1", "verdict": "needs_fix", "findings": [
            {"severity": "major", "summary": "only presence asserted", "evidence": "",
             "required_action": "assert equality"}], "requirement_coverage": [],
            "uncovered": []}}), encoding="utf-8")

    text = bundle(str(project), ledger)
    ledger.close()

    assert "## Per-task verification" in text
    assert "T1: needs_fix - 1 finding(s)" in text
    assert "[major] only presence asserted" in text
    assert "## Per-task verification" in text.split("## Worker reports")[0]
    assert "CHANGES-MARKER" in text and "VERIFICATION-MARKER" in text
    assert "chars omitted" in text


def test_bundle_is_scoped_to_the_project(tmp_path):
    """The ledger is shared across runs; another project's tasks must not leak in."""
    project = tmp_path / "p"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.add_task("MINE", str(project.resolve()), wave=1, owner_files=["a.py"])
    ledger.add_task("THEIRS", str(other.resolve()), wave=1, owner_files=["b.py"])

    text = bundle(str(project), ledger)
    ledger.close()

    assert "MINE" in text
    assert "THEIRS" not in text


def test_bundle_marks_unverified_tasks(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.add_task("T9", str(project), wave=1, owner_files=["a.py"])
    text = bundle(str(project), ledger)
    ledger.close()
    assert "T9: (not verified)" in text


def test_corrupt_baseline_does_not_crash_the_bundle(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    runstate.frozen_path(str(repo)).parent.mkdir(parents=True, exist_ok=True)
    runstate.frozen_path(str(repo)).write_text("{broken", encoding="utf-8")
    ledger = Ledger(str(tmp_path / "l.db"))
    text = bundle(str(repo), ledger)
    ledger.close()
    assert "corrupt_baseline" in text


def test_bundle_honours_extra_ignores(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    runstate.save_frozen(str(repo), {"mode": "greenfield", "files": {},
                                     "ignores": [], "owners": {}})
    (repo / "stray.weird").write_text("x", encoding="utf-8")
    ledger = Ledger(str(tmp_path / "l.db"))
    without = bundle(str(repo), ledger)
    with_ignores = bundle(str(repo), ledger, ignores=["*.weird"])
    ledger.close()
    assert "stray.weird" in without
    assert "stray.weird" not in with_ignores


@pytest.mark.skipif(not GIT, reason="git not available")
def test_bundle_contains_all_sections(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / ".gitignore").write_text(".swarmflow/\n", encoding="utf-8")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n",
                                                encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "baseline")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()

    (repo / ".swarmflow").mkdir()
    runstate.save_run(str(repo), {
        "mode": "brownfield", "branch": "swarmflow/demo", "base_sha": head,
        "untracked_baseline": ["docs/implementation-priorities.md"],
        "regression": {"baseline": {"rc": 0, "failures": None,
                                    "tests_ran": 3,
                                    "fingerprints": ["tests/test_app.py::test_ok"],
                                    "command": "python -m pytest -q"}},
    })
    (repo / ".swarmflow" / "recon.json").write_text(json.dumps({
        "git": {"is_git": True, "branch": "main", "head": head, "commits": 1,
                "dirty_tracked": [], "untracked_count": 0},
        "stacks": [{"stack": "python", "evidence": "pyproject.toml"}],
        "commands": {"regression": {"command": "python -m pytest -q",
                                    "evidence": "tests/ present"}},
        "conventions": [],
        "tests": {"count_sampled": 1, "files": ["tests/test_app.py"]},
        "inventory": {},
    }), encoding="utf-8")

    # a worker trace with a final report
    trace = repo / ".swarmflow" / "T1_a1.jsonl"
    with trace.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "REPORT CHANGES here"}],
            "usage": {"output": 5}}}) + "\n")
        handle.write(json.dumps({"type": "agent_end", "messages": [
            {"role": "assistant", "content": [{"type": "text",
                                               "text": "REPORT CHANGES here"}]}]}) + "\n")

    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.add_task("T1", str(repo), wave=1, owner_files=["app.py"],
                    acceptance=["the probe passes"])
    ledger.set_status("T1", "delivered", worker_trace=str(trace), verdict="{}")
    ledger.close()
    (repo / ".swarmflow" / "evidence").mkdir()
    (repo / ".swarmflow" / "evidence" / "wave1.txt").write_text(
        "$ cmd\n(rc=0)\nFINAL-TAIL-MARKER\n", encoding="utf-8")
    (repo / ".swarmflow" / "evidence" / "wave1.compare.json").write_text(json.dumps({
        "version": 1, "regressed": True, "indeterminate": False,
        "reasons": ["new failing tests: tests/test_app.py::test_x"],
        "new_failures": ["tests/test_app.py::test_x"], "fixed_failures": [],
    }), encoding="utf-8")
    (repo / ".swarmflow" / "evidence" / "wave1.discrimination.json").write_text(
        json.dumps({
            "version": 1, "wave": 1, "base_sha_short": "abc1234567",
            "red_parent": False, "indeterminate": False,
            "python_probe": {"attempted": True, "launched": True,
                             "candidates": ["pkgmod"], "escaped": []},
            "copied": [{"path": "tests/test_app.py", "sha256": "x"}],
            "counts": {"fails_at_parent": 1, "passes_at_parent": 1},
            "files": [
                {"path": "tests/test_app.py", "verdict": "fails_at_parent",
                 "evidence": ["tests/test_app.py::test_x"]},
                {"path": "tests/test_other.py", "verdict": "passes_at_parent",
                 "evidence": []},
            ],
        }), encoding="utf-8")

    (repo / ".swarmflow" / "evidence" / "wave1.sweep.json").write_text(json.dumps({
        "version": 1, "wave": 1, "mode": "warn",
        "counts": {"orphans": 1, "pre_existing": 1, "killed": 0},
        "orphans": [{"pid": 4242, "name": "node", "cmd": "node vite.js", "cwd": "",
                     "ports": [3000], "serverish": True}],
        "pre_existing": [{"pid": 99, "name": "node", "cmd": "node preview.js", "cwd": "",
                          "ports": [4173], "serverish": True}],
        "killed": [],
    }), encoding="utf-8")

    # a change on a sensitive path (test file)
    (repo / "tests" / "test_app.py").write_text("def test_ok():\n    assert 1\n",
                                                encoding="utf-8")

    ledger = Ledger(str(tmp_path / "ledger.db"))
    text = bundle(str(repo), ledger)
    ledger.close()

    markers = ["# Evidence bundle", "## Recon digest", "## Tasks", "T1",
               "acceptance: the probe passes",
               "## Per-task verification",
               "## Worker reports", "REPORT CHANGES", "## Audit", "## Regression",
               "python -m pytest -q", "FINAL-TAIL-MARKER", "## Git diff vs base",
               "### Sensitive paths diff", "test_app.py",
               "baseline failing tests (1)", "tests/test_app.py::test_ok",
               "latest comparison (wave1.compare.json)",
               "new failing test: tests/test_app.py::test_x",
               "## Discrimination", "fails_at_parent: tests/test_app.py",
               "passes_at_parent: tests/test_other.py",
               "## Processes (post-wave sweep)",
               "leaked: pid 4242 node (ports: 3000)",
               "pre-existing listener (untouched): pid 99"]
    missing = [marker for marker in markers if marker not in text]
    assert not missing, f"missing markers: {missing}"

    ledger = Ledger(str(tmp_path / "ledger.db"))
    path = write_bundle(str(repo), ledger)
    ledger.close()
    assert path.exists() and path.read_text(encoding="utf-8").startswith("# Evidence")
