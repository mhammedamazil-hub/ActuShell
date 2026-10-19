# Security Policy

AgentLite is software that can run commands on your machine. Security reports
take priority over features — if you think you have found a way to make it do
something it should not, please tell us.

## Supported versions

| Version | Supported |
|---|---|
| 0.2.x | ✅ |
| < 0.2 | ❌ (upgrade: 0.1.0 has known memory, SSRF and filesystem issues) |

## Reporting a vulnerability

**Please do not open a public issue for something that puts users at risk while
unpatched.**

1. Use GitHub's private vulnerability reporting:
   <https://github.com/mhammedamazil-hub/AgentLite/security/advisories/new>
2. Or, if you prefer email, open a draft security advisory on the repository —
   it is private to the maintainers by default.

Please include:

* what you expected to happen, and what happened instead;
* the smallest configuration that reproduces it (no API keys, no personal
  paths — a config with `provider.name: mock` is usually enough);
* the AgentLite version and your OS/Python version.

What to expect: an acknowledgement within a few days, an assessment, and — if it
is confirmed — a fix release with credit given unless you ask otherwise. If we
conclude it is intended behaviour rather than a vulnerability, we will explain
why rather than closing it silently.

## Scope

**In scope** (we will treat these as vulnerabilities and fix them quickly):

* escaping the configured filesystem roots (`allowed_paths`) — including via
  symlinks, hard links, races or path parsing;
* reaching a host the SSRF guard should have refused (loopback, link-local,
  private networks, cloud metadata);
* executing a command that the permission system should have denied or paused
  for confirmation;
* exhausting memory, disk or CPU through a single call in a way the documented
  limits do not contain;
* leaking a credential into the audit log, an API response or a child process;
* bypassing the API token, or acting without authentication.

**Out of scope** (reported behaviour, not vulnerabilities — see
[docs/SECURITY.md](docs/SECURITY.md)):

* anything an *allowed* command does. AgentLite is not a sandbox: an allowed
  command runs with your user's privileges. "I allow-listed `rm` and it deleted
  things" is not a bug.
* prompt injection that results in a *denied* or *confirmed* action. The
  permission system is the boundary; injection that reaches an allowed action
  means your policy allowed it.
* denial of service from someone who already holds the API token. Rate limits
  and concurrency caps are bounds, not protections against a trusted caller.
* anything that requires `allow_insecure_remote: true`,
  `confirmation_mode: allow`, `allow_shell: true` or
  `allow_private_networks: true` — those settings exist, they are off by
  default, and `agentlite doctor` warns you when they are on.
* anything whose purpose is to bypass CAPTCHAs, logins, paywalls, rate limits
  or access controls. That is not what this project is for, and such reports
  will be closed.

## The short version of the model

The AI model is **not trusted**; the operator **is**. Every action goes through
one permission engine before it runs, and the checks that can be raced are
repeated at the point of use. Full detail, including what AgentLite does *not*
protect against: [docs/SECURITY.md](docs/SECURITY.md).
