"""Project regression gate.

Runs the project's configured suite (usually recon-detected, overridable) and compares
against a recorded baseline. This is the brownfield backbone: a feature is only
"delivered" if nothing that worked before broke.

Comparison is by failure identity, not counts: exit codes and failure counts cannot see
a different test breaking at equal counts (red -> red), nor a suite quietly shrinking.
When identity cannot be compared the gate says so and - under the default strict mode -
fails closed.
"""

import os
import re
import subprocess
import time
from pathlib import Path

from .procs import kill_tree, spawn_flags

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
MAX_FINGERPRINT = 200


def _sanitize(output: str) -> str:
    """Strip ANSI/OSC escape sequences and carriage returns before parsing."""
    return ANSI_RE.sub("", output).replace("\r", "")


def parse_failures(output: str) -> int | None:
    """Best-effort failure count from common test-runner summaries. None if unknown."""
    output = _sanitize(output)
    match = re.search(r"Tests:\s+.*?(\d+) failed", output)              # jest
    if match:
        return int(match.group(1))
    match = re.search(r"test result: FAILED\.\s+(\d+) passed;\s+(\d+) failed", output)
    if match:                                                          # cargo
        return int(match.group(2))
    match = NODE_FAILS.search(output)                                  # node:test (TAP/spec)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+) failed", output)                         # pytest
    if match:
        return int(match.group(1))
    go_failures = len(re.findall(r"^--- FAIL", output, re.MULTILINE))  # go
    if go_failures:
        return go_failures
    return None


# --------------------------------------------------------------- fingerprints
# Fingerprint extractors run as a union over the whole output: a jest run embeds
# ts-jest typecheck errors, and a project's suite may not be a single runner. Each
# pattern is chosen so it cannot match another family's output.

PYTEST_FAIL_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(.+?)(?:\s+-\s+.*)?$", re.MULTILINE)
PYTEST_ERROR_LINE = re.compile(r"^ERROR\s+(.+?)(?:\s+-\s+.*)?$", re.MULTILINE)
NODE_TEST_FAIL_LINE = re.compile(r"^\s*not ok \d+ - (.+?)\s*$", re.MULTILINE)
NODE_TEST_SPEC_FAIL = re.compile(r"^\s*✖\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*ms\))?\s*$",
                                 re.MULTILINE)
NODE_TEST_DIRECTIVE = re.compile(r"\s+#\s*(?:TODO|SKIP)\b.*$", re.IGNORECASE)
TAP_MARKER = re.compile(r"^TAP version \d+\s*$", re.MULTILINE)
NODE_SPEC_MARKER = re.compile(r"^ℹ (?:tests|pass|fail) \d+", re.MULTILINE)
NODE_COUNT = re.compile(r"^(?:#|ℹ)\s*(pass|fail|skipped|todo|cancelled) (\d+)",
                        re.MULTILINE)
NODE_TOTAL = re.compile(r"^(?:#|ℹ)\s*tests (\d+)", re.MULTILINE)
NODE_FAILS = re.compile(r"^(?:#|ℹ)\s*fail (\d+)", re.MULTILINE)
JEST_FAIL_SUITE = re.compile(r"^\s*FAIL\s+(\S+)\s*$", re.MULTILINE)
VITEST_FAIL = re.compile(r"^\s*FAIL\s+(\S+)\s+>\s+(.+?)\s*$", re.MULTILINE)
VITEST_LOAD_FAIL = re.compile(r"^\s*FAIL\s+(\S+)\s*\[", re.MULTILINE)
JEST_BULLET = re.compile(r"^\s*●\s*(.+?)\s*$", re.MULTILINE)
JEST_NON_TEST_BULLETS = ("Console", "Validation Error", "Validation Warning",
                         "Test suite failed to run")
