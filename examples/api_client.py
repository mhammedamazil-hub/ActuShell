#!/usr/bin/env python3
"""Drive the AgentLite API from Python.

    agentlite start                      # terminal 1
    python examples/api_client.py "list the files in the workspace"

Shows the two things every client needs to handle: a run that finishes, and a run
that stops to ask a human (HTTP 202 -> /confirm).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("AGENTLITE_URL", "http://127.0.0.1:8765")
TOKEN = os.environ.get("AGENTLITE_API_TOKEN")


def request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_obj = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    if TOKEN:
        request_obj.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(request_obj, timeout=120) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def print_actions(body: dict) -> None:
    for action in body.get("actions", []):
        marker = "+" if action["ok"] else "!"
        preview = (action.get("output_preview") or "").splitlines()
        detail = preview[0] if preview else (action.get("error") or "")
        print(
            f"  {marker} [{action['decision']}] {action['tool']} "
            f"({action['duration_ms']}ms) {detail[:90]}"
        )


def confirm(run_id: str) -> bool:
    """Ask the operator about a parked action. Ctrl-C / empty answer = deny."""
    status, parked = request("GET", f"/api/runs/{run_id}")
    if status != 200:
        print(f"  could not load the run: {parked}")
        return False
    pending = parked.get("pending") or {}
    print(f"  [confirm] {pending.get('tool')} {pending.get('arguments')}")
    print(f"  reason  : {pending.get('reason')}")
    try:
        answer = input("  Allow? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    return answer in {"y", "yes"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", help="what the agent should do")
    parser.add_argument("--max-steps", type=int, default=None)
    args = parser.parse_args()

    status, body = request("GET", "/health")
    if status != 200:
        print(f"AgentLite is not reachable at {BASE_URL}")
        return 1
    print(f"connected to AgentLite {body['version']} at {BASE_URL}\n")

    payload = {"task": args.task}
    if args.max_steps:
        payload["max_steps"] = args.max_steps

    status, body = request("POST", "/api/run", payload)
    if status == 401:
        print("unauthorized: set AGENTLITE_API_TOKEN to the server's token")
        return 1
    if status >= 400:
        print(f"request failed ({status}): {body}")
        return 1

    # A run can park several times; keep answering until it finishes.
    while status == 202 and body.get("status") == "needs_confirmation":
        run_id = body["run_id"]
        print(f"\nrun {run_id} is waiting for confirmation (HTTP 202)")
        decision = "allow" if confirm(run_id) else "deny"
        status, body = request("POST", f"/api/runs/{run_id}/confirm", {"decision": decision})

    print()
    print_actions(body)
    print()
    print(f"status: {body.get('status')} | steps: {body.get('steps')} | "
          f"{body.get('elapsed_ms')}ms | tokens: {(body.get('usage') or {}).get('total_tokens')}")
    if body.get("result"):
        print(f"\n{body['result']}")
    if body.get("error"):
        print(f"error: {body['error']}", file=sys.stderr)
        return 1
    return 0 if body.get("status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
