"""Ledger tests: state transitions, event trail, filtering, re-enqueue safety."""

from swarmflow.ledger import Ledger


def test_add_and_get(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    assert ledger.add_task("t1", "proj", wave=1, module="m.py",
                           owner_files=["m.py"], acceptance=["tests pass"],
                           spec_path="s.md") is True
    assert ledger.add_task("t1", "proj") is False  # duplicate id ignored
    task = ledger.get("t1")
    assert task["status"] == "queued"
    assert task["owner_files"] == ["m.py"]
    assert task["acceptance"] == ["tests pass"]
    assert task["attempts"] == 0
    ledger.close()


def test_status_transitions_validate_and_record_events(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.add_task("t1", "proj")
    ledger.set_status("t1", "delivered", artifacts=["a.py"], verdict='{"outcome": "delivered"}')
    task = ledger.get("t1")
    assert task["status"] == "delivered"
    assert task["artifacts"] == ["a.py"]
    try:
        ledger.set_status("t1", "not-a-status")
        raise AssertionError("should have raised for unknown status")
    except ValueError:
        pass
    try:
        ledger.set_status("t1", "failed", nonsense_field="x")
        raise AssertionError("should have raised for unknown field")
    except ValueError:
        pass
    kinds = [event["kind"] for event in ledger.events("t1")]
    assert "enqueued" in kinds
    assert "status:delivered" in kinds
    ledger.close()


def test_counts_and_wave_filter(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.add_task("a", "p", wave=1)
    ledger.add_task("b", "p", wave=1)
    ledger.add_task("c", "p", wave=2)
    ledger.set_status("a", "delivered")
    assert ledger.counts() == {"delivered": 1, "queued": 2}
    wave2 = ledger.list_tasks(wave=2)
    assert [task["id"] for task in wave2] == ["c"]
    ledger.close()


def test_bump_attempts(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.add_task("t1", "p")
    assert ledger.bump_attempts("t1") == 1
    assert ledger.bump_attempts("t1") == 2
    assert ledger.get("t1")["attempts"] == 2
    ledger.close()
