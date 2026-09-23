"""Run worktrees: an isolated checkout so a run never touches the developer's tree.

A worktree-mode run makes its checkout the run root - workers, gates, and evidence all
live there, on the run's own branch - while the developer's checkout keeps its branch,
its index, and its uncommitted work. Mechanics only: where the worktree goes and how the
run records it stays with the caller.
"""

from pathlib import Path

from .gitutil import git

SUBDIR = Path(".swarmflow") / "worktrees"


def default_path(project_root, name: str) -> Path:
    """Where a run worktree goes unless the caller passes a path.

    Inside the project's own ignored ``.swarmflow/``: one predictable place, removed with
    the checkout, and never visible to the project's ``git status``.
    """
    return Path(project_root).resolve() / SUBDIR / (name or "run")


def listed(project_root) -> list:
    """Worktree paths git reports for the repository, main checkout first."""
    ok, output = git(project_root, "worktree", "list", "--porcelain")
    if not ok:
        return []
    return [line[len("worktree "):].strip() for line in output.splitlines()
            if line.startswith("worktree ")]


def ensure(project_root, path, branch: str) -> dict:
    """Create or reuse a worktree at ``path`` checked out on ``branch``.

    Returns ``{"action": "created"|"reused"|"failed", "reason": str}`` and never raises.
    Reuse is keyed on the branch as well as the path, so pointing a second run at the
    first run's worktree fails loudly instead of switching that checkout's branch under
    the run that owns it.
    """
    root, target = Path(project_root).resolve(), Path(path).resolve()
    if target == root:
        return {"action": "failed",
                "reason": "the run worktree cannot be the project checkout itself; pass a "
                          "different --worktree-path"}
    ok, toplevel = git(root, "rev-parse", "--show-toplevel")
    if not ok:
        return {"action": "failed",
                "reason": f"{root} is not a git repository; worktree mode has nothing to "
                          f"branch from"}
    if Path(toplevel).resolve() != root:
        return {"action": "failed",
                "reason": f"worktree mode expects the project root to be the repository "
                          f"toplevel ({toplevel}); paths in the plan would resolve against "
                          f"the wrong directory - run without --worktree"}
    if not git(root, "rev-parse", "--verify", "HEAD")[0]:
        return {"action": "failed",
                "reason": "worktree mode branches from a commit and this repository has "
                          "no resolvable HEAD; commit once and re-run"}
    for existing in listed(root):
        if Path(existing).resolve() != target:
            continue
        if not (target / ".git").exists():
            return {"action": "failed",
                    "reason": f"{target} is registered as a worktree but holds no working "
                              f"tree; run `git worktree prune` and re-run"}
        ok, current = git(target, "rev-parse", "--abbrev-ref", "HEAD")
        if not ok or current.strip() != branch:
            return {"action": "failed",
                    "reason": f"{target} is a worktree on "
                              f"{current.strip() or 'an unknown branch'}, not {branch}; it "
                              f"belongs to another run - remove it or pass a different "
                              f"--worktree-path"}
        return {"action": "reused",
                "reason": f"{target} is already this run's worktree on {branch}"}
    if target.exists():
        return {"action": "failed",
                "reason": f"{target} exists and is not a worktree of this repository; "
                          f"move it aside or pass a different --worktree-path"}
    target.parent.mkdir(parents=True, exist_ok=True)
    if git(root, "rev-parse", "--verify", branch)[0]:
        args = ["worktree", "add", "--quiet", str(target), branch]
    else:
        args = ["worktree", "add", "--quiet", "-b", branch, str(target)]
    ok, output = git(root, *args, include_stderr=True)
    if not ok:
        return {"action": "failed",
                "reason": f"git worktree add failed: {output.strip()[:300]}"}
    return {"action": "created", "reason": f"{branch} checked out at {target}"}
