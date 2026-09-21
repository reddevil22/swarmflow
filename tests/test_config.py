"""Config portability tests: env expansion, path resolution, CLI command shaping."""

import os

import pytest

from swarmflow.config import (EXAMPLE_CONFIG_PATH, build_cli_command, load_config,
                              resolve_executable)


def test_example_config_loads_and_merges_defaults(tmp_path):
    config = load_config(str(EXAMPLE_CONFIG_PATH))
    assert config["frontier"]["model"] == "deepseek/deepseek-v4-flash"
    assert config["worker"]["max_turns"] == 45
    assert config["paths"]["ledger"] == ""      # resolved to the user state dir later


def test_partial_config_merges_with_defaults(tmp_path):
    partial = tmp_path / "c.yaml"
    partial.write_text("worker:\n  model: local/test\n", encoding="utf-8")
    config = load_config(str(partial))
    assert config["worker"]["model"] == "local/test"
    assert config["worker"]["retry_thinking"] == "medium"   # default preserved
    assert config["worker"]["poll_s"] == 15.0               # supervisor defaults present
    assert config["swarm"]["concurrency"] == 8


def test_env_expansion_in_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMFLOW_TEST_MODEL", "local/expanded")
    config_path = tmp_path / "c.yaml"
    config_path.write_text('worker:\n  model: "${SWARMFLOW_TEST_MODEL}"\n',
                           encoding="utf-8")
    assert load_config(str(config_path))["worker"]["model"] == "local/expanded"


def test_missing_config_raises_with_guidance(tmp_path):
    with pytest.raises(FileNotFoundError, match="swarmflow init"):
        load_config(str(tmp_path / "does-not-exist.yaml"))


def test_build_cli_command_shapes():
    assert build_cli_command("C:/tools/pi.cmd")[0] == os.environ.get("COMSPEC", "cmd.exe")
    assert build_cli_command("C:/tools/cli.js")[0] == "node"
    assert build_cli_command("/usr/local/bin/pi") == ["/usr/local/bin/pi"]


def test_resolve_executable_reports_missing_tools():
    with pytest.raises(FileNotFoundError, match="none of"):
        resolve_executable(["definitely-not-a-real-tool-abcxyz"])
