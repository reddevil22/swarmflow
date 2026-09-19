"""Configuration loading, portability helpers and executable resolution."""

import os
import shutil
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "swarmflow.yaml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config" / "swarmflow.example.yaml"

DEFAULTS = {
    "frontier": {
        "backend": "auto",   # auto | openai | commandcode | cli | pi
        "base_url": "",      # openai backend: any OpenAI-compatible API
        "api_key": "",       # supports "${ENV_VAR}" references
        "system_prompt": "",
        "extra_body": {},    # merged verbatim into chat completion requests
        "cmd_path": "",      # commandcode backend: CLI path (auto-detected when empty)
        "command": [],       # cli backend: argv template with {prompt_file} / {prompt}
        "output": "text",    # cli backend: text | json
        "result_path": "",   # cli backend: dotted path into JSON output
        "pi_cli": "",        # pi backend: Pi executable (auto-detected when empty)
        "node": "node",
        "thinking": "",
        "model": "deepseek/deepseek-v4-flash",
        "effort": None,
        "timeout_s": 300,
    },
    "worker": {
        "node": "node",
        "pi_cli": "",
        "model": "",
        "thinking": "high",
        "retry_thinking": "medium",
        "timeout_s": 2400,
        "max_turns": 45,
        "max_output_tokens": 32768,
    },
    "swarm": {
        "concurrency": 8,
        "stagger_s": 2.0,
        "metrics_url": "http://127.0.0.1:8000/metrics",
        "backpressure_waiting": 1,
        "backpressure_kv": 0.55,
    },
    "paths": {
        "logs_dir": "logs",
        "ledger": "state/ledger.db",
    },
    "audit": {
        "ignore_extra": [],
    },
    "regression": {
        "enabled": True,
        "command": "",          # overrides recon's detected regression command
        "timeout_s": 900,
        "shrink_tolerance": 0,  # tolerated decrease in executed tests
        "strict": True,         # fail closed when results cannot be compared
    },
    "discrimination": {
        "enabled": True,        # wave tests re-run against the parent state
        "mode": "warn",         # warn (evidence) | enforce (fail the wave)
        "link_dirs": ["node_modules"],
        "timeout_s": 900,
        "max_test_files": 20,
        "skip_patterns": [],    # fnmatch; e.g. browser-e2e specs needing a live server
    },
    "sweep": {
        "enabled": True,        # post-wave process sweep
        "mode": "warn",         # warn (report) | kill (terminate this wave's new processes)
        "kill_requires_port": True,
        "ignore_ports": [8000],  # e.g. a local model / metrics endpoint
        "ignore_names": [],
        "server_names": ["node", "vite", "ts-node", "npm", "npx", "webpack", "next"],
    },
    "git": {
        "branch_prefix": "swarmflow/",
    },
}


def _expand(value):
    """Recursively expand ${ENV_VAR} / $ENV_VAR references in string values."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_config_path(path: str | None = None) -> Path:
    """Resolve the config file: explicit path, $SWARMFLOW_CONFIG, or the default."""
    candidates = []
    if path:
        candidates.append(Path(path))
    elif os.environ.get("SWARMFLOW_CONFIG"):
        candidates.append(Path(os.environ["SWARMFLOW_CONFIG"]))
    else:
        candidates.append(DEFAULT_CONFIG_PATH)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"no swarmflow config found at {candidates[0]}; run `swarmflow init` or copy "
        f"{EXAMPLE_CONFIG_PATH} to {DEFAULT_CONFIG_PATH}"
    )


def load_config(path: str | None = None) -> dict:
    """Load configuration, deep-merged over defaults, with env expansion."""
    config_path = resolve_config_path(path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return _expand(_merge(DEFAULTS, data))


def resolve_executable(names: list[str]) -> str:
    """Return the first executable found on PATH; raises with guidance otherwise."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError(
        "none of " + ", ".join(names) + " found on PATH; install one of them or set "
        "the path explicitly in config/swarmflow.yaml"
    )


def build_cli_command(executable: str, node: str = "node") -> list[str]:
    """Return a cross-platform argv prefix for running an executable or script.

    - ``*.js`` entry points run through node
    - ``*.cmd`` / ``*.bat`` shims run through the command interpreter
    - anything else runs directly
    """
    lowered = executable.lower()
    if lowered.endswith((".js", ".mjs", ".cjs")):
        return [node, executable]
    if lowered.endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", executable]
    return [executable]
