"""Regression gate tests: summary parsing, runner behavior, comparison logic."""

import sys

from swarmflow.regression import compare, parse_failures, run_regression


def _script(tmp_path, name, body):
    (tmp_path / name).write_text(body, encoding="utf-8")
    return f'"{sys.executable}" {name}'


def test_parse_failures_common_summaries():
    assert parse_failures("=== 1 failed, 12 passed in 0.5s ===") == 1
    assert parse_failures("Tests:       2 failed, 3 passed, 5 total") == 2
    assert parse_failures("test result: FAILED. 3 passed; 2 failed; 0 ignored") == 2
    assert parse_failures("--- FAIL: TestX (0.00s)\n--- FAIL: TestY (0.01s)") == 2
    assert parse_failures("all good") is None


def test_run_regression_green(tmp_path):
    command = _script(tmp_path, "ok.py", "print('3 passed')\n")
    result = run_regression(str(tmp_path), command, timeout_s=30,
                            evidence_path=str(tmp_path / "ev.txt"))
    assert result["ok"] is True
    assert result["rc"] == 0
    assert result["failures"] is None
    assert (tmp_path / "ev.txt").exists()


def test_run_regression_red_counts_failures(tmp_path):
    command = _script(tmp_path, "red.py",
                      "print('2 failed, 3 passed')\nraise SystemExit(1)\n")
    result = run_regression(str(tmp_path), command, timeout_s=30)
    assert result["ok"] is False
    assert result["rc"] == 1
    assert result["failures"] == 2


def test_run_regression_timeout(tmp_path):
    command = _script(tmp_path, "slow.py", "import time; time.sleep(5)\n")
    result = run_regression(str(tmp_path), command, timeout_s=1)
    assert result["timeout"] is True
    assert result["ok"] is False


def test_run_regression_rc5_gets_note(tmp_path):
    command = _script(tmp_path, "none.py", "raise SystemExit(5)\n")
    result = run_regression(str(tmp_path), command, timeout_s=30)
    assert result["rc"] == 5
    assert "no tests collected" in result.get("note", "")


def test_compare_green_to_red_is_regression():
    comparison = compare({"rc": 0, "failures": None}, {"rc": 1, "failures": 2, "timeout": False})
    assert comparison["regressed"] is True
    assert any("exit code" in reason for reason in comparison["reasons"])


def test_compare_baseline_red_same_count_is_not_regression():
    comparison = compare({"rc": 1, "failures": 2}, {"rc": 1, "failures": 2, "timeout": False})
    assert comparison["regressed"] is False
    assert comparison["baseline_was_red"] is True


def test_compare_failure_count_increase_is_regression():
    comparison = compare({"rc": 1, "failures": 1}, {"rc": 1, "failures": 3, "timeout": False})
    assert comparison["regressed"] is True
    assert any("failures" in reason for reason in comparison["reasons"])


def test_compare_without_baseline_is_not_regression():
    comparison = compare(None, {"rc": 0, "failures": None, "timeout": False})
    assert comparison["regressed"] is False
