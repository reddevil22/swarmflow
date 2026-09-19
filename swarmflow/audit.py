"""Frozen-file integrity and scope audit.

The envelope contract: every file present when `freeze` runs must stay byte-identical
afterwards unless it is owned by a task. New files are only legitimate inside a task's
ownership list; anything else is a violation.

This is the gate for the failure classes seen in the TaskDock pilot: frozen files
modified by workers (package.json), frozen files deleted (node_modules is ignored, but
tracked files count), and stray files created outside ownership (pnpm-lock.yaml, nul).
"""

import fnmatch
import hashlib
import json
import os
import subprocess
from pathlib import Path

from . import runstate

DEFAULT_IGNORES = [
    ".git", "node_modules", "dist", "build", "coverage", "logs", "state",
    ".swarmflow", "__pycache__", ".pytest_cache", "*.pyc", "*.log",
    "target", ".venv", "venv", "vendor", ".tox", ".next", ".mypy_cache",
    ".ruff_cache", ".coverage", "*.tsbuildinfo", "*.egg-info",
]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_patterns(ignores: list[str]) -> list[str]:
    return [pattern for pattern in ignores if any(ch in pattern for ch in "*?[")]


def _iter_files(root: Path, ignores: list[str]) -> dict:
    """Map relative posix path -> sha256 for all non-ignored files under root.

    Ignore entries match directory names, exact file names, or glob patterns.
    """
    files = {}
    patterns = _file_patterns(ignores)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignores]
        for name in filenames:
            if name in ignores:
                continue
            if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            try:
                files[rel] = _hash_file(full)
            except OSError:
                continue
    return files


def freeze(project_root: str, owners: dict, ignores: list[str] | None = None,
           mode: str = "greenfield", carry_over: bool = False) -> dict:
    """Snapshot the current tree as the frozen baseline.

    greenfield: walk-based snapshot of every non-ignored file; the effective ignore
    list is persisted in the baseline so later ignore edits cannot shift semantics.
    brownfield: git-tracked files only - the repository's own .gitignore is the
    ignore list, which ends the ignore-list arms race.

    ``carry_over`` (brownfield only, used by the per-wave freeze): files that are
    neither new nor owned by the current wave keep the hash from the previous baseline -
    even when the file is missing - so edits made between waves stay visible instead of
    being silently re-blessed. Owned files are re-read here (pre-wave content) and again
    by ``seal`` after the wave. A deliberate re-baseline uses the default
    (``carry_over=False``): ``swarmflow freeze``.

    `owners` maps task id -> list of owned (and therefore mutable) file paths,
    relative to the project root.
    """
    root = Path(project_root).resolve()
    merged = DEFAULT_IGNORES + list(ignores or [])
    owner_map = {rel: task_id for task_id, owned in owners.items() for rel in owned}
    carried = refreshed = 0
    if mode == "brownfield":
        previous = runstate.load_frozen(str(root)) if carry_over else None
        if not isinstance(previous, dict) or previous.get("mode") != "brownfield":
            previous = None
        if previous is None:
            files = {}
            for rel in _git_tracked(root):
                try:
                    files[rel] = _hash_file(root / rel)
                except OSError:
                    continue
                refreshed += 1
        else:
            previous_files = previous.get("files") or {}
            files = {}
            for rel in sorted(set(_git_tracked(root)) | set(previous_files)):
                if rel in owner_map:
                    path = root / rel
                    if path.exists():
                        try:
                            files[rel] = _hash_file(path)
                        except OSError:
                            continue
                        refreshed += 1
                    continue          # owned but missing: the seal will drop it
                if rel in previous_files:
                    files[rel] = previous_files[rel]
                    carried += 1
                elif (root / rel).exists():
                    try:
                        files[rel] = _hash_file(root / rel)
                    except OSError:
                        continue
                    refreshed += 1
        baseline = {"mode": "brownfield", "files": files, "owners": owner_map}
    else:
        files = _iter_files(root, merged)
        baseline = {"mode": "greenfield", "files": files, "ignores": merged,
                    "owners": owner_map}
        refreshed = len(files)
    runstate.save_frozen(str(root), baseline)
    return {"frozen_files": len(baseline["files"]),
            "entries": len(baseline["files"]),
            "owned": len(owner_map), "carried": carried, "refreshed": refreshed,
            "baseline": str(runstate.frozen_path(str(root))), "mode": mode}


