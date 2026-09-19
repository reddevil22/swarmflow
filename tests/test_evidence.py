"""Evidence bundle tests: sections, report extraction, sensitive-path diffs."""

import json
import subprocess

import pytest

from swarmflow.evidence import bundle, write_bundle
from swarmflow.ledger import Ledger


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


GIT = _git_available()


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
    (repo / ".swarmflow" / "run.json").write_text(json.dumps({
        "mode": "brownfield", "branch": "swarmflow/demo", "base_sha": head,
        "regression": {"baseline": {"rc": 0, "failures": None,
                                    "command": "python -m pytest -q"}},
    }), encoding="utf-8")
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

    # a change on a sensitive path (test file)
    (repo / "tests" / "test_app.py").write_text("def test_ok():\n    assert 1\n",
                                                encoding="utf-8")

    ledger = Ledger(str(tmp_path / "ledger.db"))
    text = bundle(str(repo), ledger)
    ledger.close()

    for marker in ["# Evidence bundle", "## Recon digest", "## Tasks", "T1",
                   "acceptance: the probe passes",
                   "## Worker reports", "REPORT CHANGES", "## Audit", "## Regression",
                   "python -m pytest -q", "FINAL-TAIL-MARKER", "## Git diff vs base",
                   "### Sensitive paths diff", "test_app.py"]:
        assert marker in text, f"missing: {marker}"

    ledger = Ledger(str(tmp_path / "ledger.db"))
    path = write_bundle(str(repo), ledger)
    ledger.close()
    assert path.exists() and path.read_text(encoding="utf-8").startswith("# Evidence")