TS_ERROR = re.compile(r"^(.+?)\((\d+),(\d+)\): error (TS\d+):")
MYPY_ERROR = re.compile(r"^(.+?):(\d+): error: (.+)$")
ESLINT_COMPACT = re.compile(r"^(.+?): line (\d+), col (\d+), (?:Error|Warning) - .+\(([a-z0-9-]+)\)$")
GENERIC_ERROR = re.compile(r"^(.+?):(\d+):\d*:?\s?error:", re.IGNORECASE)
GO_FAIL = re.compile(r"^\s*--- FAIL: (\S+)", re.MULTILINE)
GO_BUILD_FAIL = re.compile(r"^FAIL\s+(\S+)\s+\[build failed\]", re.MULTILINE)
CARGO_CASE = re.compile(r"^---- (\S+) stdout ----", re.MULTILINE)


def _pytest_fingerprints(output: str) -> list:
    return [match.strip()[:MAX_FINGERPRINT]
            for match in PYTEST_FAIL_LINE.findall(output)]


def _jest_fingerprints(output: str) -> list:
    fingerprints = [path[:MAX_FINGERPRINT] for path in JEST_FAIL_SUITE.findall(output)]
    fingerprints += [f"{path} > {name}"[:MAX_FINGERPRINT]
                     for path, name in VITEST_FAIL.findall(output)]
    for name in JEST_BULLET.findall(output):
        if name.startswith(JEST_NON_TEST_BULLETS):
            continue
        fingerprints.append(name[:MAX_FINGERPRINT])
    return fingerprints


def _compiler_fingerprints(output: str) -> list:
    """Typechecker/linter errors. Line and column are dropped: a shifted line is the
    same defect; the file and error code are the identity."""
    fingerprints = []
    for line in output.splitlines():
        line = line.strip()
        match = TS_ERROR.match(line)
        if match:
            fingerprints.append(f"{match.group(4)} {match.group(1)}")
            continue
        match = MYPY_ERROR.match(line)
        if match:
            code_match = re.search(r"\[([a-z0-9-]+)\]\s*$", match.group(3))
            code = code_match.group(1) if code_match else "mypy"
            fingerprints.append(f"{code} {match.group(1)}")
            continue
        match = ESLINT_COMPACT.match(line)
        if match:
            fingerprints.append(f"{match.group(4)} {match.group(1)}")
            continue
        match = GENERIC_ERROR.match(line)
        if match:
            fingerprints.append(f"error {match.group(1)}")
    return fingerprints


def _go_fingerprints(output: str) -> list:
    """Go test names (subtests kept). A package build failure collapses every compile
    error in that package into one fingerprint."""
    fingerprints = [f"Test {name}" for name in GO_FAIL.findall(output)]
    fingerprints += [f"build-failed: {pkg}" for pkg in GO_BUILD_FAIL.findall(output)]
    return fingerprints


def _cargo_fingerprints(output: str) -> list:
    if "test result:" not in output:
        return []
    fingerprints = CARGO_CASE.findall(output)
    in_block = False
    for line in output.splitlines():
        if line.strip() == "failures:":
            in_block = True
            continue
        if not in_block:
            continue
        if not line.strip():
            in_block = False
            continue
        name = line.strip()
        if line.startswith((" ", "\t")) and re.fullmatch(r"[\w:/.\-\[\]<>]+", name):
            fingerprints.append(name)
    return fingerprints


def _node_test_fingerprints(output: str) -> list:
    """node:test failing test names (TAP and spec reporters). Names only - the runner
    does not print file paths, which the discrimination check accounts for."""
    names = [NODE_TEST_DIRECTIVE.sub("", name) for name in NODE_TEST_FAIL_LINE.findall(output)]
    names += [NODE_TEST_DIRECTIVE.sub("", name) for name in NODE_TEST_SPEC_FAIL.findall(output)]
    return [name.strip()[:MAX_FINGERPRINT] for name in names if name.strip()]


def parse_fingerprints(output: str) -> list:
    """Union of all family extractors, deduplicated and sorted."""
    clean = _sanitize(output)
    found = (_pytest_fingerprints(clean) + _jest_fingerprints(clean)
             + _compiler_fingerprints(clean) + _go_fingerprints(clean)
             + _cargo_fingerprints(clean) + _node_test_fingerprints(clean))
    return sorted({name for name in found if name})


