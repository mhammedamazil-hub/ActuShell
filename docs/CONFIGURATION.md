# Configuration reference

Configuration is layered. Later layers win:

1. **Packaged defaults** — `agentlite/config/default.yaml`
2. **Your config file** — `./agentlite.yaml`, `./config/agentlite.yaml`, or
   `~/.agentlite/config.yaml` (first one found, in that order)
3. **Environment variables**

Generate a starter file:

```bash
agentlite config --init agentlite.yaml
agentlite config --path          # which file is in use
agentlite config --check         # validate it
agentlite config                 # print the merged result
```

Relative paths (workspace, allowed paths, screenshot dir, log dir) are resolved
against the **directory containing the config file**, or the current working
directory when there is no config file.

---

## Top level

| Key | Default | Meaning |
|---|---|---|
| `workspace` | `./workspace` | Root directory for the agent. Relative tool paths and (by default) `allowed_paths` resolve here. |

## `server`

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | Bind address. Anything non-loopback requires `AGENTLITE_API_TOKEN`. |
| `port` | `8765` | Listening port. |
| `api_token` | `null` | **Set via `AGENTLITE_API_TOKEN`.** Never write it in the file. |
| `allow_insecure_remote` | `false` | Last-resort escape hatch to bind publicly without a token. Don't. |
| `cors_origins` | `[]` | Browser origins allowed to call the API. Empty = no CORS. |
| `log_level` | `info` | uvicorn log level. |

## `provider`

| Key | Default | Meaning |
|---|---|---|
| `name` | `openai` | Preset (`openai`, `openrouter`, `gemini`, `ollama`, `groq`, `together`, `lmstudio`, `vllm`), `openai-compatible`, or `mock`. |
| `model` | `gpt-4o-mini` | Model id sent to the API. |
| `base_url` | from preset | Explicit endpoint; overrides the preset. |
| `api_key_env` | `OPENAI_API_KEY` | **Name** of the environment variable holding the key. |
| `timeout` | `90` | HTTP timeout in seconds. |
| `max_retries` | `2` | Retries on 408/409/425/429/5xx and transport errors (exponential backoff). |
| `temperature` | `0.0` | Sampling temperature. |
| `max_tokens` | `null` | Optional completion cap. |
| `extra_headers` | `{}` | Extra HTTP headers (e.g. `HTTP-Referer` for OpenRouter). |
| `extra_body` | `{}` | Extra JSON fields merged into the request body. |

## `permissions.terminal`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch. |
| `require_confirmation` | `true` | Commands matching `confirm_patterns` need a human. |
| `timeout` | `30` | Default timeout per command (seconds). |
| `max_timeout` | `600` | Hard ceiling; a larger per-call `timeout` is clamped. |
| `cwd` | workspace | Working directory commands start in. |
| `allow_shell` | `false` | `false` = `shlex.split` + `execve`, no shell. `true` = `/bin/sh -c`. |
| `allow_outside_cwd` | `false` | Allow a command to run outside `cwd`. |
| `allowed_commands` | `[]` | Regexes. Empty = deny-list mode. Non-empty = **allow-list mode**: nothing else runs. |
| `denied_commands` | see below | Regexes that are always refused, regardless of confirmation. |
| `confirm_patterns` | see below | Regexes that need confirmation when `require_confirmation` is true. |
| `env_denylist` | `['.*KEY.*', '.*TOKEN.*', ...]` | Env var *names* withheld from commands. |
| `env_allowlist` | `[]` | Env var names to pass even if they match the denylist. |
| `max_output_bytes` | `32768` | stdout/stderr cap (truncated with a marker). |

Defaults refuse, among others: `sudo`, `su -`, `rm -rf /`, `mkfs`, `dd of=/dev/*`,
fork bombs, `shutdown`, `reboot`, `init 0`, `passwd`, `useradd`, `chmod -R 777 /`,
`curl … | sh`, `iptables`, `crontab -r`.

