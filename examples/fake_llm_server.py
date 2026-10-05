#!/usr/bin/env python3
"""A tiny OpenAI-compatible server that replays a scripted conversation.

This exists so AgentLite can be tried end to end - and tested end to end -
without an API key or network access. It is NOT a language model; it walks a
fixed script of tool calls, exactly like a real model would.

Run it:

    python examples/fake_llm_server.py --port 8100

Then point AgentLite at it:

    agentlite run "inspect the workspace" \\
      --provider openai-compatible  # with provider.base_url=http://localhost:8100/v1

Scenarios:

    demo       list the workspace, run `ls -la`, write report.md, summarise
    dangerous  ask for `sudo rm -rf /` (shows the permission system refusing)
    confirm    ask for `rm notes.txt` (shows the confirmation round-trip)
    timeout    ask for `sleep 30` with timeout=1 (shows timeout handling)
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import urlparse

# --------------------------------------------------------------------------- #
# Scripts
# --------------------------------------------------------------------------- #

DEMO_SCRIPT: List[Dict[str, Any]] = [
    {"tool_calls": [("filesystem.list", {"path": "."})]},
    {"tool_calls": [("terminal.run", {"command": "ls -la"})]},
    {"write_report": True},
    {"content": "Done. I listed the workspace, ran `ls -la` and wrote report.md."},
]

DANGEROUS_SCRIPT: List[Dict[str, Any]] = [
    {"tool_calls": [("terminal.run", {"command": "sudo rm -rf /etc"})]},
    {
        "content": (
            "I tried to run `sudo rm -rf /etc` but AgentLite refused it: "
            "the command matches a deny rule. I did not attempt to work around it."
        )
    },
]

CONFIRM_SCRIPT: List[Dict[str, Any]] = [
    {"tool_calls": [("terminal.run", {"command": "rm notes.txt"})]},
    {"content": "Removed notes.txt."},
]

TIMEOUT_SCRIPT: List[Dict[str, Any]] = [
    {"tool_calls": [("terminal.run", {"command": "sleep 30", "timeout": 1})]},
    {"content": "The command timed out, so I stopped."},
]

SCENARIOS = {
    "demo": DEMO_SCRIPT,
    "dangerous": DANGEROUS_SCRIPT,
    "confirm": CONFIRM_SCRIPT,
    "timeout": TIMEOUT_SCRIPT,
}


def last_tool_output(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "tool":
            return str(message.get("content") or "")
    return ""


def next_step(messages: List[Dict[str, Any]], script: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Advance one step for every tool result the conversation already contains."""
    completed = sum(1 for message in messages if message.get("role") == "tool")
    return script[min(completed, len(script) - 1)]


def build_assistant_message(
    messages: List[Dict[str, Any]], step: Dict[str, Any], index: int
) -> Dict[str, Any]:
    if "content" in step:
        return {"role": "assistant", "content": step["content"]}

    if step.get("write_report"):
        output = last_tool_output(messages)
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{index}_0",
                    "type": "function",
                    "function": {
                        "name": "filesystem_write",
                        "arguments": json.dumps(
                            {
                                "path": "report.md",
                                "content": f"# Workspace report\n\n```\n{output[:2000]}\n```\n",
                            }
                        ),
                    },
                }
            ],
        }

    calls = []
    for position, (name, arguments) in enumerate(step.get("tool_calls") or []):
        calls.append(
            {
                "id": f"call_{index}_{position}",
                "type": "function",
                # Underscores on purpose: AgentLite must map them back to dots.
                "function": {"name": name.replace(".", "_"), "arguments": json.dumps(arguments)},
            }
        )
    return {"role": "assistant", "content": None, "tool_calls": calls}


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #


class FakeOpenAIHandler(BaseHTTPRequestHandler):
    script: List[Dict[str, Any]] = DEMO_SCRIPT
    requests: List[Dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # keep the demo output clean

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path in ("/healthz", "/health"):
            self._send(200, {"status": "ok", "service": "fake-openai"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send(404, {"error": f"unknown endpoint {path}"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid JSON"})
            return

        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self._send(
                401,
                {"error": {"message": "missing Authorization header", "type": "auth_error"}},
            )
            return

        messages = payload.get("messages") or []
        # The scenario can be selected per-server (start_fake_server) or
        # per-request (X-Scenario header, handy for demos with curl).
        scenario = self.headers.get("X-Scenario")
        script = SCENARIOS[scenario] if scenario in SCENARIOS else self.script
        type(self).requests.append({"messages": len(messages), "tools": payload.get("tools")})

        step = next_step(messages, script)
        assistant = build_assistant_message(messages, step, len(messages))
        finish = "tool_calls" if assistant.get("tool_calls") else "stop"
        self._send(
            200,
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 1700000000,
                "model": payload.get("model", "fake-model"),
                "choices": [{"index": 0, "message": assistant, "finish_reason": finish}],
                "usage": {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49},
            },
        )


def start_fake_server(port: int = 0, scenario: str = "demo", host: str = "127.0.0.1"):
    """Start the server in a daemon thread. Returns (base_url, shutdown)."""
    handler = type(
        "BoundFakeOpenAIHandler",
        (FakeOpenAIHandler,),
        {"script": SCENARIOS.get(scenario, DEMO_SCRIPT), "requests": []},
    )
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{host}:{server.server_address[1]}/v1"
    return base_url, server.shutdown


def main() -> int:  # pragma: no cover - manual demo helper
    parser = argparse.ArgumentParser(
        description="Fake OpenAI-compatible server for AgentLite demos."
    )
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--scenario", default="demo", choices=sorted(SCENARIOS))
    args = parser.parse_args()

    base_url, shutdown = start_fake_server(port=args.port, scenario=args.scenario, host=args.host)
    print(f"Fake OpenAI-compatible server listening on {base_url} (scenario={args.scenario})")
    print("Press Ctrl+C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
