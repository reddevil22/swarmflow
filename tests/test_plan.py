"""Plan validation tests: ownership disjointness is the load-bearing check."""

from swarmflow.plan import validate_plan


def _task(task_id, files):
    return {"id": task_id, "module": files[0], "owner_files": files,
            "spec": "requirements"}


def test_valid_plan_passes():
    plan = {"tasks": [_task("a", ["a.py", "test_a.py"]), _task("b", ["b.py"])]}
    assert validate_plan(plan) == []


def test_overlapping_ownership_is_rejected():
    plan = {"tasks": [_task("a", ["shared.py"]), _task("b", ["shared.py"])]}
    errors = validate_plan(plan)
    assert any("ownership overlap" in error for error in errors)


def test_duplicate_and_empty_owner_lists_are_rejected():
    plan = {"tasks": [_task("a", ["a.py"]), _task("a", ["b.py"]),
                      {"id": "c", "module": "c.py", "owner_files": [], "spec": "x"}]}
    errors = validate_plan(plan)
    assert any("duplicate id" in error for error in errors)
    assert any("owner_files is empty" in error for error in errors)


def test_missing_keys_and_no_tasks():
    assert validate_plan({"tasks": []}) == ["plan has no tasks"]
    errors = validate_plan({"tasks": [{"id": "a"}]})
    assert any("missing keys" in error for error in errors)


def _task_with_command(task_id, files, command, wave=1):
    task = _task(task_id, files)
    task["test_command"] = command
    task["wave"] = wave
    return task


def test_same_wave_tasks_cannot_share_a_test_command():
    """One change split into 'implement' + 'its tests' races inside a single wave."""
    plan = {"tasks": [_task_with_command("impl", ["a.py"], "pytest tests/test_a.py"),
                      _task_with_command("tests", ["test_a.py"], "pytest tests/test_a.py")]}
    errors = validate_plan(plan)
    assert any("must be independent" in error for error in errors)
    assert any("impl" in error and "tests" in error for error in errors)


def test_the_same_command_in_different_waves_is_allowed():
    plan = {"tasks": [_task_with_command("a", ["a.py"], "pytest -q", wave=1),
                      _task_with_command("b", ["b.py"], "pytest -q", wave=2)]}
    assert validate_plan(plan) == []


def test_distinct_commands_in_one_wave_are_allowed():
    plan = {"tasks": [_task_with_command("a", ["a.py"], "pytest tests/test_a.py"),
                      _task_with_command("b", ["b.py"], "pytest tests/test_b.py")]}
    assert validate_plan(plan) == []
