"""End-to-end: the provider selection decides where frontier calls actually land.

A stub OpenAI-compatible endpoint runs on loopback; two providers point at sibling
routes on it. Selecting one, then the other, must move the traffic (and the model id)
without touching any other config key.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from swarmflow import cli


class _Stub(BaseHTTPRequestHandler):
    hits: list = []
    auths: list = []

    def do_POST(self):                                      # noqa: N802 - http hook
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        _Stub.hits.append(self.path)
        _Stub.auths.append(self.headers.get("Authorization") or "")
        body = json.dumps({
            "model": "stub",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "frontier-ok"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):                            # keep pytest output clean
        return


@pytest.fixture()
def stub_endpoint():
    _Stub.hits = []
    _Stub.auths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _config(tmp_path, base, selected):
    config = {
        "providers": {
            "alpha": {"frontier": {"backend": "openai", "base_url": f"{base}/alpha/v1",
                                   "model": "stub-alpha", "api_key": "dummy",
                                   "timeout_s": 5}},
            "beta": {"frontier": {"backend": "openai", "base_url": f"{base}/beta/v1",
                                  "model": "stub-beta", "api_key": "dummy",
                                  "timeout_s": 5}},
        },
        "role_providers": {"frontier": selected},
        "paths": {"ledger": str(tmp_path / "ledger.db"),
                  "state_dir": str(tmp_path / "state")},
    }
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_selection_routes_frontier_traffic(tmp_path, stub_endpoint, capsys):
    assert cli.main(["--config", str(_config(tmp_path, stub_endpoint, "alpha")),
                     "smoke-frontier", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["provider"] == "alpha"
    assert payload["model"] == "stub-alpha"
    assert _Stub.hits == ["/alpha/v1/chat/completions"]
    assert _Stub.auths == ["Bearer dummy"]


def test_switching_the_selection_switches_the_endpoint(tmp_path, stub_endpoint, capsys):
    assert cli.main(["--config", str(_config(tmp_path, stub_endpoint, "alpha")),
                     "smoke-frontier", "--json"]) == 0
    capsys.readouterr()
    assert _Stub.hits == ["/alpha/v1/chat/completions"]

    assert cli.main(["--config", str(_config(tmp_path, stub_endpoint, "beta")),
                     "smoke-frontier", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["provider"], payload["model"]) == ("beta", "stub-beta")
    assert _Stub.hits == ["/alpha/v1/chat/completions", "/beta/v1/chat/completions"]
    assert _Stub.auths[1] == "Bearer dummy"
