"""Configuration loading, portability helpers and executable resolution."""

import os
import re
import shutil
import warnings
from pathlib import Path

import yaml

from .resources import asset, state_root, user_config_path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = user_config_path()
LEGACY_CONFIG_PATH = REPO_ROOT / "config" / "swarmflow.yaml"
EXAMPLE_CONFIG_PATH = asset("config", "swarmflow.example.yaml")

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
        "retry_thinking": "medium",
        "timeout_s": 2400,
        "max_turns": 45,
        "max_output_tokens": 32768,
        "poll_s": 15.0,         # supervisor tick (per-handle turn/timeout checks)
        "kill_grace_s": 30.0,   # bounded wait after a tree kill before giving up
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
        "ledger": "",          # default: <state_dir>/ledger.db
        "state_dir": "",       # control-plane run store; default: the user state dir
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
        "python_paths": ["src"],  # worktree roots prepended to PYTHONPATH (python runs)
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
    "verify": {
        "enabled": False,       # frontier verification of a wave's delivered tasks
        "max_tasks": 5,         # per wave (wave-run --verify forces the stage for one wave)
    },
    "git": {
        "branch_prefix": "swarmflow/",
        # one commit per delivered task after the wave's scope audit: attribution and a
        # cheap rollback for the operator, with an explicit committer identity
        "commit_tasks": True,
        "commit_name": "swarmflow",
        "commit_email": "swarmflow@localhost",
    },
    # Named providers (see apply_providers): swap models with one line, e.g.
    #   providers:
    #     commandcode:
    #       frontier: {backend: openai, base_url: "https://api.commandcode.ai/provider/v1",
    #                  api_key: "${COMMANDCODE_API_KEY}", model: deepseek/deepseek-v4-flash}
    #       worker: "commandcode/deepseek/deepseek-v4-flash"
    #   role_providers: {frontier: commandcode, worker: commandcode}
    "providers": {},
    "role_providers": {"frontier": "", "worker": ""},
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
    """Resolve the config file: explicit path, $SWARMFLOW_CONFIG, user config, legacy.

    The per-user path is what `swarmflow init` writes; a config left in a source
    checkout's ``config/`` directory is still honoured so dev setups keep working.
    """
    candidates = []
    if path:
        candidates.append(Path(path))
    elif os.environ.get("SWARMFLOW_CONFIG"):
        candidates.append(Path(os.environ["SWARMFLOW_CONFIG"]))
    else:
        candidates.extend([DEFAULT_CONFIG_PATH, LEGACY_CONFIG_PATH])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"no swarmflow config found at {candidates[0]}; run `swarmflow init` or copy "
        f"{EXAMPLE_CONFIG_PATH} there"
    )


def resolve_paths(config: dict) -> dict:
    """Make ``paths.state_dir`` and ``paths.ledger`` absolute, in place.

    Empty values mean "the user state directory" - never the install directory, which
    an installed CLI cannot write inside. An explicit relative path resolves against the
    current working directory.
    """
    paths = config.setdefault("paths", {})
    state_value = str(paths.get("state_dir") or "").strip()
    state = Path(state_value).expanduser() if state_value else state_root()
    if not state.is_absolute():
        state = Path.cwd() / state
    ledger_value = str(paths.get("ledger") or "").strip()
    ledger = Path(ledger_value).expanduser() if ledger_value else state / "ledger.db"
    if not ledger.is_absolute():
        ledger = Path.cwd() / ledger
    paths["state_dir"] = str(state)
    paths["ledger"] = str(ledger)
    return config


SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9._/@:+-]{1,120}$")
CMD_UNSAFE_RE = re.compile(r'[&|^<>%" ]')

# provider frontier blocks are an overlay of these keys; anything else is a typo
FRONTIER_KEYS = frozenset(DEFAULTS["frontier"])
PROVIDER_KEYS = frozenset({"frontier", "worker"})
# obvious placeholders for local endpoints, where a "key" is never a secret
KEY_PLACEHOLDERS = frozenset({"dummy", "no-key-needed", "not-needed", "none", "local", "-"})


def _cmd_routed(path: str) -> bool:
    return path.lower().endswith((".cmd", ".bat"))


