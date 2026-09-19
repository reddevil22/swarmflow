"""Scope audit tests: frozen-file integrity and stray detection."""

from swarmflow.audit import audit, freeze


def _project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "existing.py").write_text("print('hi')\n")
    (tmp_path / "package.json").write_text('{"name": "x"}\n')
    return tmp_path


def test_clean_project_passes(tmp_path):
    _project(tmp_path)
    freeze(str(tmp_path), {"T1": ["src/new.py", "tests/test_new.py"]})
    assert audit(str(tmp_path))["ok"] is True


def test_modified_frozen_file_detected(tmp_path):
    _project(tmp_path)
    freeze(str(tmp_path), {})
    (tmp_path / "package.json").write_text('{"name": "y"}\n')
    result = audit(str(tmp_path))
    assert not result["ok"]
    assert any(v["kind"] == "modified_frozen" and v["path"] == "package.json"
               for v in result["violations"])


def test_deleted_frozen_file_detected(tmp_path):
    _project(tmp_path)
    freeze(str(tmp_path), {})
    (tmp_path / "package.json").unlink()
    result = audit(str(tmp_path))
    assert any(v["kind"] == "deleted_frozen" for v in result["violations"])


def test_added_unowned_detected_but_owned_allowed(tmp_path):
    _project(tmp_path)
    freeze(str(tmp_path), {"T1": ["src/new.py"]})
    (tmp_path / "stray.txt").write_text("oops")
    (tmp_path / "src" / "new.py").write_text("ok")
    result = audit(str(tmp_path))
    violations = {(v["kind"], v["path"]) for v in result["violations"]}
    assert ("added_unowned", "stray.txt") in violations
    assert not any(path == "src/new.py" for _, path in violations)


def test_owned_files_exempt_from_frozen_changes(tmp_path):
    _project(tmp_path)
    (tmp_path / "src" / "mod.py").write_text("v1")
    freeze(str(tmp_path), {"T1": ["src/mod.py"]})
    (tmp_path / "src" / "mod.py").write_text("v2")
    assert audit(str(tmp_path))["ok"] is True


def test_ignored_directories_are_skipped(tmp_path):
    _project(tmp_path)
    freeze(str(tmp_path), {})
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x")
    assert audit(str(tmp_path))["ok"] is True


def test_no_baseline_reports_missing(tmp_path):
    _project(tmp_path)
    result = audit(str(tmp_path))
    assert [v["kind"] for v in result["violations"]] == ["no_baseline"]