def _jest_suite_errors(output: str) -> list:
    """Jest paths whose `FAIL <path>` line is followed by the module-level
    `Test suite failed to run` marker (resolution/transform/compile failures)."""
    lines = output.splitlines()
    errors = []
    for index, line in enumerate(lines):
        match = JEST_FAIL_SUITE.match(line.strip())
        if not match:
            continue
        for follow in lines[index + 1:index + 5]:
            if "●" in follow and "Test suite failed to run" in follow:
                errors.append(match.group(1)[:MAX_FINGERPRINT])
                break
    return errors


def parse_errors(output: str) -> list:
    """Collection/run-level entries: the file could not be executed at all (import,
    compile, transform), as opposed to a test assertion failing."""
    clean = _sanitize(output)
    found = ([match.strip()[:MAX_FINGERPRINT]
              for match in PYTEST_ERROR_LINE.findall(clean)]
             + [path[:MAX_FINGERPRINT] for path in VITEST_LOAD_FAIL.findall(clean)]
             + _jest_suite_errors(clean) + _compiler_fingerprints(clean))
    return sorted({name for name in found if name})


# ----------------------------------------------------------------- inventory

JEST_SUMMARY = re.compile(r"^\s*Tests:?\s+(.+)$", re.MULTILINE)
VITEST_MARKER = re.compile(r"^\s*Test Files\s+", re.MULTILINE)
JEST_COUNTS = re.compile(r"(\d+) (failed|passed|skipped|todo|total)")
CARGO_SUMMARY = re.compile(r"^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; "
                           r"(\d+) ignored", re.MULTILINE)
GO_PACKAGE_LINE = re.compile(r"^(?:ok|FAIL)\s+\S+\s+(?:\(cached\)|\[\S[^\]]*\]|[0-9.]+s)",
                             re.MULTILINE)
PYTEST_SUMMARY_COUNTS = re.compile(
    r"(\d+) (passed|failed|error|errors|skipped|xfailed|deselected)\b")
PYTEST_BARE_SUMMARY = re.compile(
    r"^(?:\d+ (?:passed|failed|error|errors|skipped|xfailed|deselected)(?:, )?)+$")


def _node_test_inventory(output: str) -> dict | None:
    """Counts from node:test summaries (TAP `# pass N` or spec `ℹ pass N`).

    Multi-file runs emit one summary per file; all of them are summed. Cancelled tests
    are counted as skipped (they did not execute)."""
    counts = {}
    for kind, number in NODE_COUNT.findall(output):
        counts[kind] = counts.get(kind, 0) + int(number)
    total = NODE_TOTAL.search(output)
    if not counts and not total:
        return None
    ran = counts.get("pass", 0) + counts.get("fail", 0)
    if ran == 0 and total:
        ran = int(total.group(1))
    skipped = counts.get("skipped", 0) + counts.get("todo", 0) + counts.get("cancelled", 0)
    return {"tests_ran": ran, "skipped": skipped}


def _jest_inventory(output: str) -> dict | None:
    summaries = JEST_SUMMARY.findall(output)
    if not summaries:
        return None
    tests_ran = 0
    skipped = 0
    for summary in summaries:
        counts = {kind: 0 for kind in ("failed", "passed", "skipped", "todo", "total")}
        for number, kind in JEST_COUNTS.findall(summary):
            counts[kind] += int(number)
        skipped += counts["skipped"] + counts["todo"]
        tests_ran += counts["total"] if counts["total"] else \
            counts["failed"] + counts["passed"] + counts["skipped"] + counts["todo"]
    return {"tests_ran": tests_ran - skipped, "skipped": skipped}


def _cargo_inventory(output: str) -> dict | None:
    summaries = CARGO_SUMMARY.findall(output)
    if not summaries:
        return None
    passed = built = ignored = 0
    for passed_n, failed_n, ignored_n in summaries:
        passed += int(passed_n)
        built += int(failed_n)
        ignored += int(ignored_n)
    return {"tests_ran": passed + built, "skipped": ignored}


