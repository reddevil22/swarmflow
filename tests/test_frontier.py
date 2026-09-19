"""Frontier NDJSON parsing tests (no network calls)."""

import json

from swarmflow.frontier import parse_ndjson_result


def test_result_frame_is_extracted():
    text = "\n".join([
        json.dumps({"type": "event", "event": {"type": "tool_running"}}),
        json.dumps({"type": "event", "event": {"type": "agent_end"}}),
        json.dumps({"type": "result", "subtype": "success", "finalText": "hello",
                    "usage": {"outputTokens": 5}}),
    ])
    frame = parse_ndjson_result(text)
    assert frame is not None
    assert frame["subtype"] == "success"
    assert frame["finalText"] == "hello"


def test_last_result_wins():
    text = "\n".join([
        json.dumps({"type": "result", "subtype": "error"}),
        json.dumps({"type": "result", "subtype": "success", "finalText": "final"}),
    ])
    assert parse_ndjson_result(text)["finalText"] == "final"


def test_garbage_lines_are_skipped():
    text = "not json at all\n{\"broken\": \n" + json.dumps(
        {"type": "result", "subtype": "success", "finalText": "ok"})
    assert parse_ndjson_result(text)["finalText"] == "ok"


def test_no_result_frame_returns_none():
    text = json.dumps({"type": "event", "event": {"type": "start"}})
    assert parse_ndjson_result(text) is None
