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
from pathlib import Path

DEFAULT_IGNORES = [
    ".git", "node_modules", "dist", "build", "coverage", "logs", "state",
    ".swarmflow", "__pycache__", ".pytest_cache", "*.pyc", "*.log",
]

BASELINE_REL = ".swarmflow/frozen.json"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_patterns(ignores: list[str]) -> list[str]:
    return [pattern for pattern in ignores if any(ch in pattern for ch in "*?[")]


def _iter_files(root: Path, ignores: list[str]) -> dict:
    """Map relative posix path -> sha256 for all non-ignored files under root."""
    files = {}
    patterns = _file_patterns(ignores)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignores]
        for name in filenames:
            if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if rel == BASELINE_REL:
                continue
            try:
                files[rel] = _hash_file(full)
            except OSError:
                continue
    return files


def freeze(project_root: str, owners: dict, ignores: list[str] | None = None) -> dict:
    """Snapshot the current tree as the frozen baseline.

    `owners` maps task id -> list of owned (and therefore mutable) file paths,
    relative to the project root.
    """
    root = Path(project_root).resolve()
    merged = DEFAULT_IGNORES + list(ignores or [])
    files = _iter_files(root, merged)
    owner_map = {rel: task_id for task_id, owned in owners.items() for rel in owned}
    baseline = {"files": files, "owners": owner_map}
    path = root / BASELINE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, indent=2, sort_keys=True), encoding="utf-8")
    return {"frozen_files": len(files), "owned": len(owner_map), "baseline": str(path)}


def audit(project_root: str, ignores: list[str] | None = None) -> dict:
    """Compare the current tree against the frozen baseline.

    Violation kinds: deleted_frozen, modified_frozen, added_unowned, no_baseline.
    Owned files (from the baseline owner map) are exempt from all checks.
    """
    root = Path(project_root).resolve()
    baseline_path = root / BASELINE_REL
    if not baseline_path.exists():
        return {"ok": False, "violations": [
            {"kind": "no_baseline", "path": BASELINE_REL, "detail": "run freeze first"}]}
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    frozen = baseline.get("files", {})
    owners = baseline.get("owners", {})
    current = _iter_files(root, DEFAULT_IGNORES + list(ignores or []))

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
