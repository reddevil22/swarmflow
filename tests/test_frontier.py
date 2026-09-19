"""Frontier backend tests: NDJSON parsing, factory selection, request/response shaping.

No network: the OpenAI-compatible backend uses an injected transport, and the CLI
backend runs the local Python interpreter.
"""

import json
import sys

import pytest

from swarmflow.frontier import (CommandCodeBackend, FrontierError, GenericCliBackend,
                                OpenAICompatBackend, PiBackend, build_backend,
                                parse_ndjson_result)


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


# ---------------------------------------------------------------- backend factory


def test_auto_prefers_explicit_configuration():
    assert isinstance(
        build_backend({"backend": "auto", "command": ["x"], "model": "m"}),
        GenericCliBackend)
    assert isinstance(
        build_backend({"backend": "auto", "base_url": "http://x/v1", "model": "m"}),
        OpenAICompatBackend)
    assert isinstance(
        build_backend({"backend": "auto", "cmd_path": "C:/tools/commandcode.cmd",
                       "model": "m"}),
        CommandCodeBackend)


def test_unknown_backend_raises():
    with pytest.raises(FrontierError, match="unknown frontier backend"):
        build_backend({"backend": "wat"})


def test_pi_backend_constructs_via_factory():
    backend = build_backend({"backend": "pi", "pi_cli": "pi", "model": "m"})
    assert isinstance(backend, PiBackend)


# ------------------------------------------------------------- openai-compatible


def test_openai_backend_request_and_response_shaping():
    captured = {}

    def transport(method, url, headers, body, timeout):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(body)
        return 200, json.dumps({
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        })

    backend = OpenAICompatBackend("http://x/v1", "m", api_key="k",
                                  system_prompt="sys", transport=transport)
    result = backend.complete("hi")
    assert result["ok"] is True
    assert result["backend"] == "openai"
    assert result["final_text"] == "hello"
    assert result["usage"] == {"inputTokens": 5, "outputTokens": 2}
    assert captured["url"] == "http://x/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert captured["body"]["temperature"] == 0


def test_openai_backend_merges_extra_body():
    captured = {}

    def transport(method, url, headers, body, timeout):
        captured["body"] = json.loads(body)
        return 200, json.dumps({"choices": [{"message": {"content": "x"}}]})

    backend = OpenAICompatBackend("http://x/v1", "m", transport=transport,
                                  extra_body={"chat_template_kwargs":
                                              {"enable_thinking": False}})
    backend.complete("hi")
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_openai_backend_http_error_is_not_ok():
    backend = OpenAICompatBackend("http://x/v1", "m",
                                  transport=lambda *a, **k: (500, "boom"))
    result = backend.complete("hi")
    assert result["ok"] is False
    assert result["exit_code"] == 500


# -------------------------------------------------------------------- cli backend


def test_cli_backend_text_mode():
    backend = GenericCliBackend([sys.executable, "-c", "print('cli-ok')"])
    result = backend.complete("ignored")
    assert result["ok"] is True
    assert result["final_text"].strip() == "cli-ok"


def test_cli_backend_json_result_path():
    code = "import json; print(json.dumps({'result': {'finalText': 'json-ok'}}))"
    backend = GenericCliBackend([sys.executable, "-c", code],
                                output="json", result_path="result.finalText")
    result = backend.complete("x")
    assert result["ok"] is True
    assert result["final_text"] == "json-ok"


def test_cli_backend_receives_prompt_file():
    code = "import sys; print(open(sys.argv[1], encoding='utf-8').read().strip())"
    backend = GenericCliBackend([sys.executable, "-c", code, "{prompt_file}"])
    result = backend.complete("prompt-payload")
    assert result["final_text"].strip() == "prompt-payload"


def test_cli_backend_nonzero_exit_is_not_ok():
    backend = GenericCliBackend([sys.executable, "-c", "raise SystemExit(3)"])
    result = backend.complete("x")
    assert result["ok"] is False
    assert result["exit_code"] == 3
