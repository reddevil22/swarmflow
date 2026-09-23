"""Packaging: shipped assets resolve inside the package, state stays user-writable."""

from pathlib import Path

from swarmflow import resources
from swarmflow.config import (DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH, LEGACY_CONFIG_PATH,
                              load_config, resolve_paths)


def test_every_shipped_asset_resolves():
    for parts in (("prompts", "planner.md"), ("prompts", "verifier.md"),
                  ("prompts", "acceptance.md"), ("prompts", "task_brief.md"),
                  ("AGENTS.worker.md",), ("config", "swarmflow.example.yaml")):
        path = resources.asset(*parts)
        assert path.is_file(), parts
        assert path.read_text(encoding="utf-8").strip()


def test_assets_live_inside_the_package():
    """A wheel install must be able to find them; the source tree layout cannot leak."""
    package_root = Path(resources.__file__).resolve().parent
    assert resources.asset("prompts", "planner.md").is_relative_to(package_root)


def test_example_config_loads_over_defaults():
    config = load_config(str(EXAMPLE_CONFIG_PATH))
    assert config["worker"]["model"] == ""
    assert config["frontier"]["backend"] == "auto"


def test_state_and_ledger_default_to_a_user_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARMFLOW_STATE_DIR", str(tmp_path / "state"))
    config = resolve_paths({"paths": {}})
    assert config["paths"]["state_dir"] == str(tmp_path / "state")
    assert config["paths"]["ledger"] == str(tmp_path / "state" / "ledger.db")


def test_explicit_paths_win_and_relative_resolves_against_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARMFLOW_STATE_DIR", str(tmp_path / "ignored"))
    monkeypatch.chdir(tmp_path)
    config = resolve_paths({"paths": {"state_dir": "local-state", "ledger": "custom.db"}})
    assert config["paths"]["state_dir"] == str(tmp_path / "local-state")
    assert config["paths"]["ledger"] == str(tmp_path / "custom.db")


def test_the_user_config_path_is_not_the_checkout_config():
    assert DEFAULT_CONFIG_PATH != LEGACY_CONFIG_PATH
