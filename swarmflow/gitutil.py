"""Git subprocess helpers.

Three copies of the run helper had drifted apart (strip or not, stderr folded into the
output or dropped, different default timeouts), which hid behavioral differences in
incidental code. The parameters here keep every difference explicit at the call site.
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


def is_ignored(project_root, path: str) -> bool:
    """True if git would ignore this path (``check-ignore`` exit 0)."""
    ok, _ = git(project_root, "check-ignore", "-q", "--", path)
    return ok


def changed_against_head(project_root, paths) -> list:
    """Which of ``paths`` differ from HEAD: modified, deleted, or untracked.

    The task's whole delivered change, not one attempt's diff - a retry starts from a
    fresh snapshot, so per-attempt bookkeeping cannot see the earlier attempt's files.
    Pathspecs are literal, matching how the scope audit reads the same names.
    """
    if not paths:
        return []
    changed = []
    ok, tracked = git(project_root, "--literal-pathspecs", "diff", "--name-only", "HEAD",
                      "--", *paths, include_stderr=True)
    if ok:
        changed.extend(line for line in tracked.splitlines() if line.strip())
    ok, untracked = git(project_root, "--literal-pathspecs", "ls-files", "--others",
                        "--exclude-standard", "--", *paths)
    if ok:
        changed.extend(line for line in untracked.splitlines()
                       if line.strip() and line not in changed)
    return changed


def commit_paths(project_root, paths, message: str, name: str = "",
                 email: str = "") -> tuple:
    """Commit the working-tree content of exactly ``paths``; return ``(ok, sha|error)``.

    Pathspecs are literal, so a path that looks like a glob (or like ``:/``) can only ever
    match itself, and a pathspec commit leaves the operator's other staged work staged.
    ``name``/``email`` and ``commit.gpgsign`` are set for this one commit, so a generated
    commit does not depend on the machine's config - repository hooks still run.
    """
    if not paths:
        return False, "no paths to commit"
    ok, output = git(project_root, "--literal-pathspecs", "add", "--", *paths,
                     include_stderr=True)
    if not ok:
        git(project_root, "reset", "-q", "--", *paths)   # leave no half-staged index
        return False, f"git add failed: {output.strip()[:200]}"
    identity = ["-c", "commit.gpgsign=false"]
    if name:
        identity += ["-c", f"user.name={name}"]
    if email:
        identity += ["-c", f"user.email={email}"]
    ok, output = git(project_root, "--literal-pathspecs", *identity, "commit", "-m",
                     message, "--", *paths, include_stderr=True)
    if not ok:
        git(project_root, "reset", "-q", "--", *paths)   # leave no half-staged index
        return False, output.strip()[:300] or "git commit failed"
    ok, sha = git(project_root, "rev-parse", "--short", "HEAD")
    return True, sha if ok else ""
