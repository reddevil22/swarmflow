"""Control-plane run state, kept outside the worker-writable project tree.

Gate decisions must not read state a worker can rewrite: the frozen baseline, the run
record, and the resolved regression command live here, keyed by the project path. The
project keeps only an informational mirror of ``run.json`` that nothing reads.
"""

import hashlib
import json
import os
from pathlib import Path

from .config import REPO_ROOT

_STATE_ROOT = REPO_ROOT / "state"
LEGACY_KEYS = ("project", "mode", "branch", "base_sha", "created_at")


def set_state_root(path) -> None:
    global _STATE_ROOT
    _STATE_ROOT = Path(path)


def state_root() -> Path:
    return _STATE_ROOT


def store_dir(project_root) -> Path:
    """Per-project store directory (created on demand)."""
    resolved = str(Path(project_root).resolve())
    key = hashlib.sha1(resolved.lower().encode("utf-8")).hexdigest()[:12]
    directory = state_root() / "runs" / key
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _write_atomic(path: Path, data: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temp, path)


def load_run(project_root) -> dict:
    data = _read_json(store_dir(project_root) / "run.json")
    return data if isinstance(data, dict) else {}


def save_run(project_root, data: dict) -> None:
    _write_atomic(store_dir(project_root) / "run.json", data)


def persist_run(project_root, data: dict, mirror: bool = False) -> dict:
    """Save the run state, optionally refreshing the project-side mirror.

    One place knows about the mirror so no caller has to remember the pair."""
    save_run(project_root, data)
    if mirror:
        write_mirror(project_root, data)
    return data


def load_frozen(project_root):
    data = _read_json(store_dir(project_root) / "frozen.json")
    return data if isinstance(data, dict) else None


def save_frozen(project_root, baseline: dict) -> None:
    _write_atomic(store_dir(project_root) / "frozen.json", baseline)


def frozen_path(project_root) -> Path:
    return store_dir(project_root) / "frozen.json"


def run_path(project_root) -> Path:
    return store_dir(project_root) / "run.json"


def write_mirror(project_root, data: dict) -> None:
    """Write the informational project-side copy. Nothing ever reads it."""
    path = Path(project_root) / ".swarmflow" / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    mirror = dict(data)
    mirror["_mirror"] = ("informational only; the control plane reads its own state store")
    _write_atomic(path, mirror)


def adopt_legacy(project_root) -> list:
    """One-time import of a pre-upgrade `.swarmflow/run.json` (brownfield only).

    Structural fields only: `audit_ignores` and `regression` are worker-influenceable
    and are never imported; a mirror written by this tool is refused.
    """
    project = Path(project_root)
    if run_path(project_root).exists():
        return []
    legacy = _read_json(project / ".swarmflow" / "run.json")
    if not isinstance(legacy, dict) or legacy.get("_mirror"):
        return []
    if legacy.get("mode") != "brownfield":
        return []
    adopted = {key: legacy[key] for key in LEGACY_KEYS if key in legacy}
    adopted["project"] = str(project.resolve())
    adopted["adopted"] = True
    adopted["adopted_from"] = str(project / ".swarmflow" / "run.json")
    save_run(project_root, adopted)
    return sorted(adopted)
