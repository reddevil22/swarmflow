"""Configuration loading for swarmflow."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "swarmflow.yaml"

DEFAULTS = {
    "frontier": {
        "cmd_path": "",
        "model": "deepseek/deepseek-v4-flash",
        "effort": None,
        "timeout_s": 300,
    },
    "worker": {
        "node": "node",
        "pi_cli": "",
        "model": "vllm-79/qwen36",
        "thinking": "high",
        "retry_thinking": "medium",
        "timeout_s": 2400,
        "max_output_tokens": 32768,
    },
    "swarm": {
        "concurrency": 8,
        "stagger_s": 2.0,
        "metrics_url": "http://192.168.1.79:8000/metrics",
        "backpressure_waiting": 1,
        "backpressure_kv": 0.55,
    },
    "paths": {
        "logs_dir": "logs",
        "ledger": "state/ledger.db",
    },
}


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | None = None) -> dict:
    """Load configuration, deep-merged over defaults."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data = {}
    if config_path.exists():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return _merge(DEFAULTS, data)
