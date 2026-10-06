# Roadmap

AgentLite is at **v0.2.0 (hardened MVP)**. The rule for this project is simple: the
three MVP tools have to be boring before anything new is started.

## Done (v0.2.0)

- [x] Terminal memory safety: output capped in constant memory
- [x] `permissions.terminal.timeout` actually applied
- [x] Per-command resource limits (memory, CPU, file size, process count)
- [x] Browser SSRF guard: private / loopback / link-local / metadata addresses refused
- [x] Redirect revalidation after navigation
- [x] Browser thread-safety (one dedicated Playwright thread)
- [x] Filesystem: TOCTOU-safe opens, non-regular files refused, symlinks cannot escape
- [x] Capped model payloads (output *and* metadata), always valid JSON
- [x] Server concurrency and task-size limits
- [x] Credential-shape redaction in the audit log
- [x] Dangerous-setting warnings in `doctor` and `/api/status`

## Done (v0.1.0)

- [x] `terminal.run` — stdout, stderr, exit code, timeout, no shell, deny/confirm rules
- [x] `filesystem.list` / `.read` / `.write` — path confinement, symlink refusal, size limits
- [x] Browser tools — Playwright behind a swappable backend
- [x] Permission engine — allow / deny / confirm, path, command and domain rules
- [x] Agent loop — step limits, deadline, resumable confirmation round-trips
- [x] OpenAI-compatible provider (+ scripted mock provider for tests)
- [x] HTTP API — `/api/run`, status, tools, runs, confirmations, audit tail, token auth
- [x] CLI — `start`, `doctor`, `tools`, `config`, `run`, `version`
- [x] JSONL audit log with secret redaction
- [x] 260 offline tests, including a full end-to-end run over live HTTP

## Not started, ordered by how much they matter

### 0.3 — make the MVP boring

- [ ] More providers implemented natively (Anthropic, Gemini native, Ollama native)
- [ ] Streaming responses so long runs report progress
- [ ] Per-session tool toggles (`/api/run` can enable a subset of tools)
- [ ] Better terminal ergonomics: background commands, `cwd` per session
- [ ] Structured diffs for filesystem writes instead of whole-file rewrites
- [ ] `agentlite doctor --fix` (create the workspace, install hints)
- [ ] Optional seccomp / landed-mode execution for `terminal.run`
- [ ] Per-token rate limiting on the HTTP API
- [ ] Pin resolved IPs for browser navigation (closes the DNS-rebinding window)

### 0.4 — sessions and ergonomics

- [ ] Persistent agent sessions (resume a conversation later)
- [ ] Token/cost accounting per run
- [ ] Dry-run mode ("show me the actions you would take")
- [ ] Richer audit tooling (`agentlite log --follow`, filtering)

### Later, only if the architecture stays clean

- [ ] Desktop control (mouse, keyboard, window focus)
- [ ] Screenshots / VNC / noVNC view
- [ ] Docker sandbox backend for tool execution
- [ ] Remote computers (AgentLite on another host, same API)
- [ ] Mobile control panel
- [ ] Plugin system for third-party tools
- [ ] MCP compatibility (consume and expose MCP tools)
- [ ] Multiple computers behind one endpoint

## Explicitly not planned

- A local LLM (that is what Ollama, llama.cpp and friends are for)
- A large web dashboard
- Voice recognition / synthesis
- Autonomous long-running agents that act without a human in the loop
- Kubernetes operators, service meshes, cloud orchestration
- Anything whose purpose is to bypass CAPTCHAs, logins, paywalls or rate limits

## How to influence this list

Open an issue with the tool you need and the smallest version of it that would be
useful. PRs that make terminal, filesystem or browser *more reliable* beat PRs that
add new surface area.
