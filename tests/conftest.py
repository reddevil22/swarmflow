"""Shared test fixtures."""

import pytest

from swarmflow import runstate


@pytest.fixture(autouse=True)
def isolated_state_root(tmp_path, monkeypatch):
    """Keep every test's control-plane state inside its own tmp dir."""
    runstate.set_state_root(tmp_path / "state")
    yield
