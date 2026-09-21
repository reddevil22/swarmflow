"""Packaged assets and the user-writable state location.

Shipped files (prompts, worker rules, the example config) live inside the package so an
installed wheel has them; code resolves them through :func:`asset`, never through the
source-tree layout. Control-plane state defaults to a per-user directory for the same
reason - an installed CLI cannot write inside site-packages.
"""

import os
import sys
from importlib import resources
from pathlib import Path


def asset(*parts: str) -> Path:
    """Absolute path of a packaged asset, e.g. ``asset("prompts", "planner.md")``."""
    return Path(str(resources.files("swarmflow").joinpath("assets", *parts)))


def _env_dir(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else fallback


def state_root() -> Path:
    """Default control-plane state directory ($SWARMFLOW_STATE_DIR wins)."""
    override = os.environ.get("SWARMFLOW_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = _env_dir("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = _env_dir("XDG_STATE_HOME", Path.home() / ".local" / "state")
    return base / "swarmflow"


def user_config_path() -> Path:
    """Default per-user config path ($SWARMFLOW_CONFIG wins)."""
    override = os.environ.get("SWARMFLOW_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = _env_dir("APPDATA", Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = _env_dir("XDG_CONFIG_HOME", Path.home() / ".config")
    return base / "swarmflow" / "swarmflow.yaml"
