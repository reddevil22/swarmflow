"""Frontier backends: model access for planning, verification and acceptance.

Frontier roles are text-in / text-out completions (mostly structured JSON), so any of
the following can serve them:

- OpenAICompatBackend: any OpenAI-compatible ``/chat/completions`` endpoint
  (OpenRouter, DeepSeek API, Groq, Ollama, vLLM, LM Studio, ...) - stdlib only,
  no SDK dependency.
- CommandCodeBackend: the Command Code CLI in headless mode.
- GenericCliBackend: any terminal agent CLI via an argv template plus text/JSON
  output extraction.
- PiBackend: Pi itself driving a provider model configured in Pi.

Every backend exposes::

    complete(prompt, *, max_turns=1, model=None, effort=None, timeout=None) -> dict

returning normalized keys: ok, backend, subtype, final_text, usage, duration_ms,
session_id, exit_code. ``max_turns``/``effort`` are best-effort: single-shot HTTP
backends ignore them.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from . import __version__
from .config import build_cli_command, resolve_executable


class FrontierError(Exception):
    """Raised when no backend can serve a request or the transport fails."""


def parse_ndjson_result(text: str) -> dict | None:
    """Extract the last JSON frame with type == 'result' from NDJSON output."""
    result = None
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if isinstance(frame, dict) and frame.get("type") == "result":
            result = frame
    return result


def _empty_result(backend: str, exit_code=None, raw_tail: str = "") -> dict:
    return {
        "ok": False,
        "backend": backend,
        "subtype": None,
        "final_text": "",
        "usage": {},
        "duration_ms": None,
        "session_id": None,
        "exit_code": exit_code,
        "raw_tail": raw_tail,
    }


# --------------------------------------------------------------- openai-compatible

# Some gateways (the Command Code provider edge included) reject urllib's default
# User-Agent with a WAF 403/1010, so identify the client explicitly.
USER_AGENT = f"swarmflow/{__version__}"


def _urllib_transport(method, url, headers, body, timeout):
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        raise FrontierError(f"transport error for {url}: {exc}") from exc


class OpenAICompatBackend:
    """Minimal stdlib client for any OpenAI-compatible chat completions endpoint."""

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout_s: float = 300, system_prompt: str = "",
                 extra_body: dict | None = None, transport=None):
        if not base_url:
            raise FrontierError("openai backend requires frontier.base_url")
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.system_prompt = system_prompt
        self.extra_body = dict(extra_body or {})
        self._transport = transport or _urllib_transport

    def complete(self, prompt, *, max_turns=1, model=None, effort=None, timeout=None):
        started = time.time()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": prompt})
        body = {"model": model or self.model, "messages": messages, "temperature": 0}
        body.update(self.extra_body)
        url = self.base_url.rstrip("/") + "/chat/completions"
        status, text = self._transport("POST", url, headers,
                                       json.dumps(body).encode("utf-8"),
                                       timeout or self.timeout_s)
        result = _empty_result("openai", exit_code=status, raw_tail=text[-500:])
        result["duration_ms"] = int((time.time() - started) * 1000)
        try:
            payload = json.loads(text)
        except ValueError:
            return result
        try:
            message = payload["choices"][0]["message"]
            result["final_text"] = message.get("content") or ""
            usage = payload.get("usage") or {}
            result["usage"] = {"inputTokens": usage.get("prompt_tokens"),
                               "outputTokens": usage.get("completion_tokens")}
        except (KeyError, IndexError, TypeError):
            return result
        result["ok"] = status == 200
        return result


# ------------------------------------------------------------------- command code


class CommandCodeBackend:
    """Command Code CLI in headless mode (`cmd -p --output-format json`)."""

    def __init__(self, cmd_path: str, model: str, effort: str | None = None,
                 timeout_s: float = 300):
        self.cmd_path = cmd_path
        self.model = model
        self.effort = effort
        self.timeout_s = timeout_s

    def complete(self, prompt, *, max_turns=8, model=None, effort=None, timeout=None):
        # the CLI is an agent: it spends turns on tool calls before answering, so the
        # previous default of 1 ended real planning prompts with subtype "max_turns" and
        # no answer at all (the CLI's own default is 100)
        prefix = build_cli_command(self.cmd_path, "node")
        args = prefix + [
            "-p", "--output-format", "json", "--skip-onboarding", "-t",
            "--model", model or self.model,
            "--max-turns", str(max_turns),
        ]
        chosen_effort = effort or self.effort
        if chosen_effort:
            args += ["--effort", chosen_effort]
        try:
            proc = subprocess.run(
                args, input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout or self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise FrontierError(
                f"command code backend timed out after {timeout or self.timeout_s}s") from exc
        except OSError as exc:
            raise FrontierError(f"could not launch command code cli: {exc}") from exc

        result = _empty_result("commandcode", exit_code=proc.returncode,
                               raw_tail=(proc.stdout or "")[-500:])
        frame = parse_ndjson_result(proc.stdout or "")
        if frame:
            result["subtype"] = frame.get("subtype")
            result["final_text"] = frame.get("finalText") or ""
            result["usage"] = frame.get("usage") or {}
            result["duration_ms"] = frame.get("durationMs")
            result["session_id"] = frame.get("sessionId")
            result["ok"] = frame.get("subtype") == "success"
        return result


# -------------------------------------------------------------------- generic cli


class GenericCliBackend:
    """Adapter for any terminal agent CLI.

    ``command`` is an argv template. Tokens may contain ``{prompt_file}`` (a temp file
    holding the prompt) or ``{prompt}`` (inline substitution). ``output`` is ``text``
    (stdout is the answer) or ``json`` (stdout parsed as JSON; optionally select a
    dotted ``result_path``; a final NDJSON ``{"type": "result", ...}`` frame is also
    accepted).
    """

    def __init__(self, command: list, output: str = "text", result_path: str = "",
                 timeout_s: float = 300):
        if not command:
            raise FrontierError("cli backend requires frontier.command")
        self.command = list(command)
        self.output = output
        self.result_path = result_path
        self.timeout_s = timeout_s

    def complete(self, prompt, *, max_turns=1, model=None, effort=None, timeout=None):
        started = time.time()
        needs_file = any("{prompt_file}" in token for token in self.command)
        prompt_file = ""
        if needs_file:
            fd, prompt_file = tempfile.mkstemp(prefix="swarmflow_prompt_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(prompt)
        try:
            argv = [token.replace("{prompt_file}", prompt_file).replace("{prompt}", prompt)
                    for token in self.command]
            try:
                proc = subprocess.run(argv, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace",
                                      timeout=timeout or self.timeout_s)
            except subprocess.TimeoutExpired as exc:
                raise FrontierError(
                    f"cli backend timed out after {timeout or self.timeout_s}s") from exc
            except OSError as exc:
                raise FrontierError(f"could not launch cli backend: {exc}") from exc
        finally:
            if prompt_file:
                try:
                    os.unlink(prompt_file)
                except OSError:
                    pass

        result = _empty_result("cli", exit_code=proc.returncode,
                               raw_tail=(proc.stdout or "")[-500:])
        result["duration_ms"] = int((time.time() - started) * 1000)
        text = proc.stdout or ""
        if self.output == "json":
            try:
                data = json.loads(text)
            except ValueError:
                data = parse_ndjson_result(text)
            if data is None:
                return result
            value = data
            if self.result_path:
                for part in self.result_path.split("."):
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    else:
                        value = ""
                        break
            result["final_text"] = value if isinstance(value, str) else json.dumps(value)
        else:
            result["final_text"] = text
        result["ok"] = proc.returncode == 0 and bool(result["final_text"].strip())
        return result


# ---------------------------------------------------------------------------- pi


def _pi_final_text(trace_path: str) -> dict:
    """Extract the final assistant text and usage from a Pi --mode json trace."""
    events = []
    for line in open(trace_path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    out = {"has_agent_end": False, "text": "", "out_tokens": 0}
    for event in events:
        if event.get("type") == "message_end":
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            out["out_tokens"] += usage.get("output") or 0
        elif event.get("type") == "agent_end":
            out["has_agent_end"] = True
            for message in event.get("messages", []):
                if message.get("role") == "assistant":
                    for item in message.get("content") or []:
                        if isinstance(item, dict) and item.get("type") == "text" \
                                and item.get("text"):
                            out["text"] = item["text"]
    return out


class PiBackend:
    """Pi itself as the frontier runner, using a provider model configured in Pi."""

    def __init__(self, cli: str, model: str, node: str = "node", thinking: str = "",
                 timeout_s: float = 300):
        self.cli = cli
        self.model = model
        self.node = node
        self.thinking = thinking
        self.timeout_s = timeout_s

    def complete(self, prompt, *, max_turns=1, model=None, effort=None, timeout=None):
        prefix = build_cli_command(self.cli, self.node)
        cmd = prefix + ["-p", "--mode", "json", "--no-session", "--model", model or self.model]
        if self.thinking:
            cmd += ["--thinking", self.thinking]
        fd, trace_path = tempfile.mkstemp(prefix="swarmflow_pi_frontier_", suffix=".jsonl")
        try:
            with os.fdopen(fd, "wb") as out:
                try:
                    proc = subprocess.run(cmd, input=prompt.encode("utf-8"), stdout=out,
                                          stderr=subprocess.STDOUT,
                                          timeout=timeout or self.timeout_s)
                except subprocess.TimeoutExpired as exc:
                    raise FrontierError(
                        f"pi backend timed out after {timeout or self.timeout_s}s") from exc
                except OSError as exc:
                    raise FrontierError(f"could not launch pi backend: {exc}") from exc
            scan = _pi_final_text(trace_path)
        finally:
            try:
                os.unlink(trace_path)
            except OSError:
                pass
        result = _empty_result("pi", exit_code=proc.returncode)
        result["final_text"] = scan["text"]
        result["usage"] = {"outputTokens": scan["out_tokens"]}
        result["ok"] = scan["has_agent_end"] and bool(scan["text"].strip())
        return result


# ------------------------------------------------------------------------ factory


def build_backend(frontier_cfg: dict):
    """Create the configured frontier backend.

    ``backend: auto`` (default) resolves deterministically: an explicit
    ``frontier.command`` wins, then ``frontier.base_url`` (OpenAI-compatible), then a
    configured or PATH-discoverable Command Code CLI.
    """
    kind = (frontier_cfg.get("backend") or "auto").lower()
    if kind == "auto":
        if frontier_cfg.get("command"):
            kind = "cli"
        elif frontier_cfg.get("base_url"):
            kind = "openai"
        elif frontier_cfg.get("cmd_path"):
            kind = "commandcode"
        elif shutil.which("commandcode") or shutil.which("cmdc"):
            kind = "commandcode"
        else:
            raise FrontierError(
                "no frontier backend available: set frontier.base_url (+ api_key) for an "
                "OpenAI-compatible API, set frontier.command for a custom CLI agent, or "
                "install the Command Code CLI"
            )

    timeout_s = float(frontier_cfg.get("timeout_s", 300))
    if kind == "openai":
        return OpenAICompatBackend(
            base_url=frontier_cfg.get("base_url", ""),
            model=frontier_cfg.get("model", ""),
            api_key=frontier_cfg.get("api_key", ""),
            timeout_s=timeout_s,
            system_prompt=frontier_cfg.get("system_prompt", ""),
            extra_body=frontier_cfg.get("extra_body") or {},
        )
    if kind == "commandcode":
        cmd_path = frontier_cfg.get("cmd_path") or resolve_executable(["commandcode", "cmdc"])
        return CommandCodeBackend(cmd_path, frontier_cfg.get("model", ""),
                                  frontier_cfg.get("effort"), timeout_s)
    if kind == "cli":
        return GenericCliBackend(
            command=frontier_cfg.get("command") or [],
            output=frontier_cfg.get("output", "text"),
            result_path=frontier_cfg.get("result_path", ""),
            timeout_s=timeout_s,
        )
    if kind == "pi":
        cli = frontier_cfg.get("pi_cli") or resolve_executable(["pi"])
        return PiBackend(cli, frontier_cfg.get("model", ""),
                         node=frontier_cfg.get("node", "node"),
                         thinking=frontier_cfg.get("thinking", ""),
                         timeout_s=timeout_s)
    raise FrontierError(f"unknown frontier backend: {kind}")