def validate_config(config: dict) -> list:
    """Problems that would let a value reach cmd.exe unescaped (or break silently).

    Empty strings and None are legitimate defaults ("not configured yet") and are
    skipped - only values that are actually present are checked.
    """
    problems = []
    worker = config.get("worker") or {}
    frontier = config.get("frontier") or {}

    def check_value(label, value):
        if isinstance(value, str) and value and not SAFE_VALUE_RE.match(value):
            problems.append(f"{label} contains unsupported characters: {value!r}")

    for label in ("model", "thinking", "retry_thinking"):
        check_value(f"worker.{label}", worker.get(label))
    for label in ("model", "thinking", "effort"):
        check_value(f"frontier.{label}", frontier.get(label))
    check_value("worker.node", worker.get("node"))
    check_value("frontier.node", frontier.get("node"))
    for label, value in (("worker.pi_cli", worker.get("pi_cli")),
                         ("frontier.pi_cli", frontier.get("pi_cli")),
                         ("frontier.cmd_path", frontier.get("cmd_path"))):
        if isinstance(value, str) and value and _cmd_routed(value) \
                and CMD_UNSAFE_RE.search(value):
            problems.append(
                f"{label}: a .cmd/.bat path containing spaces or shell metacharacters "
                f"cannot be routed through cmd.exe safely ({value!r}); point at the "
                f".js entry instead")
    for name in (config.get("discrimination") or {}).get("link_dirs") or []:
        if not isinstance(name, str) or not SAFE_VALUE_RE.match(name):
            problems.append(f"discrimination.link_dirs entry is invalid: {name!r}")
    for name in (config.get("sweep") or {}).get("ignore_names") or []:
        if not isinstance(name, str) or not SAFE_VALUE_RE.match(name):
            problems.append(f"sweep.ignore_names entry is invalid: {name!r}")
    problems.extend(_provider_problems(config.get("providers")))
    return problems


def _provider_problems(providers) -> list:
    """Shape problems inside ``providers`` (unknown keys are typos, not tolerated)."""
    if providers is None:
        return []
    if not isinstance(providers, dict):
        return ["providers must be a mapping of name -> {frontier: ..., worker: ...}"]
    problems = []
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            problems.append(f"providers.{name} must be a mapping")
            continue
        unknown = sorted(set(entry) - PROVIDER_KEYS)
        if unknown:
            problems.append(f"providers.{name} has unknown key(s) {unknown}; "
                            f"expected any of {sorted(PROVIDER_KEYS)}")
        block = entry.get("frontier")
        if block is not None:
            if not isinstance(block, dict):
                problems.append(f"providers.{name}.frontier must be a mapping")
            else:
                unknown = sorted(set(block) - FRONTIER_KEYS)
                if unknown:
                    problems.append(
                        f"providers.{name}.frontier has unknown key(s) {unknown}; "
                        f"frontier keys are {sorted(FRONTIER_KEYS)}")
        worker = entry.get("worker")
        if worker is not None and not isinstance(worker, str):
            problems.append(f"providers.{name}.worker must be a Pi model string")
    return problems


def _warn_literal_keys(data: dict) -> None:
    """Nudge configs toward ${ENV_VAR} for secrets. The value is never echoed.

    Runs on the raw file (before expansion), so an env reference is recognisable."""
    def check(label: str, value) -> None:
        if not isinstance(value, str) or not value.strip() or "${" in value:
            return
        if value.strip().lower() in KEY_PLACEHOLDERS:
            return
        warnings.warn(
            f"{label} holds a literal value; prefer ${{ENV_VAR}} so the secret stays out "
            "of the config file", UserWarning, stacklevel=3)

    frontier = data.get("frontier")
    if isinstance(frontier, dict):
        check("frontier.api_key", frontier.get("api_key"))
    providers = data.get("providers")
    if isinstance(providers, dict):
        for name, entry in providers.items():
            block = entry.get("frontier") if isinstance(entry, dict) else None
            if isinstance(block, dict):
                check(f"providers.{name}.frontier.api_key", block.get("api_key"))


def apply_providers(config: dict) -> dict:
    """Resolve ``role_providers`` selections into the frontier and worker settings.

    ``providers.<name>`` holds a ``frontier:`` block (backend/base_url/api_key/model/
    extra_body/...) and/or a ``worker:`` model string in Pi's ``provider/model`` form.
    ``role_providers.frontier`` / ``role_providers.worker`` select one by name; the
    selected block overrides the corresponding role defaults, so changing model or
    endpoint is a one-line edit and no prompt or code changes.
    """
    providers = config.get("providers") or {}
    roles = config.get("role_providers") or {}
    for role in ("frontier", "worker"):
        name = str(roles.get(role) or "").strip()
        if not name:
            continue
        if name not in providers:
            raise ValueError(
                f"role_providers.{role} names unknown provider {name!r}; known providers: "
                f"{sorted(providers) or '(none defined)'}")
        entry = providers[name] or {}
        if role == "frontier":
            block = entry.get("frontier")
            if not isinstance(block, dict) or not block:
                raise ValueError(
                    f"providers.{name}.frontier must be a non-empty mapping with the "
                    "backend/base_url/model fields for this role")
            config.setdefault("frontier", {}).update(block)
        else:
            model = str(entry.get("worker") or "").strip()
            if not model:
                raise ValueError(
                    f"providers.{name}.worker must be a Pi model string such as "
                    "'commandcode/deepseek/deepseek-v4-flash'")
            config.setdefault("worker", {})["model"] = model
    return config


def load_config(path: str | None = None) -> dict:
    """Load configuration, deep-merged over defaults, with env expansion."""
    config_path = resolve_config_path(path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    _warn_literal_keys(data)
    merged = apply_providers(_expand(_merge(DEFAULTS, data)))
    problems = validate_config(merged)
    if problems:
        raise ValueError("invalid configuration:\n  - " + "\n  - ".join(problems))
    return merged


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
