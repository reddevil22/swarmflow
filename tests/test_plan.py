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
