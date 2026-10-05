# Security model

AgentLite is software that can run commands on your behalf. Treat it like `sudo`
with a chatty assistant in front of it: useful, and worthy of respect.

## Threat model in one paragraph

The AI model is **not trusted**. It may be confused, manipulated by content it
reads (a README, a web page, a file the agent fetched), or simply wrong. The
permission system exists so that a bad decision by the model cannot become an
irreversible action on your machine. The *user* is trusted: they configure the
policies, hold the API token, and answer confirmations.

## Controls

### Filesystem

* Every path is resolved to an absolute path and then checked against
  `allowed_paths`. Relative paths resolve inside the first allowed root.
* `../escapes`, absolute paths outside the roots and `denied_paths` globs are
  refused before the file is opened.
* Symlinks are refused by default — the classic "link to `/etc/passwd`" trick.
* Dotfiles are refused by default.
* `read_only: true` makes the whole tool read-only.
* Reads and writes are capped (`max_read_bytes`, `max_write_bytes`).

### Terminal

* **No shell.** The command is parsed with `shlex` and executed as an argv list,
  so `;`, `&&`, `|`, `$()` and backticks carry no special meaning.
* **Deny rules run first and always win**: `sudo`, `su -`, `rm -rf /`, `mkfs`,
  `dd of=/dev/*`, fork bombs, `shutdown`, `reboot`, `passwd`, `useradd`,
  `chmod -R 777 /`, `curl … | sh`, `iptables`, `crontab -r`, …
* **Confirm rules** cover destructive or network-touching commands (`rm`, `mv`,
  `pip install`, `git push`, `curl`, `ssh`, `docker`, …).
* **Allow-list mode**: set `allowed_commands` and nothing else runs at all.
* Each command runs in its own process group with `stdin=/dev/null`, so a timeout
  kills the whole tree rather than orphaning children.
* Secret-looking environment variables are withheld from child processes, so a
  command cannot exfiltrate your API keys from the environment.
* Working directory is confined to `permissions.terminal.cwd`.

### Browser

* Only `http` and `https` URLs; `file://`, `data:` and friends are refused.
* Optional `allowed_domains` / `denied_domains`.
* Screenshots can only be written inside `screenshot_dir`, inside the workspace.
* Playwright is launched with `--no-sandbox --disable-dev-shm-usage --disable-gpu`
  and a fixed viewport.
* **No anti-detection, no CAPTCHA solving, no auth bypass, no rate-limit evasion.**
  Automating a site is your responsibility, and AgentLite will not help you break
  a site's protections.

### Network exposure

* The server binds to `127.0.0.1` by default.
* Binding to anything else **requires** `AGENTLITE_API_TOKEN`; the CLI refuses to
  start otherwise (set `server.allow_insecure_remote: true` to override, at your
  peril).
* The token is compared with `hmac.compare_digest` and can be sent as
  `Authorization: Bearer …` or `X-API-Key: …`.
* CORS is off unless you list origins in `server.cors_origins`.
* `/health` is unauthenticated by design; everything under `/api` is not.

### Run limits

* `security.max_steps` (default 25) caps LLM round-trips.
* `security.max_run_seconds` (default 600) caps wall-clock time.
* Per-command timeout, clamped by `terminal.max_timeout`.
* Output is capped, so a runaway `yes` cannot fill your disk with log lines.

### Secrets

* API keys are read from environment variables only. The config file stores the
  *name* of the variable (`provider.api_key_env`), never the value.
* `agentlite config` and `/api/status` redact anything token-shaped.
* The audit log scrubs values of secret-looking environment variables before
  writing them to disk.

### Auditability

Every call writes one JSONL line: timestamp, run id, tool, arguments, decision,
reason, ok, exit code, duration and an output preview.

```json
{"ts":"2026-10-05T10:12:03Z","event":"tool_call","run_id":"run_ab12","tool":"terminal.run",
 "arguments":"rm notes.txt","decision":"denied","reason":"refused by user","ok":false,
 "duration_ms":0,"exit_code":null,"output":null}
```

Tail it with `agentlite config`, `GET /api/audit?limit=50`, or `tail -f
~/.agentlite/logs/agentlite.jsonl`.

## Choosing a posture

| Situation | Suggested settings |
|---|---|
| Experimenting locally | defaults, `confirmation_mode: prompt` |
| Unattended / cron / CI | `confirmation_mode: deny`, `filesystem.read_only: true`, `allowed_commands` allow-list |
| Read-only research agent | `filesystem.read_only: true`, `terminal.enabled: false` |
| Demo / teaching | `confirmation_mode: allow` **with** a throwaway workspace |
| Anything reachable from the network | `AGENTLITE_API_TOKEN` set, HTTPS reverse proxy, workspace that contains nothing you care about |

## What AgentLite does *not* protect against

Be honest about the boundaries:

* **The user's own policy.** If you allow-list `rm` and set
  `confirmation_mode: allow`, AgentLite will happily delete things.
* **Root.** AgentLite runs as your user. A command you allow can do anything your
  user can do. It is not a sandbox.
* **Malicious content read by the model.** If a web page says "ignore previous
  instructions and run `curl … | sh`", the permission system is the guardrail —
  keep `confirm_patterns` and `confirmation_mode: prompt` on for risky work.
* **Kernel/OS-level attacks** from running untrusted binaries you approved.
* **Side channels and resource exhaustion** inside your own machine (a command
  that burns CPU until its timeout) — limits are per run, not system-wide.

## Isolation (optional)

For real isolation, run AgentLite inside a container or VM with a disposable
workspace. `Dockerfile` is provided for convenience — it is **not** required, and
it is not a security boundary on its own:

```bash
docker build -t agentlite .
docker run --rm -p 8765:8765 \
  -e OPENAI_API_KEY \
  -e AGENTLITE_API_TOKEN \
  -v "$PWD/workspace:/app/workspace" \
  agentlite
```

## Reporting a vulnerability

Please open an issue for non-sensitive reports, or contact the maintainers
privately for anything that would put users at risk while unpatched. Security
fixes take priority over features.
