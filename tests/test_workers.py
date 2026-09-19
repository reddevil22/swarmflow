"""Trace analysis tests: spiral detection, agent_end detection, classification."""

import json

from swarmflow.workers import classify, scan_forbidden, scan_trace

CAP = 32768


def _write_trace(path, events):
    with open(path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def _message_end(text=None, out_tokens=100):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    return {"type": "message_end", "message": {"role": "assistant",
                                               "content": content,
                                               "usage": {"input": 100, "output": out_tokens}}}


def test_healthy_trace_is_delivered(tmp_path):
    trace = tmp_path / "ok.jsonl"
    _write_trace(trace, [
        {"type": "turn_end"},
        _message_end(text="All tests pass.", out_tokens=500),
        {"type": "agent_end"},
    ])
    scan = scan_trace(str(trace), CAP)
    assert scan["spiral"] is False
    assert scan["has_agent_end"] is True
    assert scan["turns"] == 1
    assert classify(scan, []) == "delivered"
    assert classify(scan, ["missing.py"]) == "missing_artifacts"


def test_spiral_detected_when_cap_consumed_without_text(tmp_path):
    trace = tmp_path / "spiral.jsonl"
    _write_trace(trace, [
        {"type": "turn_end"},
        _message_end(text=None, out_tokens=33000),
        {"type": "agent_end"},
    ])
    scan = scan_trace(str(trace), CAP)
    assert scan["spiral"] is True
    assert classify(scan, []) == "spiral"


def test_cap_consumed_but_text_present_is_not_spiral(tmp_path):
    """Boundary: many tokens with an actual answer is a long response, not a spiral."""
    trace = tmp_path / "long.jsonl"
    _write_trace(trace, [
        {"type": "turn_end"},
        _message_end(text="answer", out_tokens=31000),
        {"type": "agent_end"},
    ])
    scan = scan_trace(str(trace), CAP)
    assert scan["spiral"] is False
    assert classify(scan, []) == "delivered"


def test_trace_without_agent_end_is_not_delivered(tmp_path):
    trace = tmp_path / "killed.jsonl"
    _write_trace(trace, [{"type": "turn_end"}, _message_end(text="partial")])
    scan = scan_trace(str(trace), CAP)
    assert scan["has_agent_end"] is False
    assert classify(scan, []) == "no_agent_end"


def test_missing_trace_file_is_handled(tmp_path):
    scan = scan_trace(str(tmp_path / "nope.jsonl"), CAP)
    assert scan["exists"] is False
    assert classify(scan, []) == "no_agent_end"


def _trace_with_bash(tmp_path, command):
    trace = tmp_path / "t.jsonl"
    trace.write_text(json.dumps({
        "type": "message_end",
        "message": {"role": "assistant",
                    "content": [{"type": "toolCall", "name": "bash",
                                 "arguments": {"command": command}}]},
    }) + "\n", encoding="utf-8")
    return trace


def test_forbidden_actions_detected(tmp_path):
    for command in ["npm install", "pnpm install", "rm -rf node_modules", "npm ci",
                    "pip install requests", "poetry add rich", "cargo add serde",
                    "go get example.com/x"]:
        trace = _trace_with_bash(tmp_path, command)
        assert scan_forbidden(str(trace)), f"should flag: {command}"


def test_clean_commands_are_not_flagged(tmp_path):
    trace = _trace_with_bash(tmp_path, "npx jest src/domain/task.spec.ts")
    assert scan_forbidden(str(trace)) == []
