"""One git subprocess helper.

Three copies had drifted apart (strip or not, stderr folded into the output or dropped,
different default timeouts), which hid behavioral differences in incidental code. The
parameters here keep every difference explicit at the call site.
"""

import subprocess


def git(project_root, *args: str, timeout: int = 60, strip: bool = True,
        include_stderr: bool = False):
    """Run ``git <args>`` in ``project_root``; return ``(ok, output)``, never raising.

    ``strip`` trims the captured output; ``include_stderr`` folds stderr into it (useful
    for diff/log, where git writes progress there).
    """
    try:
        proc = subprocess.run(["git", *args], cwd=str(project_root), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    output = proc.stdout or ""
    if include_stderr:
        output += proc.stderr or ""
    return proc.returncode == 0, output.strip() if strip else output
