# Changelog

All notable changes to AgentLite are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
semantic versioning, where a `0.x` **minor** bump may contain breaking changes.

## [0.2.0] - hardening release

This release is about the parts of AgentLite that touch the machine: the
terminal, the filesystem and the browser. Every change below was driven by a
concrete way to break or abuse the 0.1.0 behaviour.

### Added

- **SSRF protection for the browser.** `permissions.check_url` now resolves the
  host and refuses loopback, link-local, private, reserved and unspecified
  addresses, plus hosts with no DNS record (fail-closed). Covers `localhost`,
  `127.0.0.1`, `[::1]`, `169.254.169.254` (cloud metadata), RFC1918 ranges and
  their numeric/hexadecimal spellings (`http://2130706433/`).
  Set `permissions.browser.allow_private_networks: true` to disable it.
- **Post-navigation URL revalidation.** Redirects are re-checked after the fact,
  so a public site cannot redirect the browser to `http://127.0.0.1:8080/`.
  In-flight requests go through a route interceptor as well.
- **Per-command resource limits** (POSIX): `terminal.max_memory_mb`,
  `max_cpu_seconds`, `max_file_size_mb`, `max_processes`. All default to `0`
  (unlimited).
- **Server limits**: `server.max_concurrent_runs` (default 4, extra callers get
  HTTP 429 with `Retry-After`) and `server.max_task_chars` (default 32768,
  longer tasks get HTTP 413).
- **Credential-shape redaction in the audit log.** Keys that look like
  OpenAI/Groq/Slack/AWS/GitHub tokens, JWTs or `Bearer …` headers are scrubbed
  even when they only ever appeared in tool output.
- **Config warnings.** `agentlite doctor` and `GET /api/status` report
  dangerous settings (`allow_shell`, `confirmation_mode: allow`,
  `allow_private_networks`, `allowed_paths: ["/"]`).
- **Better CLI errors**: a missing API key now explains how to set it (or how
  to try AgentLite against `examples/fake_llm_server.py`), an unparseable
  config file names the file and the YAML error.
- 48 new tests, most of them in `tests/test_security.py`.

### Fixed

- **Terminal output is capped in constant memory.** Output is read
  incrementally instead of with `communicate()`, so a command that prints
  hundreds of megabytes returns a truncated result instead of exhausting RAM.
- **`permissions.terminal.timeout` is honoured.** It was previously documented
  as the default timeout but ignored, so `--timeout`/`timeout=` were the only
  working settings.
- **The terminal working directory is resolved.** A symlink inside the
  workspace could previously be used as `cwd` to run commands outside it;
  `cwd` is now `realpath`-resolved and re-checked.
- **Filesystem opens are now TOCTOU-safe.** The file type is checked with
  `lstat` *before* opening (a FIFO used to hang the read forever, defeating
  every timeout), the descriptor is opened with `O_NOFOLLOW | O_NONBLOCK`, and
  the inode plus the containment check are verified again after opening.
- **Non-regular files are refused**: FIFOs, sockets, devices and directories.
- **Model payloads are capped, not just output.** A 500-entry directory listing
  could blow past `max_tool_result_chars`; the cap now applies to the whole
  serialised payload, which is always valid JSON.
- **The browser is thread-safe.** All Playwright calls run on one dedicated
  thread, so concurrent tool calls no longer touch Chromium from two threads.
- **Provider lifecycle**: the browser session, HTTP client and tool registry are
  closed on shutdown, and the `httpx` client uses connection limits and does
  not follow redirects.

### Changed (breaking)

- **`filesystem.follow_symlinks: true` no longer allows escaping the workspace.**
  It now means "follow symlinks that stay inside the allowed roots". Paths
  resolving outside the roots are refused, before and after opening. This is
  the only behavioural break in this release; the previous meaning was a
  sandbox escape waiting to be used.
- `ToolResult.to_model_payload()` may now omit metadata when the payload does
  not fit the character budget (it previously returned a JSON string cut in
  half, which was not valid JSON).
- File and directory checks that used to be advisory now refuse the operation.

### Removed

- Dead helpers: `terminal.available()`, `_decode_limited()`, `_env_summary()`,
  `_default_deadline()`, `_default_log_dir()`.

## [0.1.0] - first MVP release

- `terminal.run`, `filesystem.list`/`read`/`write`, and the Playwright browser
  tools (`open`, `click`, `type`, `read_page`, `screenshot`, `back`, `close`).
- Permission engine (allow / deny / confirm) independent of the tools.
- OpenAI-compatible provider with presets for OpenAI, OpenRouter, Gemini,
  Groq, Together, Ollama, LM Studio and vLLM; scripted `mock` provider.
- FastAPI server (`/api/run`, `/api/status`, `/api/tools`, confirmations, audit
  tail) and the `agentlite` CLI (`start`, `doctor`, `tools`, `config`, `run`).