Defaults ask for confirmation for: `rm`, `rmdir`, `mv`, `cp -r`, `chmod`, `chown`,
`kill`/`pkill`, `tar -x`/`-c`, `zip`/`unzip`, `curl`, `wget`, `ssh`, `scp`, `rsync`,
`git push/reset/clean/commit`, `pip install`, `npm install`, `apt install`, `docker`,
`systemctl`, `crontab`, `nohup`, `screen`.

## `permissions.filesystem`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch. |
| `require_confirmation` | `false` | When true, writes need confirmation. |
| `allowed_paths` | `[]` (= workspace) | Directories the agent may touch. Anything outside is refused. |
| `denied_paths` | `[]` | Glob patterns refused even inside allowed paths (e.g. `*.pem`). |
| `read_only` | `false` | Refuse all writes. |
| `allow_hidden` | `false` | Allow dotfiles/dot-directories. |
| `follow_symlinks` | `false` | Refuse symlinks (they are the classic escape trick). |
| `max_read_bytes` | `262144` | Read cap; longer files are truncated. |
| `max_write_bytes` | `1048576` | Write cap; larger writes are refused. |
| `max_list_entries` | `500` | Directory listing cap. |

## `permissions.browser`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch (also off when Playwright/Chromium is missing). |
| `require_confirmation` | `false` | When true, medium/high risk actions need confirmation. |
| `headless` | `true` | Run Chromium headless. |
| `timeout_ms` | `20000` | Default action timeout. |
| `navigation_timeout_ms` | `30000` | Page load timeout. |
| `allowed_domains` | `[]` | Empty = any http(s) site. Non-empty = only these (suffix match). |
| `denied_domains` | `[]` | Always refused. |
| `screenshot_dir` | `./workspace/screenshots` | Screenshots are written here and nowhere else. |
| `max_page_chars` | `20000` | `read_page` text cap. |
| `viewport_width` / `viewport_height` | `1280` / `800` | Browser viewport. |
| `user_agent` | `null` | Custom user agent string. |

## `security`

| Key | Default | Meaning |
|---|---|---|
| `confirmation_mode` | `prompt` | `prompt` (ask a human), `allow` (approve all), `deny` (refuse all). |
| `max_steps` | `25` | Maximum LLM round-trips per run. |
| `max_run_seconds` | `600` | Wall-clock limit per run. |
| `audit_log` | `true` | Write the JSONL audit log. |
| `redact_secrets` | `true` | Scrub secret-looking values from logs. |
| `log_tool_output` | `true` | Include a preview of tool output in the audit log. |
| `max_output_preview` | `500` | Characters of output kept per audit entry. |
| `max_tool_result_chars` | `8000` | Cap on the result text handed back to the model. |

## `logging`

| Key | Default | Meaning |
|---|---|---|
| `dir` | `~/.agentlite/logs` | Log directory (`~` is expanded). |
| `level` | `INFO` | Python log level for the `agentlite` loggers. |
| `file` | `agentlite.jsonl` | Audit log file name. |

---

## Environment variables

| Variable | Effect |
|---|---|
| `AGENTLITE__SECTION__KEY` | Any setting, e.g. `AGENTLITE__SERVER__PORT=9000`, `AGENTLITE__PERMISSIONS__TERMINAL__TIMEOUT=10`, `AGENTLITE__PERMISSIONS__TERMINAL__ENABLED=false` |
| `AGENTLITE_MODEL` | `provider.model` |
| `AGENTLITE_PROVIDER` | `provider.name` |
| `AGENTLITE_BASE_URL` | `provider.base_url` |
| `AGENTLITE_API_KEY` | Fallback API key when `provider.api_key_env` is unset |
| `AGENTLITE_API_TOKEN` | Token the HTTP API requires on `/api/*` |
| `AGENTLITE_WORKSPACE` | `workspace` |
| `AGENTLITE_CONFIRM` | `security.confirmation_mode` |
| `AGENTLITE_LOG_DIR` | `logging.dir` |

## Ready-made configurations

* [`examples/read_only.yaml`](../examples/read_only.yaml) — inspect only, nothing
  can be written, no network commands.
* [`examples/local_ollama.yaml`](../examples/local_ollama.yaml) — local model, no
  external API at all.
* [`agentlite.yaml.example`](../agentlite.yaml.example) — every key, annotated.
