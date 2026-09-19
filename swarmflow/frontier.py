"""Frontier-model client: deepseek-v4-flash via Command Code headless mode.

Calls `cmd -p --output-format json` with the prompt on stdin (safe for long prompts)
and parses the NDJSON stream for the final {"type":"result"} line.
"""

import json
import os
import subprocess


class FrontierError(Exception):
    """Raised when the frontier model cannot be reached or its output is unusable."""


def parse_ndjson_result(text: str) -> dict | None:
    """Extract the last result frame from NDJSON headless output. None if absent."""
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


class FrontierClient:
    def __init__(self, cmd_path: str, model: str = "deepseek/deepseek-v4-flash",
                 effort: str | None = None, timeout_s: float = 300, cwd: str | None = None):
        self.cmd_path = cmd_path
        self.model = model
        self.effort = effort
        self.timeout_s = timeout_s
        self.cwd = cwd

    def call(self, prompt: str, *, max_turns: int = 1, model: str | None = None,
             effort: str | None = None, timeout: float | None = None) -> dict:
        """Run one headless call. Returns a normalized result dict.

        Keys: ok, subtype, final_text, usage, duration_ms, session_id, exit_code.
        Never raises for model-level failures; raises FrontierError for transport problems.
        """
        args = [
            os.environ.get("COMSPEC", "cmd.exe"), "/c", self.cmd_path,
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
                timeout=timeout or self.timeout_s, cwd=self.cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise FrontierError(f"frontier call timed out after {timeout or self.timeout_s}s") from exc
        except OSError as exc:
            raise FrontierError(f"could not launch frontier cli: {exc}") from exc

        frame = parse_ndjson_result(proc.stdout)
        out = {
            "ok": False,
            "subtype": None,
            "final_text": "",
            "usage": {},
            "duration_ms": None,
            "session_id": None,
            "exit_code": proc.returncode,
            "raw_tail": proc.stdout[-500:] if proc.stdout else "",
        }
        if frame:
            out["subtype"] = frame.get("subtype")
            out["final_text"] = frame.get("finalText") or ""
            out["usage"] = frame.get("usage") or {}
            out["duration_ms"] = frame.get("durationMs")
            out["session_id"] = frame.get("sessionId")
            out["ok"] = frame.get("subtype") == "success"
        return out

    def call_json(self, prompt: str, **kwargs) -> dict:
        """Like call(), but parse final_text as JSON (tolerating code fences)."""
        result = self.call(prompt, **kwargs)
        if not result["ok"]:
            raise FrontierError(
                f"frontier call failed: subtype={result['subtype']} exit={result['exit_code']}"
            )
        text = result["final_text"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text[: -3]
            text = text.strip()
            if text.startswith("json"):
                text = text[4:].strip()
        try:
            result["json"] = json.loads(text)
        except ValueError as exc:
            raise FrontierError(f"frontier returned non-JSON output: {text[:300]!r}") from exc
        return result
