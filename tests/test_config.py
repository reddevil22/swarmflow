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


def _provider_config(tmp_path, frontier_role="commandcode", worker_role="commandcode",
                     api_key=""):
    key_line = f'      api_key: "{api_key}"\n' if api_key else ""
    text = (
        "providers:\n"
        "  commandcode:\n"
        "    frontier:\n"
        "      backend: openai\n"
        '      base_url: "https://api.commandcode.ai/provider/v1"\n'
        "      model: deepseek/deepseek-v4-flash\n"
        + key_line +
        '    worker: "commandcode/deepseek/deepseek-v4-flash"\n'
        "  local:\n"
        "    frontier:\n"
        "      backend: openai\n"
        '      base_url: "http://127.0.0.1:8000/v1"\n'
        "      model: qwen3.8-flash-next\n"
        '    worker: "local/qwen3.8-flash-next"\n'
        f"role_providers: {{frontier: {frontier_role}, worker: {worker_role}}}\n"
    )
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_named_provider_swaps_both_roles(tmp_path):
    config = load_config(_provider_config(tmp_path))
    assert config["frontier"]["backend"] == "openai"
    assert config["frontier"]["base_url"] == "https://api.commandcode.ai/provider/v1"
    assert config["frontier"]["model"] == "deepseek/deepseek-v4-flash"
    assert config["worker"]["model"] == "commandcode/deepseek/deepseek-v4-flash"


def test_switching_the_selection_switches_the_models(tmp_path):
    config = load_config(_provider_config(tmp_path, frontier_role="local",
                                          worker_role="local"))
    assert config["frontier"]["base_url"] == "http://127.0.0.1:8000/v1"
    assert config["frontier"]["model"] == "qwen3.8-flash-next"
    assert config["worker"]["model"] == "local/qwen3.8-flash-next"


def test_provider_api_keys_expand_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMFLOW_TEST_KEY", "sk-test-value")
    config = load_config(_provider_config(tmp_path, api_key="${SWARMFLOW_TEST_KEY}"))
    assert config["frontier"]["api_key"] == "sk-test-value"


def test_unknown_or_incomplete_provider_selections_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown provider"):
        load_config(_provider_config(tmp_path, frontier_role="nope"))
    path = tmp_path / "bad.yaml"
    path.write_text("providers:\n  p:\n    worker: \"x/y\"\n"
                    "role_providers: {frontier: p}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="providers.p.frontier"):
        load_config(str(path))


def test_build_cli_command_shapes():
    assert build_cli_command("C:/tools/pi.cmd")[0] == os.environ.get("COMSPEC", "cmd.exe")
    assert build_cli_command("C:/tools/cli.js")[0] == "node"
    assert build_cli_command("/usr/local/bin/pi") == ["/usr/local/bin/pi"]


def test_resolve_executable_reports_missing_tools():
    with pytest.raises(FileNotFoundError, match="none of"):
        resolve_executable(["definitely-not-a-real-tool-abcxyz"])
