"""Regression gate tests: summary parsing, fingerprints, inventory, comparison logic."""

import sys

from swarmflow.regression import (compare, parse_failures, parse_fingerprints,
                                  parse_report, run_regression)


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
    assert result["tests_ran"] == 3
    assert result["family"] == "pytest"
    assert (tmp_path / "ev.txt").exists()


def test_run_regression_red_counts_failures(tmp_path):
    command = _script(tmp_path, "red.py",
                      "print('2 failed, 3 passed')\nraise SystemExit(1)\n")
    result = run_regression(str(tmp_path), command, timeout_s=30)
    assert result["ok"] is False
    assert result["rc"] == 1
    assert result["failures"] == 2
    assert result["tests_ran"] == 5


def test_run_regression_timeout(tmp_path):
    command = _script(tmp_path, "slow.py", "import time; time.sleep(5)\n")
    result = run_regression(str(tmp_path), command, timeout_s=1)
    assert result["timeout"] is True
    assert result["ok"] is False


def test_run_regression_timeout_kills_the_tree(tmp_path):
    import os
    import time

    from swarmflow import procs

    body = ("import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "time.sleep(60)\n")
    command = _script(tmp_path, "spawner.py", body)
    result = run_regression(str(tmp_path), command, timeout_s=3)
    assert result["timeout"] is True
    time.sleep(1)
    survivors = [pid for pid, entry in procs.snapshot().items()
                 if "time.sleep(60)" in entry["cmdline"] and pid != os.getpid()]
    assert survivors == []
    assert procs.is_alive(os.getpid())


def test_run_regression_rc5_gets_note(tmp_path):
    command = _script(tmp_path, "none.py", "raise SystemExit(5)\n")
    result = run_regression(str(tmp_path), command, timeout_s=30)
    assert result["rc"] == 5
    assert "no tests collected" in result.get("note", "")


def test_run_regression_parses_before_output_truncation(tmp_path):
    body = ("print('x' * 30000)\n"
            "print('FAILED tests/test_late.py::test_late - AssertionError')\n"
            "print('1 failed, 2 passed in 0.4s')\n"
            "raise SystemExit(1)\n")
    command = _script(tmp_path, "long.py", body)
    result = run_regression(str(tmp_path), command, timeout_s=30)
    assert result["failures"] == 1
    assert result["fingerprints"] == ["tests/test_late.py::test_late"]
    assert len(result["output"]) <= 20000


# ----------------------------------------------------------------- fingerprints

def test_pytest_fingerprints_drop_assertion_message():
    first = parse_fingerprints("FAILED tests/test_x.py::test_y - AssertionError: boom\n")
    second = parse_fingerprints("FAILED tests/test_x.py::test_y - AssertionError: bam\n")
    assert first == second == ["tests/test_x.py::test_y"]


def test_pytest_collection_error_fingerprint():
    output = ("ERROR tests/test_bad.py - ImportError: cannot import name 'x'\n"
              "1 error in 0.12s\n")
    report = parse_report(output)
    assert report["family"] == "pytest"
    assert report["fingerprints"] == ["tests/test_bad.py"]
    assert report["tests_ran"] == 1


def test_pytest_inventory_excludes_deselected_and_warnings():
    report = parse_report("1 failed, 2 passed, 1 skipped, 3 deselected, "
                          "1 warning in 1.20s\n")
    assert report["tests_ran"] == 3
    assert report["skipped"] == 1


def test_jest_fingerprints_ansi_and_bullet_filtering():
    output = ("\x1b[1mFAIL\x1b[22m src/a.test.ts\n"
              "  \x1b[1m●\x1b[22m Console\n\n    console.log stuff\n\n"
              "  \x1b[1m●\x1b[22m Validation Warning:\n\n"
              "  \x1b[1m●\x1b[22m Widget › renders the thing\n\n"
              "  \x1b[1m●\x1b[22m Test suite failed to run\n\n"
              "Tests:       1 failed, 2 passed, 3 total\n")
    report = parse_report(output)
    assert report["family"] == "jest"
    assert set(report["fingerprints"]) == {"src/a.test.ts", "Widget › renders the thing"}
    assert report["tests_ran"] == 3
    assert report["failures"] == 1


def test_jest_multi_project_summaries_are_summed():
    output = ("Tests:       0 failed, 2 passed, 2 total\n"
              "Tests:       1 skipped, 1 passed, 2 total\n")
    report = parse_report(output)
    assert report["tests_ran"] == 3
    assert report["skipped"] == 1


def test_typescript_errors_inside_jest_output():
    output = ("FAIL src/broken.test.ts\n"
              "  ● Test suite failed to run\n\n"
              "    src/broken.ts(12,5): error TS2322: Type 'string' is not assignable\n\n"
              "Tests:       1 failed, 1 total\n")
    report = parse_report(output)
    assert "TS2322 src/broken.ts" in report["fingerprints"]
    assert "src/broken.test.ts" in report["fingerprints"]


def test_compiler_families():
    assert parse_fingerprints("src/x.ts(3,1): error TS1005: ';' expected\n") \
        == ["TS1005 src/x.ts"]
    assert parse_fingerprints("app/main.py:12: error: Incompatible return value  "
                              "[return-value]\n") == ["return-value app/main.py"]
    assert parse_fingerprints("src/x.js: line 4, col 2, Error - Missing semicolon "
                              "(semi)\n") == ["semi src/x.js"]


def test_go_fingerprints_and_inventory():
    output = ("--- FAIL: TestAlpha (0.00s)\n"
              "    --- FAIL: TestAlpha/sub (0.00s)\n"
              "ok  \texample.com/pkg\t0.031s\n"
              "FAIL\texample.com/broken\t[build failed]\n")
    report = parse_report(output)
    assert "Test TestAlpha" in report["fingerprints"]
    assert "Test TestAlpha/sub" in report["fingerprints"]
    assert "build-failed: example.com/broken" in report["fingerprints"]
    assert report["family"] == "go"
    assert report["tests_ran"] == 2
    assert report["unit"] == "packages"


def test_cargo_fingerprints_and_ignored_is_not_executed():
    output = ("---- test_one stdout ----\nthread 'test_one' panicked at src/lib.rs:1\n\n"
              "failures:\n    test_one\n    test_two\n\ntest result: FAILED. "
              "1 passed; 2 failed; 1 ignored; 0 measured; 0 filtered out\n")
    report = parse_report(output)
    assert "test_one" in report["fingerprints"]
    assert "test_two" in report["fingerprints"]
    assert report["family"] == "cargo"
    assert report["tests_ran"] == 3
    assert report["skipped"] == 1


def test_unknown_family_has_no_inventory(tmp_path):
    report = parse_report("everything is fine\n")
    assert report["family"] == "unknown"
    assert report["tests_ran"] is None
    assert report["fingerprints"] == []


def test_errors_distinguish_run_level_failures():
    pytest_report = parse_report("ERROR tests/test_bad.py - ImportError: nope\n"
                                 "1 error in 0.10s\n")
    assert pytest_report["errors"] == ["tests/test_bad.py"]
    assert "tests/test_bad.py" in pytest_report["fingerprints"]

    jest_suite = parse_report("FAIL src/broken.test.ts\n"
                              "  ● Test suite failed to run\n\n"
                              "    Cannot find module 'x'\n\n"
                              "Tests:       1 failed, 1 total\n")
    assert jest_suite["errors"] == ["src/broken.test.ts"]

    jest_assertion = parse_report("FAIL src/fails.test.ts\n"
                                  "  ● Widget › renders\n\n"
                                  "Tests:       1 failed, 1 total\n")
    assert jest_assertion["errors"] == []
    assert "Widget › renders" in jest_assertion["fingerprints"]


def test_vitest_output_is_parsed():
    output = (" RUN  v4.1.10 C:/repo/wt-wave1\n\n"
              " ❯ src/machines/historyMachine.test.ts (8 tests | 4 failed) 19ms\n"
              "     × broadcasts with version 1 and the entry 9ms\n"
              "     ✓ behaves identically to the original\n\n"
              " Test Files  1 failed (1)\n"
              "      Tests  4 failed | 4 passed (8)\n"
              "   Duration  35.14s\n\n"
              " Failed Tests 4 \n"
              " FAIL  src/machines/historyMachine.test.ts > ADD broadcasts exactly "
              "one HISTORY_SYNC message > broadcasts with version 1 and the entry\n"
              " FAIL  src/machines/historyMachine.test.ts > persistence triggers > "
              "remote merge triggers persistence\n")
    report = parse_report(output)
    assert report["family"] == "vitest"
    assert report["failures"] == 4
    assert report["tests_ran"] == 8
    fingerprints = report["fingerprints"]
    assert ("src/machines/historyMachine.test.ts > ADD broadcasts exactly one "
            "HISTORY_SYNC message > broadcasts with version 1 and the entry"
            in fingerprints)
    assert report["errors"] == []


def test_vitest_load_failure_is_an_error():
    output = (" Test Files  1 failed (1)\n"
              "      Tests  no tests\n\n"
              " FAIL  src/newmod.test.ts [ src/newmod.test.ts ]\n")
    report = parse_report(output)
    assert report["family"] == "vitest"
    assert report["errors"] == ["src/newmod.test.ts"]


def test_node_test_tap_parsing():
    output = ("TAP version 13\n"
              "# Subtest: accepts a positive integer\n"
              "ok 1 - accepts a positive integer\n"
              "  ---\n  duration_ms: 0.9\n  type: 'test'\n  ...\n"
              "# Subtest: rejects zero\n"
              "not ok 2 - rejects zero\n"
              "  ---\n  duration_ms: 0.2\n  ...\n"
              "# Subtest: nested group\n"
              "    not ok 3 - nested child # TODO\n"
              "# Subtest: skipped one\n"
              "ok 4 - skipped one # SKIP\n"
              "1..4\n# tests 4\n# pass 2\n# fail 2\n# cancelled 1\n# skipped 1\n# todo 1\n")
    report = parse_report(output)
    assert report["family"] == "node_test"
    assert report["failures"] == 2
    assert report["tests_ran"] == 4                 # pass 2 + fail 2
    assert report["skipped"] == 3                   # skipped 1 + todo 1 + cancelled 1
    assert report["fingerprints"] == ["nested child", "rejects zero"]


def test_node_test_matches_the_captured_real_output():
    output = ("\n> prompt-scaler@1.0.0 test\n> tsx --test test/cli-options.test.ts\n\n"
              "TAP version 13\n"
              "# Subtest: accepts a positive integer\nok 1 - accepts a positive integer\n"
              "  ---\n  duration_ms: 0.9577\n  type: 'test'\n  ...\n"
              "# Subtest: two-sided bounds: below min throws with correct message\n"
              "not ok 21 - two-sided bounds: below min throws with correct message\n"
              "  ---\n  duration_ms: 0.1\n  ...\n"
              "1..22\n# tests 22\n# suites 0\n# pass 18\n# fail 4\n# cancelled 0\n"
              "# skipped 0\n# todo 0\n# duration_ms 218.939\n")
    report = parse_report(output)
    assert report["family"] == "node_test"
    assert report["failures"] == 4
    assert report["tests_ran"] == 22
    assert "two-sided bounds: below min throws with correct message" \
        in report["fingerprints"]


def test_node_test_spec_reporter_parsing():
    output = ("ℹ tests 22\nℹ suites 0\nℹ pass 18\nℹ fail 4\nℹ cancelled 0\n"
              "ℹ skipped 0\nℹ todo 0\n"
              "✖ two-sided bounds: above max throws with correct message (0.2859ms)\n"
              "✖ rejects zero (0.2ms)\n")
    report = parse_report(output)
    assert report["family"] == "node_test"
    assert report["failures"] == 4
    assert report["tests_ran"] == 22
    assert report["fingerprints"] == ["rejects zero",
                                      "two-sided bounds: above max throws with correct message"]


def test_node_test_file_level_failure_is_path_shaped():
    output = "TAP version 13\nnot ok 1 - /tmp/x/file.test.ts\n# tests 1\n# pass 0\n# fail 1\n"
    report = parse_report(output)
    assert report["failures"] == 1
    assert report["tests_ran"] == 1
    assert report["fingerprints"] == ["/tmp/x/file.test.ts"]


def test_run_regression_passes_env_overrides(tmp_path):
    command = _script(tmp_path, "env.py",
                      "import os\nprint('PYTHONPATH=' + os.environ.get('PYTHONPATH', ''))\n")
    result = run_regression(str(tmp_path), command, timeout_s=30,
                            env={"PYTHONPATH": "/custom/path"})
    assert result["rc"] == 0
    assert "PYTHONPATH=/custom/path" in result["output"]


# ------------------------------------------------------------------ comparison

def test_compare_green_to_red_is_regression():
    comparison = compare({"rc": 0, "failures": None}, {"rc": 1, "failures": 2,
                                                       "timeout": False})
    assert comparison["regressed"] is True
    assert any("exit code" in reason for reason in comparison["reasons"])


def test_compare_baseline_red_same_count_is_indeterminate_and_fails_closed():
    baseline = {"rc": 1, "failures": 2}
    current = {"rc": 1, "failures": 2, "timeout": False}
    strict = compare(baseline, current)
    assert strict["indeterminate"] is True
    assert strict["regressed"] is True
    assert any("identity not available" in reason for reason in strict["reasons"])

    lax = compare(baseline, current, strict=False)
    assert lax["indeterminate"] is True
    assert lax["regressed"] is False
    assert any(reason.startswith("warning:") for reason in lax["reasons"])


def test_compare_failure_count_increase_is_regression():
    comparison = compare({"rc": 1, "failures": 1}, {"rc": 1, "failures": 3,
                                                    "timeout": False})
    assert comparison["regressed"] is True
    assert any("failures" in reason for reason in comparison["reasons"])


def test_compare_same_failing_set_is_clean_with_fixed_reported():
    baseline = {"rc": 1, "failures": 1, "fingerprints": ["a", "b"],
                "tests_ran": 10}
    current = {"rc": 1, "failures": 1, "fingerprints": ["b"], "tests_ran": 10}
    comparison = compare(baseline, current)
    assert comparison["regressed"] is False
    assert comparison["new_failures"] == []
    assert comparison["fixed_failures"] == ["a"]


def test_compare_different_failing_test_equal_counts_is_regression():
    # the xstate/TaskDock case: 1 -> 1 failures, different identity
    baseline = {"rc": 1, "failures": 1, "fingerprints": ["old"], "tests_ran": 10}
    current = {"rc": 1, "failures": 1, "fingerprints": ["new"], "tests_ran": 10}
    comparison = compare(baseline, current)
    assert comparison["regressed"] is True
    assert comparison["new_failures"] == ["new"]
    assert comparison["fixed_failures"] == ["old"]


def test_compare_suite_shrink_green_to_green_is_regression():
    baseline = {"rc": 0, "failures": None, "tests_ran": 13, "unit": "tests"}
    current = {"rc": 0, "failures": None, "tests_ran": 5, "unit": "tests"}
    comparison = compare(baseline, current)
    assert comparison["regressed"] is True
    assert comparison["suite_delta"] == -8
    assert any("shrank 13 -> 5" in reason for reason in comparison["reasons"])


def test_compare_suite_shrink_within_tolerance_is_clean():
    baseline = {"rc": 0, "tests_ran": 13}
    current = {"rc": 0, "tests_ran": 11}
    assert compare(baseline, current, shrink_tolerance=3)["regressed"] is False
    assert compare(baseline, current)["regressed"] is True


def test_compare_suite_growth_is_clean():
    comparison = compare({"rc": 0, "tests_ran": 5}, {"rc": 0, "tests_ran": 13})
    assert comparison["regressed"] is False
    assert comparison["suite_delta"] == 8


def test_compare_timeout_is_regression():
    comparison = compare({"rc": 0, "tests_ran": 5},
                         {"rc": None, "timeout": True, "tests_ran": 2})
    assert comparison["regressed"] is True
    assert comparison["indeterminate"] is True
    assert any("timed out" in reason for reason in comparison["reasons"])


def test_compare_baseline_red_current_green_is_improvement():
    comparison = compare({"rc": 1, "failures": 1, "fingerprints": ["a"]},
                         {"rc": 0, "failures": None, "fingerprints": []})
    assert comparison["regressed"] is False
    assert any("green" in reason for reason in comparison["reasons"])


def test_compare_without_baseline_is_not_regression():
    comparison = compare(None, {"rc": 0, "failures": None, "timeout": False})
    assert comparison["regressed"] is False