def _go_inventory(output: str) -> dict | None:
    packages = GO_PACKAGE_LINE.findall(output)
    if not packages:
        return None
    return {"tests_ran": len(packages), "skipped": None}


def _pytest_inventory(output: str) -> dict | None:
    summary = ""
    for candidate in output.splitlines():
        stripped = candidate.strip()
        if "no tests ran" in stripped:
            summary = stripped
        elif PYTEST_SUMMARY_COUNTS.search(stripped) \
                and (" in " in stripped or PYTEST_BARE_SUMMARY.match(stripped)):
            summary = stripped
    if not summary:
        return None
    if "no tests ran" in summary:
        return {"tests_ran": 0, "skipped": 0}
    counts = {}
    for number, kind in PYTEST_SUMMARY_COUNTS.findall(summary):
        counts[kind] = counts.get(kind, 0) + int(number)
    tests_ran = sum(counts.get(kind, 0) for kind in ("passed", "failed", "error", "errors"))
    return {"tests_ran": tests_ran, "skipped": counts.get("skipped", 0)}


INVENTORIES = [
    ("jest", "tests", _jest_inventory),
    ("cargo", "tests", _cargo_inventory),
    ("go", "packages", _go_inventory),
    ("pytest", "tests", _pytest_inventory),
]


def parse_report(output: str) -> dict:
    """Parse a suite run: failure count, failure fingerprints, executed-test inventory.

    ``tests_ran`` counts executed tests only (skips/ignores excluded) so weakening a
    suite by skipping or deleting tests shows up as shrinkage.
    """
    clean = _sanitize(output)
    report = {"family": "unknown", "failures": parse_failures(clean),
              "fingerprints": parse_fingerprints(clean),
              "errors": parse_errors(clean), "tests_ran": None,
              "skipped": None, "unit": "tests"}
    if VITEST_MARKER.search(clean):
        report["family"] = "vitest"
        found = _jest_inventory(clean)
        if found:
            report.update(found)
        return report
    if TAP_MARKER.search(clean) or NODE_SPEC_MARKER.search(clean):
        report["family"] = "node_test"
        found = _node_test_inventory(clean)
        if found:
            report.update(found)
        return report
    for family, unit, inventory in INVENTORIES:
        found = inventory(clean)
        if found:
            report["family"] = family
            report["unit"] = unit
            report.update(found)
            break
    return report


def _capture_report(result: dict, report: dict, stdout: str) -> None:
    """Fold one parse_report output into the result (shared by success and timeout)."""
    result["family"] = report["family"]
    result["fingerprints"] = report["fingerprints"]
    result["errors"] = report["errors"]
    result["tests_ran"] = report["tests_ran"]
    result["skipped"] = report["skipped"]
    result["unit"] = report["unit"]
    result["output"] = stdout[-20000:]


def run_regression(project_root: str, command: str, timeout_s: float = 900,
                   evidence_path: str | None = None, env: dict | None = None) -> dict:
    """Run the suite via the shell (user-visible commands, .cmd shims), capture output.

    ``env`` overrides individual environment variables (the discrimination check uses it
    to prepend the worktree's source roots to PYTHONPATH).

    On timeout the whole process tree is killed, not just the shell: a hung watch-mode
    suite otherwise keeps running (and keeps holding ports) after the gate gives up.
    """
    started = time.time()
    result = {"command": command, "rc": None, "failures": None, "timeout": False,
              "duration_s": None, "ok": False, "output": ""}
    run_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.Popen(command, shell=True, cwd=str(project_root),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                env=run_env, **spawn_flags())
        try:
            full, _ = proc.communicate(timeout=timeout_s)
            full = full or ""
            report = parse_report(full)
            result["rc"] = proc.returncode
            result["failures"] = report["failures"]
            _capture_report(result, report, full)
            result["ok"] = proc.returncode == 0
            if proc.returncode == 5 and result["failures"] is None:
                result["note"] = "no tests collected (pytest rc=5) - check the command"
        except subprocess.TimeoutExpired:
            result["timeout"] = True
            kill_tree(proc.pid)
            try:
                partial, _ = proc.communicate(timeout=30)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                proc.kill()
                partial = ""
            partial = partial or ""
            _capture_report(result, parse_report(partial), partial)
            result["note"] = f"timed out after {timeout_s}s"
    except OSError as exc:
        result["note"] = f"could not launch: {exc}"
    result["duration_s"] = round(time.time() - started, 1)
    if evidence_path:
        path = Path(evidence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"$ {command}\n(rc={result['rc']}, failures={result['failures']},"
                        f" timeout={result['timeout']})\n\n{result['output']}\n",
                        encoding="utf-8")
    return result


