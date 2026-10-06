# Contributing

Small, tested, boring pull requests are very welcome — especially anything that
makes the three MVP tools (terminal, filesystem, browser) more reliable.

## Setup

```bash
git clone https://github.com/mhammedamazil-hub/AgentLite.git
cd AgentLite
python -m venv .venv && source .venv/bin/activate
pip install -e ".[browser,dev]"
playwright install chromium      # optional, for the browser tools
pytest
ruff check agentlite tests
ruff format --check agentlite tests
```

The suite is 260 tests and runs offline in about 19 seconds. If it needs a key,
a network call or a browser binary, it does not belong in it.

## The rules

1. **Tests or it didn't happen.** New behaviour needs a test that fails without it.
   The suite must stay offline: no API keys, no network calls, no browser binary.
   (The one exception is `tests/test_e2e.py`, which talks to a local server started
   by the test itself.)
2. **Lint and format clean.** `ruff check agentlite tests` and
   `ruff format --check agentlite tests` (config in `pyproject.toml`).
3. **Security changes are not casual.** Anything touching `core/permissions.py`,
   `core/executor.py`, `tools/terminal.py`, `tools/filesystem.py` or
   `tools/browser.py` needs a test in `tests/test_security.py` that proves the
   restriction works — including one that tries to break it. Before adding a
   check, ask where it has to live: the permission engine runs *before* an
   action, but a path, a URL or a process can change between the check and the
   use, so anything that can be swapped must be re-checked at the point of use.
4. **No placeholder implementations.** If a feature is not finished, do not
   describe it as finished in the README or the docstring.
5. **Keep it light.** A new dependency is a big deal on a 4 GB machine. Prefer the
   standard library, and make anything heavy an optional extra.

## Adding a tool

1. Subclass `Tool` in `agentlite/tools/`:

   ```python
   class MyTool(Tool):
       name = "my.tool"
       family = "filesystem"          # which rule set judges it
       risk = RiskLevel.MEDIUM
       description = "one clear sentence for the model"
       parameters = {"type": "object", "properties": {...}, "required": [...]}

       def permission_request(self, arguments, context) -> PermissionRequest: ...
       def execute(self, arguments, context) -> ToolOutput: ...
   ```

2. Register it in `agentlite/core/registry.py` (`build_registry`).
3. Add tests: happy path, refusal path, and any limit (size, timeout, path).
4. Document it in the README's tool section.

`execute` must honour `context.timeout`. `permission_request` must describe the
action without performing it — that separation is the whole security model.

## Adding a provider

Implement `LLMProvider` (`complete`, `describe`) in `agentlite/providers/`, register
it in `providers/__init__.py`, and test it without a real key (see how
`test_provider.py` uses `httpx.MockTransport` and a fake HTTP server).

## Continuous integration

CI runs the test suite on Python 3.9-3.12 and builds the wheel. The workflow
lives in [`docs/ci-workflow.yml.example`](docs/ci-workflow.yml.example) — copy it
to `.github/workflows/ci.yml` when you set the repository up.

## Running things

```bash
agentlite doctor                 # is my setup sane?
agentlite tools                  # what can the agent do?
pytest -k terminal               # one area
pytest tests/test_security.py -v # the security boundaries
pytest tests/test_e2e.py -v      # the full loop over real HTTP
python examples/fake_llm_server.py --port 8100    # keyless demo backend
```

## Commit and PR hygiene

* One logical change per PR.
* Describe *why*, and how you tested it.
* Update the README when you change behaviour a user can see.
* Never commit an API key, a workspace file, or a config containing a secret.
