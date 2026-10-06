# AgentLite

**A lightweight, self-hosted runtime that gives AI models controlled access to real computers.**

AgentLite is not an AI model, and it is not another chatbot. It is the small execution
layer that sits between an AI model and your machine: the model supplies the
intelligence, AgentLite supplies the controlled computer access.

```
                AI MODEL
        Gemini / OpenAI / Claude /
       OpenRouter / Ollama / etc.
                    │
                    │ tool calls
                    ▼
          ┌───────────────────┐
          │     AgentLite     │
          │   Runtime / API   │
          └─────────┬─────────┘
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
     Browser     Terminal    Filesystem
        │           │           │
        └───────────┼───────────┘
                    ▼
              USER COMPUTER
```

The AI never touches your machine directly. Every action goes through one pipeline:

```
User task → LLM → tool request → permission system → tool → result → LLM → answer
```

---

## Why

Large "computer use" platforms are heavy: containers, orchestration, GPU stacks,
dashboards. AgentLite is the opposite. It is a single Python process with three
tools, a permission system and a small HTTP API.

Measured on a 2-core / 4 GB machine (the project's target hardware):

| | |
|---|---|
| Resident memory (idle server) | **~56 MB** |
| Cold start (import + app build) | **~0.4 s** |
| Mandatory dependencies | 5 small packages |
| GPU / CUDA | not used, not needed |
| Docker, Kubernetes | optional, never required |

---

## Status: v0.2.0 MVP

What is implemented and tested today:

| Area | State |
|---|---|
| `terminal.run` | ✅ stdout, stderr, exit code, timeout, no shell by default |
| `filesystem.list` / `.read` / `.write` | ✅ confined to configured paths |
| `browser.*` (Playwright) | ✅ modular backend, needs `playwright install chromium` |
| Permission system | ✅ allow / deny / confirm, path + command + domain rules |
| Agent loop | ✅ tool-calling loop with step and deadline limits |
| Provider abstraction | ✅ one real provider: OpenAI-compatible (+ scripted mock for tests) |
| HTTP API | ✅ `/api/run`, `/api/status`, `/api/tools`, confirmations, audit tail |
| CLI | ✅ `start`, `doctor`, `tools`, `config`, `run`, `version` |
| Tests | ✅ 252 tests, all offline (43 of them security boundaries) |

v0.2.0 is a hardening release: it closes a terminal memory-exhaustion hole, an
SSRF hole in the browser, a filesystem symlink/FIFO escape and several smaller
gaps. See [CHANGELOG.md](CHANGELOG.md) — it includes one deliberate breaking
change (`filesystem.follow_symlinks`).

[`docs/ci-workflow.yml.example`](docs/ci-workflow.yml.example) has a ready-made
GitHub Actions workflow (tests on Python 3.9-3.12 + wheel build).

Deliberately **not** in this release: a dashboard, a local model, voice, autonomous
long-running agents, desktop control, a plugin system. See [ROADMAP.md](ROADMAP.md).

---

## Installation

```bash
pip install agentlite              # core (terminal + filesystem)
pip install "agentlite[browser]"   # + browser automation
playwright install chromium        # only if you want the browser tools
```

Python 3.9+ on Linux, macOS or WSL. From a clone:

```bash
git clone https://github.com/mhammedamazil-hub/AgentLite.git
cd AgentLite
python -m venv .venv && source .venv/bin/activate
pip install -e ".[browser,dev]"
```

Check the installation:

```bash
agentlite doctor
```

```
AgentLite 0.2.0 - doctor

[ ok ] python: 3.11.2
[ ok ] config file: /home/user/agentlite.yaml
[ ok ] workspace: /home/user/workspace
[ ok ] provider: openai-compatible preset=openai model=gpt-4o-mini
[ ok ] base_url: https://api.openai.com/v1
[ ok ] api key: found in $OPENAI_API_KEY
[ ok ] tool terminal.run: enabled
[ ok ] tool filesystem.list: enabled
[ ok ] tool filesystem.read: enabled
[ ok ] tool filesystem.write: enabled
[ ok ] tool browser.open: enabled
[ ok ] audit log: /home/user/.agentlite/logs/agentlite.jsonl
```

---

## Quick start

**1. Point AgentLite at a model.** Keys come from the environment, never from the
config file:

```bash
export OPENAI_API_KEY=sk-...                 # or OPENROUTER_API_KEY, GEMINI_API_KEY...
export AGENTLITE_MODEL=gpt-4o-mini           # optional override
```

**2. Start the runtime.**

```bash
agentlite start
```

```
AgentLite 0.2.0
  workspace : /home/user/workspace
  provider  : openai / gpt-4o-mini
  listening : http://127.0.0.1:8765
  health    : http://127.0.0.1:8765/health
  run       : POST http://127.0.0.1:8765/api/run
  auth      : disabled (loopback only)
```

**3. Give it a task.**

```bash
curl -s http://127.0.0.1:8765/api/run \
  -H "Content-Type: application/json" \
  -d '{"task": "List the files in the workspace and summarise what this project is"}' \
  | python -m json.tool
```

```json
{
  "run_id": "run_6d1f8c2a9b0e4d13",
  "status": "completed",
  "result": "The workspace contains 4 Python files ...",
  "actions": [
    {
      "step": 1,
      "tool": "filesystem.list",
      "arguments": { "path": "." },
      "decision": "allow",
      "reason": "path allowed",
      "ok": true,
      "output_preview": "D  4096  src/",
      "duration_ms": 2,
      "timestamp": "2026-10-05T10:12:03Z"
    }
  ],
  "steps": 2,
  "provider": "openai-compatible",
  "model": "gpt-4o-mini",
  "usage": { "prompt_tokens": 812, "completion_tokens": 96, "total_tokens": 908, "requests": 2 },
  "elapsed_ms": 1840
}
```

**Or stay in the terminal:**

```bash
agentlite run "create a file called hello.txt containing a haiku about disk space"
```

```
  + [allow] filesystem.write (1ms) wrote 84 bytes to /home/user/workspace/hello.txt

I wrote hello.txt with a short haiku about disk space.
```

Interactive docs (Swagger UI) are served at `http://127.0.0.1:8765/docs`.

---

## Trying it without an API key

`examples/fake_llm_server.py` is a tiny OpenAI-compatible server that replays a
scripted conversation. It is not a language model, but it drives exactly the same
code path, so you can watch the loop work before you spend a cent:

```bash
python examples/fake_llm_server.py --port 8100        # terminal 1

AGENTLITE_BASE_URL=http://localhost:8100/v1 \
AGENTLITE_API_KEY=demo-key \
  agentlite run "inspect the workspace and write a report"      # terminal 2
```

---

## Configuration

Configuration is layered: **packaged defaults → your config file → environment
variables**. Create a starter file with `agentlite config --init agentlite.yaml`,
or copy [`agentlite.yaml.example`](agentlite.yaml.example).

```yaml
workspace: ./workspace          # everything is relative to this

server:
  host: 127.0.0.1               # loopback by default
  port: 8765

provider:
  name: openai                  # openai | openrouter | gemini | ollama | mock | ...
  model: gpt-4o-mini
  api_key_env: OPENAI_API_KEY   # name of the env var, never the key itself

permissions:
  terminal:
    enabled: true
    require_confirmation: true
    cwd: ./workspace
  filesystem:
    enabled: true
    allowed_paths:
      - ./workspace
  browser:
    enabled: true

security:
  confirmation_mode: prompt     # prompt | allow | deny
  max_steps: 25
  audit_log: true
```

Every value can be overridden from the environment:

| Variable | Effect |
|---|---|
| `AGENTLITE__SERVER__PORT` | any nested setting (`AGENTLITE__PERMISSIONS__TERMINAL__TIMEOUT=10`) |
| `AGENTLITE_MODEL` | override `provider.model` |
| `AGENTLITE_PROVIDER` | override `provider.name` |
| `AGENTLITE_BASE_URL` | override `provider.base_url` |
| `AGENTLITE_API_KEY` | fallback API key |
| `AGENTLITE_API_TOKEN` | token required by the HTTP API |
| `AGENTLITE_CONFIRM` | `prompt` / `allow` / `deny` |

Full reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

---

## Available tools

### `terminal.run`

```json
{ "command": "ls -la", "cwd": "subdir", "timeout": 30 }
```

Returns stdout, stderr, exit code, working directory and whether it timed out.

* No shell by default: the command is parsed with `shlex` and executed without
  one, so `;`, `&&`, `|` and backticks are just characters. Set
  `permissions.terminal.allow_shell: true` if you really want `/bin/sh -c`.
* Runs in its own process group, so a timeout kills the whole tree, not just the
  parent.
* Output is capped in constant memory, so `yes` cannot fill your RAM.
* Optional per-command limits: `max_memory_mb`, `max_cpu_seconds`,
  `max_file_size_mb`, `max_processes`.
* Environment variables whose names look like secrets (`*KEY*`, `*TOKEN*`,
  `*SECRET*`, ...) are not passed to the command.
* Working directory is confined to `permissions.terminal.cwd` and resolved, so a
  symlink cannot be used to run commands elsewhere.

### `filesystem.list` / `filesystem.read` / `filesystem.write`

```json
{ "path": "notes/todo.md" }
{ "path": "notes/todo.md", "content": "# TODO", "append": false }
```

* Paths are resolved and then checked against `allowed_paths` — `../etc/passwd`
  and `/etc/passwd` are both refused.
* Symlinks are refused by default (`follow_symlinks: false`). With
  `follow_symlinks: true` they are followed **only inside the allowed roots** —
  the check is repeated after the file is opened, so a swap mid-operation is
  caught too.
* Only regular files and directories are opened: FIFOs, sockets and devices are
  refused (a FIFO would otherwise hang the read forever).
* Hidden files (dotfiles) are refused by default.
* `read_only: true` turns the whole filesystem tool read-only.
* Size limits for reads (truncation) and writes (refusal).

### Browser (`browser.open`, `.click`, `.type`, `.read_page`, `.screenshot`, `.back`, `.close`)

```json
{ "url": "https://example.com" }
{ "selector": "input[name=q]", "text": "agentlite" }
```

Backed by Playwright/Chromium behind a `BrowserBackend` interface, so another
engine can be dropped in without touching the tools or the loop. Screenshots are
written inside the workspace; domains can be restricted with `allowed_domains` /
`denied_domains`.

Navigation is checked against more than the hostname: the host is resolved and
loopback, link-local, private and reserved addresses are refused, so the browser
cannot be used to reach `http://localhost:8080` or
`http://169.254.169.254/latest/meta-data/`. Redirects are re-checked after the
fact. Set `permissions.browser.allow_private_networks: true` to allow it.

Browser automation uses normal, user-facing browser interfaces. AgentLite contains
nothing designed to bypass CAPTCHAs, anti-bot systems, logins, paywalls, rate
limits or access controls — and it never will.

---

## Security model

AgentLite can control a real computer, so security is the product, not a feature.

| Control | Default |
|---|---|
| Filesystem scope | workspace only (`allowed_paths`) |
| Symlink escape | refused |
| Dotfiles | refused |
| Shell metacharacters | no shell; commands are argv, not strings |
| Destructive commands (`sudo`, `rm -rf /`, `mkfs`, `curl \| sh`, ...) | **denied**, always |
| Risky commands (`rm`, `mv`, `pip install`, `git push`, `curl`, ...) | **confirmation required** |
| Command timeout | 30 s (max 600 s), process tree killed |
| Command output | capped in constant memory (32 KB) |
| Private / loopback / metadata addresses from the browser | **denied** (SSRF) |
| Redirects to a private address | **denied** after the fact |
| Steps per run | 25 |
| Wall clock per run | 600 s |
| Concurrent runs | 4 (extra callers get HTTP 429) |
| Task length | 32,768 characters (longer → HTTP 413) |
| API bind address | `127.0.0.1` |
| API authentication | required when bound to anything but loopback |
| Secret storage | environment variables only |
| Environment leaks | secret-looking env vars are withheld from commands |
| Audit trail | JSONL log of every call, decision and duration |
| Audit redaction | known secrets **and** credential shapes (`sk-…`, `AKIA…`, JWTs, `Bearer …`) |
| Dangerous settings | reported by `agentlite doctor` and `/api/status` |

**AgentLite is not a sandbox.** It runs as your user, and an allowed command can
do anything your user can do. The controls above decide *which* actions reach the
machine; they do not constrain what an allowed action can do once it starts.
Prompt injection is not solved by any of this: if a web page tells the model to
run something, the permission system is the only guardrail. Read
[docs/SECURITY.md](docs/SECURITY.md) for the full model and its limits.

**Confirmation modes** (`security.confirmation_mode`):

* `prompt` — ask a human. In the CLI, on stdin; in the API, the run parks with
  HTTP 202 and resumes when a client answers.
* `allow` — approve everything (demos, scripted runs).
* `deny` — refuse anything that would need confirmation (headless default).

The permission engine is independent from the tools: a tool describes what it
*wants* to do, the engine answers `allow` / `deny` / `confirm`, and only then does
anything run. A tool cannot decide for itself, and a bug in a tool cannot escalate
privileges.

More detail, including what AgentLite does *not* protect against:
[docs/SECURITY.md](docs/SECURITY.md).

### Confirmation over the API

```bash
# 1. The run stops and parks (HTTP 202)
curl -s -X POST localhost:8765/api/run -d '{"task":"delete the old build files"}' \
  -H "Content-Type: application/json"
# {"status":"needs_confirmation","run_id":"run_ab12...","pending":{"tool":"terminal.run",...}}

# 2. Inspect it
curl -s localhost:8765/api/runs/run_ab12...

# 3. Answer it
curl -s -X POST localhost:8765/api/runs/run_ab12.../confirm \
  -H "Content-Type: application/json" -d '{"decision":"deny"}'
```

---

## Supported providers

v1 ships **one real provider**: the OpenAI-compatible one. It speaks
`POST {base_url}/chat/completions` with function calling, which covers:

| Preset | Base URL | Key env var |
|---|---|---|
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` |
| `ollama` | `http://localhost:11434/v1` | `OLLAMA_API_KEY` (optional) |
| `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| `together` | `https://api.together.xyz/v1` | `TOGETHER_API_KEY` |
| `lmstudio` | `http://localhost:1234/v1` | `LMSTUDIO_API_KEY` |
| `vllm` | `http://localhost:8000/v1` | `VLLM_API_KEY` |

Any other OpenAI-compatible endpoint works with `name: openai-compatible` plus an
explicit `base_url`. Anything that is *not* OpenAI-compatible needs a new provider
class — the interface is deliberately small ([`providers/base.py`](agentlite/providers/base.py)):

```python
class LLMProvider(ABC):
    name: str
    model: str
    def complete(self, messages, tools, temperature=None, max_tokens=None) -> LLMResponse: ...
```

A scripted `mock` provider exists for tests and demos. It is not a model.

```bash
# Ollama, fully local inference, no API key
agentlite run "summarise this folder" --provider ollama --model llama3.2
```

---

## Hardware requirements

Target (and tested) hardware:

* **RAM:** 4 GB (AgentLite itself uses ~56 MB)
* **CPU:** old Intel/AMD x86-64, 2 cores is plenty
* **GPU:** none. The model runs somewhere else; AgentLite only runs commands
* **OS:** Linux (macOS and WSL work; Windows is untested)

Not required: local LLMs, CUDA, large Docker images, Kubernetes, cloud accounts.

The heaviest optional component is Chromium (~150 MB on disk, a few hundred MB of
RSS while a page is open). On very small machines, disable the browser
(`permissions.browser.enabled: false`) and keep terminal + filesystem.

---

## Examples

| File | What it shows |
|---|---|
| [`examples/fake_llm_server.py`](examples/fake_llm_server.py) | OpenAI-compatible server for keyless demos |
| [`examples/api_client.py`](examples/api_client.py) | drive `/api/run` from Python, including confirmations |
| [`examples/local_ollama.yaml`](examples/local_ollama.yaml) | fully local model via Ollama |
| [`examples/read_only.yaml`](examples/read_only.yaml) | locked-down, read-only agent |
| [`agentlite.yaml.example`](agentlite.yaml.example) | annotated full configuration |

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness, version, uptime (unauthenticated) |
| `GET /api/status` | resolved config, provider, tools, warnings |
| `GET /api/tools` | tool catalogue with parameters and risk |
| `POST /api/run` | run a task (202 when parked for confirmation) |
| `GET /api/runs/{id}` | inspect a parked run |
| `POST /api/runs/{id}/confirm` | `{"decision": "allow"}` or `{"decision": "deny"}` |
| `GET /api/audit?limit=50` | tail of the JSONL audit log |
| `GET /docs` | Swagger UI |

---

## Project layout

```
agentlite/
├── core/
│   ├── agent.py          # the tool-calling loop (resumable runs)
│   ├── executor.py       # the only path from a model request to an action
│   ├── permissions.py    # allow / deny / confirm engine, independent of tools
│   ├── registry.py       # tool catalogue
│   ├── confirmation.py   # how a human is reached (stdin, API round-trip, none)
│   ├── models.py         # ToolCall, ToolResult, ActionRecord, AgentRunResult
│   ├── audit.py          # JSONL audit log
│   └── config.py         # defaults + YAML + env
├── providers/
│   ├── base.py               # LLMProvider interface
│   ├── openai_compatible.py  # the implemented provider
│   └── mock.py               # scripted provider for tests
├── tools/
│   ├── base.py, terminal.py, filesystem.py, browser.py
├── api/server.py         # FastAPI app
├── cli/main.py           # agentlite start | doctor | tools | config | run
└── config/default.yaml   # packaged defaults
```

Design notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Testing

```bash
pytest                            # 252 tests, offline, ~17 s
pytest tests/test_security.py -v  # the security boundaries only
ruff check agentlite tests        # lint
```

The suite covers the permission system, terminal execution and timeouts,
filesystem restrictions, the tool registry, the provider abstraction (including a
real HTTP round trip), the API (including the confirmation flow and token auth),
the CLI and a full end-to-end run through a live uvicorn server.

`tests/test_security.py` holds the tests that exist to fail loudly if a control
is weakened: the SSRF decision table, redirect revalidation, symlink and FIFO
handling, the open-time swap, output and resource limits, process-tree cleanup,
payload caps, credential redaction, and the API's size and concurrency limits.

---

## Roadmap

Desktop control, mouse/keyboard, screenshots/VNC, Docker sandbox, remote
computers, a mobile control panel, a plugin system, MCP compatibility, multiple
computers and agent sessions are all *possible* with this architecture — and none
of them will be started until terminal, filesystem and browser are boring and
reliable. See [ROADMAP.md](ROADMAP.md).

---

## Contributing

Small, tested, boring pull requests are very welcome — especially for the three MVP
tools. Start with [CONTRIBUTING.md](CONTRIBUTING.md); the short version is
`pip install -e ".[browser,dev]"`, `pytest`, `ruff check .`.

Changes to the terminal, filesystem, browser or permission engine should come
with a test in `tests/test_security.py` that fails without the change.

## License

MIT — see [LICENSE](LICENSE).