# ------------------------------------------------------------------ comparison

def compare(baseline: dict | None, current: dict, strict: bool = True,
            shrink_tolerance: int = 0) -> dict:
    """Decide whether `current` regressed relative to `baseline`.

    Identity-based for red -> red (new failing tests), inventory-based for green -> green
    (suite shrinkage), and fail-closed on uncomparable results when ``strict``.
    """
    if baseline is None:
        return {"regressed": False, "reasons": ["no baseline recorded"],
                "baseline_was_red": False, "indeterminate": False, "strict": strict,
                "new_failures": [], "fixed_failures": [], "suite_delta": None,
                "baseline": None, "current": current}

    regressed = False
    reasons = []
    indeterminate = False
    baseline_ok = baseline.get("rc") == 0
    current_ok = current.get("rc") == 0
    base_fp = set(baseline.get("fingerprints") or [])
    curr_fp = set(current.get("fingerprints") or [])
    new_failures = sorted(curr_fp - base_fp)
    fixed_failures = sorted(base_fp - curr_fp)
    suite_delta = None
    base_tests = baseline.get("tests_ran")
    curr_tests = current.get("tests_ran")
    if isinstance(base_tests, int) and isinstance(curr_tests, int):
        suite_delta = curr_tests - base_tests
    unit = current.get("unit") or baseline.get("unit") or "tests"

    if current.get("timeout"):
        regressed = True
        indeterminate = True
        reasons.append("regression run timed out")
    elif baseline_ok and not current_ok:
        regressed = True
        reasons.append(f"exit code {baseline.get('rc')} -> {current.get('rc')}")
        if new_failures:
            reasons.append("new failing tests: " + ", ".join(new_failures[:5]))
    elif base_fp and curr_fp:
        if new_failures:
            regressed = True
            reasons.append("new failing tests: " + ", ".join(new_failures[:5]))
        else:
            reasons.append(f"no new failing tests ({len(curr_fp)} known-failing, "
                           f"{len(fixed_failures)} fixed)")
    elif not baseline_ok and not current_ok:
        base_count = baseline.get("failures")
        curr_count = current.get("failures")
        if base_count is not None and curr_count is not None and curr_count > base_count:
            regressed = True
            reasons.append(f"failures {base_count} -> {curr_count}")
        else:
            indeterminate = True
            reasons.append("failure identity not available (no fingerprints)")
    elif not baseline_ok and current_ok:
        reasons.append("baseline was already failing; current run is green")

    if not current.get("timeout") and suite_delta is not None \
            and suite_delta < -abs(int(shrink_tolerance)):
        regressed = True
        reasons.append(f"suite shrank {base_tests} -> {curr_tests} {unit}")

    if indeterminate and strict and not regressed:
        regressed = True
    if indeterminate and not strict and not regressed:
        reasons[-1] = "warning: " + reasons[-1]
    return {"regressed": regressed, "reasons": reasons,
            "baseline_was_red": not baseline_ok, "indeterminate": indeterminate,
            "strict": strict, "new_failures": new_failures,
            "fixed_failures": fixed_failures, "suite_delta": suite_delta,
            "baseline": baseline, "current": current}
