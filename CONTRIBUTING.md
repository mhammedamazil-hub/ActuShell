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
```

## The rules

1. **Tests or it didn't happen.** New behaviour needs a test that fails without it.
   The suite must stay offline: no API keys, no network calls, no browser binary.
   (The one exception is `tests/test_e2e.py`, which talks to a local server started
   by the test itself.)
2. **Lint clean.** `ruff check .` (config in `pyproject.toml`).
3. **Security changes are not casual.** Anything touching `core/permissions.py`,
   `core/executor.py`, `tools/terminal.py` or `tools/filesystem.py` needs a test
   that proves the restriction works — including one that tries to break it.
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
pytest tests/test_e2e.py -v      # the full loop over real HTTP
python examples/fake_llm_server.py --port 8100    # keyless demo backend
```

## Commit and PR hygiene

* One logical change per PR.
* Describe *why*, and how you tested it.
* Update the README when you change behaviour a user can see.
* Never commit an API key, a workspace file, or a config containing a secret.