def seal(project_root: str, owners: dict) -> dict:
    """Record post-wave hashes for the files this wave owned.

    An owned file's content right after its wave is what the wave was allowed to change
    it to; missing owned files are dropped (an owner removing its own file is
    legitimate). Called by ``wave-run`` immediately after the wave audit.
    """
    root = Path(project_root).resolve()
    baseline = runstate.load_frozen(str(root))
    if not isinstance(baseline, dict) or baseline.get("mode") != "brownfield":
        return {"sealed": 0, "dropped": 0}
    paths = {rel for owned in owners.values() for rel in owned}
    files = baseline.get("files") or {}
    sealed = dropped = 0
    for rel in sorted(paths):
        if rel not in files:
            continue              # new/untracked files are outside the baseline today
        path = root / rel
        if path.exists():
            try:
                files[rel] = _hash_file(path)
            except OSError:
                continue
            sealed += 1
        else:
            del files[rel]
            dropped += 1
    baseline["files"] = files
    runstate.save_frozen(str(root), baseline)
    return {"sealed": sealed, "dropped": dropped}


def audit(project_root: str, ignores: list[str] | None = None) -> dict:
    """Compare the current tree against the frozen baseline.

    Violation kinds: deleted_frozen, modified_frozen, added_unowned, no_baseline,
    corrupt_baseline, no_git. Owned files (from the baseline owner map) are exempt.
    """
    root = Path(project_root).resolve()
    baseline = runstate.load_frozen(str(root))
    if baseline is None:
        path = runstate.frozen_path(str(root))
        if path.exists():
            return {"ok": False, "violations": [
                {"kind": "corrupt_baseline", "path": str(path),
                 "detail": "the frozen baseline is unreadable; re-run freeze"}]}
        return {"ok": False, "violations": [
            {"kind": "no_baseline", "path": str(path), "detail": "run freeze first"}]}
    if baseline.get("mode") == "brownfield":
        return _audit_brownfield(root, baseline, ignores)
    frozen = baseline.get("files", {})
    owners = baseline.get("owners", {})
    effective = baseline.get("ignores") or (DEFAULT_IGNORES + list(ignores or []))
    current = _iter_files(root, effective)

    violations = []
    for rel, digest in sorted(frozen.items()):
        if rel in owners:
            continue
        if rel not in current:
            violations.append({"kind": "deleted_frozen", "path": rel})
        elif current[rel] != digest:
            violations.append({"kind": "modified_frozen", "path": rel})
    for rel in sorted(current):
        if rel in frozen or rel in owners:
            continue
        violations.append({"kind": "added_unowned", "path": rel})
    return {"ok": not violations, "violations": violations}


def _git(project_root: Path, *args: str, timeout: int = 60):
    try:
        proc = subprocess.run(["git", *args], cwd=str(project_root), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "")
    except (OSError, subprocess.TimeoutExpired):
        return False, ""


def _git_tracked(root: Path) -> list:
    ok, out = _git(root, "ls-files")
    if not ok:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _git_untracked(root: Path) -> list:
    ok, out = _git(root, "status", "--porcelain")
    if not ok:
        return []
    entries = []
    for line in out.splitlines():
        if line.startswith("?? "):
            rel = line[3:].strip()
            if rel.startswith('"') and rel.endswith('"'):
                rel = rel[1:-1].encode("latin-1", "replace").decode("unicode_escape")
            entries.append(rel.replace("\\", "/"))
    return entries


def _ignored(rel: str, ignores: list | None) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in (ignores or []))


def _audit_brownfield(root: Path, baseline: dict, ignores: list | None = None) -> dict:
    """Git-based audit: tracked hashes must match unless owned; untracked-unowned is
    a violation; gitignored files are invisible to both lists by construction."""
    ok, _ = _git(root, "rev-parse", "--is-inside-work-tree")
    if not ok:
        return {"ok": False, "violations": [
            {"kind": "no_git", "path": str(root),
             "detail": "brownfield audit requires a git repository"}]}
    frozen = baseline.get("files", {})
    owners = baseline.get("owners", {})
    violations = []
    for rel, digest in sorted(frozen.items()):
        if rel in owners:
            continue
        path = root / rel
        if not path.exists():
            violations.append({"kind": "deleted_frozen", "path": rel})
            continue
        try:
            if _hash_file(path) != digest:
                violations.append({"kind": "modified_frozen", "path": rel})
        except OSError:
            violations.append({"kind": "deleted_frozen", "path": rel})
    for rel in _git_untracked(root):
        if rel in owners or rel in frozen or _ignored(rel, ignores):
            continue
        if rel.split("/", 1)[0] in (".swarmflow", "logs", "state"):
            continue  # swarmflow's own artifacts, expected to be gitignored
        violations.append({"kind": "added_unowned", "path": rel})
    return {"ok": not violations, "violations": violations}
