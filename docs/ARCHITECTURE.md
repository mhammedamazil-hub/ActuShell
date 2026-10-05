# Architecture

AgentLite is one Python process with four responsibilities: describe tools to a
model, ask a permission engine before acting, run the tools, and report what
happened.

## The loop

```
                    ┌──────────────────────────────────────────┐
   task ───────────►│ AgentRun                                 │
                    │  messages = [system, user]               │
                    └───────────────┬──────────────────────────┘
                                    │ complete(messages, tools)
                                    ▼
                          ┌───────────────────┐
                          │ LLMProvider       │  OpenAI-compatible (or mock)
                          └─────────┬─────────┘
                        content ◄───┤ tool_calls
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │ ToolExecutor                             │
                    │  1. registry: does this tool exist?      │
                    │  2. arguments: required ones present?    │
                    │  3. tool.permission_request(args)        │
                    │  4. PermissionEngine.evaluate(request)   │
                    │       allow  -> run                      │
                    │       deny   -> refused result           │
                    │       confirm-> ConfirmationHandler      │
                    │  5. tool.execute(args, context)          │
                    │  6. audit.log(...)                       │
                    └───────────────┬──────────────────────────┘
                                    ▼
                          ToolResult → tool message → back to the model
```

The run ends when the model answers without tool calls, or when a limit is hit
(`max_steps`, `max_run_seconds`). If a call needs a human and nobody is able to
answer inline, the run **parks**: it returns `needs_confirmation` and can be
resumed later with an explicit decision. That is how the HTTP API does confirmations
without holding a socket open.

## Invariants

1. **The model cannot touch the machine.** It produces JSON. `ToolExecutor.execute`
   is the only function in the codebase that calls a tool.
2. **Tools cannot grant themselves permission.** A tool returns a
   `PermissionRequest` (a description, not an action). The engine answers.
3. **The engine knows nothing about tools.** It reads policy from the config and
   patterns from the request. Adding a tool never changes the engine.
4. **Everything is logged.** Allowed, denied, confirmed and failed calls all land
   in the JSONL audit log with their decision and duration.
5. **No shell by default.** Commands are parsed into argv, so injection via `;`,
   `|`, `&&` or backticks cannot happen.

## Components

### `core/models.py`

Plain dataclasses shared by everything: `ToolSpec`, `ToolCall`, `ToolResult`,
`ActionRecord`, `AgentRunResult`, `TokenUsage`, `RunStatus`. No logic, no imports
from the rest of the package — this is what keeps the layers decoupled.

### `core/permissions.py`

`PermissionEngine.evaluate(request) -> Decision(ALLOW | DENY | CONFIRM, reason, rule)`.

Rules are read from the config on every call (regexes are memoised), so a config
reload is just a new engine. Rule sets exist per family:

* **terminal** — deny regexes, optional allow-list mode, confirm regexes, cwd
  containment.
* **filesystem** — allowed roots, denied globs, hidden files, symlinks, read-only.
* **browser** — scheme, domain allow/deny lists, confirmation threshold by risk.

A new tool picks a family by setting `family` on its `PermissionRequest`, so it
inherits the matching rule set without any new code in the engine.

### `core/executor.py`

The single choke point. It also owns the boring-but-important behaviour: unknown
tool, disabled tool, missing argument, tool exception, timeout — each becomes a
`ToolResult` the model can read and adapt to, instead of an exception that kills
the run.

### `core/confirmation.py`

`ConfirmationHandler.confirm(request, decision) -> bool`. Three implementations:

| Handler | Used by | Behaviour |
|---|---|---|
| `CliConfirmationHandler` | `agentlite run` (tty) | asks on stdin, default no |
| `PendingConfirmationHandler` | HTTP API | raises `ConfirmationRequired`, run parks |
| `AutoAllowHandler` / `DenyHandler` | `--yes`, headless | approve / refuse everything |

### `core/registry.py`

Name → tool instance, plus enabled state and the reason a tool is disabled (this
is what `agentlite doctor` prints when Playwright is missing). Tool specs handed to
the model come from here, so a disabled tool is invisible to the model *and*
refused by the executor.

### `providers/`

`LLMProvider.complete(messages, tools, ...) -> LLMResponse(content, tool_calls, usage)`.
One real implementation (`openai_compatible.py`), one scripted (`mock.py`). Tool
names are mapped for the wire (`terminal.run` ↔ `terminal_run`) because several
backends reject dots.

### `tools/`

`Tool` declares `name`, `description`, `parameters` (JSON Schema), `risk`, and
implements `execute` + `permission_request`. Terminal and filesystem are single
classes; the browser is one `BrowserBackend` interface (`PlaywrightBackend` today)
shared by seven small tool objects through a `BrowserSession`.

### `api/server.py`

A thin FastAPI layer. Blocking work (the agent loop is synchronous) is pushed to a
worker thread with `run_in_threadpool`. Runs parked for confirmation live in an
in-memory store with a 30 minute TTL.

## Concurrency and resources

* One process, no background workers, no queue.
* The agent loop is synchronous; the API hands it to a thread pool so the event
  loop keeps serving `/health`.
* Idle memory: ~56 MB. Chromium, when used, dominates (a few hundred MB while a
  page is open) and is closed with the process.
* The browser is started lazily on the first browser call, not at boot.

## Extension points

| I want to… | Touch |
|---|---|
| add a tool | `tools/`, then `registry.build_registry` |
| add a model backend | `providers/` (implement `LLMProvider`) |
| add a permission rule | `core/permissions.py` or just the config |
| change how a human is asked | `core/confirmation.py` |
| add an endpoint | `api/server.py` |
