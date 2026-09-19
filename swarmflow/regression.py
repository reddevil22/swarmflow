"""Project regression gate.

Runs the project's configured suite (usually recon-detected, overridable) and compares
against a recorded baseline. This is the brownfield backbone: a feature is only
"delivered" if nothing that worked before broke.
"""

import re
import subprocess
import time
from pathlib import Path


def parse_failures(output: str) -> int | None:
    """Best-effort failure count from common test-runner summaries. None if unknown."""
    match = re.search(r"Tests:\s+.*?(\d+) failed", output)              # jest
    if match:
        return int(match.group(1))
    match = re.search(r"test result: FAILED\.\s+(\d+) passed;\s+(\d+) failed", output)
    if match:                                                          # cargo
        return int(match.group(2))
    match = re.search(r"(\d+) failed", output)                         # pytest
    if match:
        return int(match.group(1))
    go_failures = len(re.findall(r"^--- FAIL", output, re.MULTILINE))  # go
    if go_failures:
        return go_failures
    return None


def run_regression(project_root: str, command: str, timeout_s: float = 900,
                   evidence_path: str | None = None) -> dict:
    """Run the suite via the shell (user-visible commands, .cmd shims), capture output."""
    started = time.time()
    result = {"command": command, "rc": None, "failures": None, "timeout": False,
              "duration_s": None, "ok": False, "output": ""}
    try:
        proc = subprocess.run(command, shell=True, cwd=str(project_root),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout_s)
        output = (proc.stdout or "")
        if proc.stderr:
            output += ("\n" if output else "") + proc.stderr
        result["rc"] = proc.returncode
        result["failures"] = parse_failures(output)
        result["ok"] = proc.returncode == 0
        if proc.returncode == 5 and result["failures"] is None:
            result["note"] = "no tests collected (pytest rc=5) - check the command"
        result["output"] = output[-20000:]
    except subprocess.TimeoutExpired as exc:
        result["timeout"] = True
        captured = exc.stdout if isinstance(exc.stdout, str) else ""
        result["output"] = captured[-20000:]
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


def compare(baseline: dict | None, current: dict) -> dict:
    """Decide whether `current` regressed relative to `baseline`."""
    if baseline is None:
        return {"regressed": False, "reasons": ["no baseline recorded"],
                "baseline_was_red": False, "baseline": None, "current": current}
    regressed = False
    reasons = []
    if current.get("timeout"):
        regressed = True
        reasons.append("regression run timed out")
    baseline_ok = baseline.get("rc") == 0
    current_ok = current.get("rc") == 0
    if baseline_ok and not current_ok:
        regressed = True
        reasons.append(f"exit code {baseline.get('rc')} -> {current.get('rc')}")
    base_failures = baseline.get("failures")
    curr_failures = current.get("failures")
    if base_failures is not None and curr_failures is not None \
            and curr_failures > base_failures:
        regressed = True
        reasons.append(f"failures {base_failures} -> {curr_failures}")
    if not baseline_ok and not regressed:
        reasons.append("baseline was already failing; no new failures introduced")
    return {"regressed": regressed, "reasons": reasons,
            "baseline_was_red": not baseline_ok,
            "baseline": baseline, "current": current}
