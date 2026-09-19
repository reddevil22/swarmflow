"""Control-plane state store: location, atomicity, guarded reads, adoption."""

import json

from swarmflow import runstate


def test_store_lives_outside_the_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    directory = runstate.store_dir(str(project))
    assert directory.is_dir()
    assert str(project.resolve()) not in str(directory)
    assert directory == runstate.store_dir(str(project))   # stable for the path


def test_roundtrip_and_leftover_tmp_is_harmless(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    runstate.save_run(str(project), {"mode": "brownfield", "base_sha": "abc"})
    assert runstate.load_run(str(project))["base_sha"] == "abc"
    leftover = runstate.run_path(str(project)).with_suffix(".json.tmp")
    leftover.write_text("{not json", encoding="utf-8")
    assert runstate.load_run(str(project))["mode"] == "brownfield"
    assert runstate.store_dir(str(project)).joinpath("run.json").exists()


def test_corrupt_store_reads_are_guarded(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    runstate.run_path(str(project)).write_text("{broken", encoding="utf-8")
    assert runstate.load_run(str(project)) == {}
    runstate.frozen_path(str(project)).write_text("{broken", encoding="utf-8")
    assert runstate.load_frozen(str(project)) is None


def test_adoption_imports_structural_fields_only(tmp_path):
    project = tmp_path / "proj"
    (project / ".swarmflow").mkdir(parents=True)
    (project / ".swarmflow" / "run.json").write_text(json.dumps({
        "mode": "brownfield", "branch": "swarmflow/x", "base_sha": "deadbeef",
        "created_at": "2026-01-01", "plan_path": "p.yaml",
        "audit_ignores": ["*"], "regression": {"baseline": {"rc": 0}},
        "regression_command": "hostile --payload",
    }), encoding="utf-8")

    adopted = runstate.adopt_legacy(str(project))
    stored = runstate.load_run(str(project))

    assert "mode" in adopted and stored["base_sha"] == "deadbeef"
    assert stored.get("adopted") is True
    assert "audit_ignores" not in stored
    assert "regression" not in stored
    assert "regression_command" not in stored
    assert runstate.adopt_legacy(str(project)) == []          # only once


def test_adoption_refuses_mirrors_and_greenfield(tmp_path):
    project = tmp_path / "proj"
    (project / ".swarmflow").mkdir(parents=True)
    (project / ".swarmflow" / "run.json").write_text(json.dumps(
        {"mode": "brownfield", "base_sha": "x", "_mirror": "informational only"}),
        encoding="utf-8")
    assert runstate.adopt_legacy(str(project)) == []
    assert runstate.load_run(str(project)) == {}

    (project / ".swarmflow" / "run.json").write_text(json.dumps({"mode": "greenfield"}),
                                                     encoding="utf-8")
    assert runstate.adopt_legacy(str(project)) == []


def test_mirror_is_marked_and_never_authoritative(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    runstate.save_run(str(project), {"mode": "brownfield", "base_sha": "abc1234567"})
    runstate.write_mirror(str(project), {"mode": "brownfield", "base_sha": "abc1234567"})
    mirror = json.loads((project / ".swarmflow" / "run.json").read_text(encoding="utf-8"))
    assert "_mirror" in mirror
    assert mirror["base_sha"] == "abc1234567"
