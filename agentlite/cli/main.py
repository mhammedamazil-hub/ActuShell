"""Command line interface.

agentlite start              run the local API server
agentlite doctor             check the installation
agentlite tools              list the available tools
agentlite config             show / write the configuration
agentlite run "task"         run one task in the terminal
agentlite version            print the version
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Optional

import yaml

from .. import __version__
from ..core.config import (
    IMPLEMENTED_PROVIDERS,
    PROVIDER_PRESETS,
    Config,
    default_config_path,
    find_config_file,
    load_config,
    security_warnings,
    validate_config,
)
from ..core.models import RunStatus

OK = "[ ok ]"
WARN = "[warn]"
FAIL = "[fail]"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _print(message: str = "") -> None:
    print(message)


def _status(kind: str, label: str, detail: str = "") -> None:
    line = f"{kind} {label}"
    if detail:
        line += f": {detail}"
    _print(line)


def _load(args: argparse.Namespace) -> Config:
    """Load the configuration, or exit with a message a human can act on."""
    try:
        return load_config(getattr(args, "config", None))
    except FileNotFoundError as exc:
        _status(FAIL, "configuration", str(exc))
        _print("  Run 'agentlite config --init agentlite.yaml' to create one.")
        raise SystemExit(2) from exc
    except yaml.YAMLError as exc:
        found = find_config_file(getattr(args, "config", None))
        _status(FAIL, "configuration", f"{found}: invalid YAML")
        _print(f"  {str(exc).splitlines()[0] if str(exc) else exc}")
        raise SystemExit(2) from exc


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_start(args: argparse.Namespace) -> int:
    config = _load(args)
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port

    problems = validate_config(config)
    if problems:
        for problem in problems:
            _status(FAIL, "configuration", problem)
        _print("\nRefusing to start. Fix the configuration or pass --host 127.0.0.1.")
        return 2

    config.ensure_directories()

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - dependency is always installed
        _status(FAIL, "uvicorn is not installed", "pip install 'agentlite[dev]' or uvicorn")
        return 2

    from ..api.server import Runtime, create_app
    from ..core.confirmation import PendingConfirmationHandler

    # The API defers confirmations to the client instead of prompting on stdin.
    runtime = Runtime(config, confirmation_handler=PendingConfirmationHandler())
    app = create_app(config, runtime=runtime)
    scheme = "http"
    base = f"{scheme}://{config.server.host}:{config.server.port}"
    _print(f"AgentLite {__version__}")
    _print(f"  workspace : {config.workspace_root}")
    _print(f"  provider  : {config.provider.name} / {config.provider.model}")
    _print(f"  listening : {base}")
    _print(f"  health    : {base}/health")
    _print(f"  run       : POST {base}/api/run")
    auth_state = "token required" if config.server.api_token else "disabled (loopback only)"
    _print(f"  auth      : {auth_state}")
    _print()
    uvicorn.run(
        app, host=config.server.host, port=config.server.port, log_level=config.server.log_level
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    _print(f"AgentLite {__version__} - doctor\n")
    problems = 0

    # Python ------------------------------------------------------------ #
    _status(OK, "python", sys.version.split()[0])

    # Config ------------------------------------------------------------ #
    config = _load(args)
    found = find_config_file(getattr(args, "config", None))
    if found:
        _status(OK, "config file", str(found))
    else:
        _status(
            WARN, "config file", f"none found, using packaged defaults ({default_config_path()})"
        )

    for problem in validate_config(config):
        _status(FAIL, "config", problem)
        problems += 1
    for warning in security_warnings(config):
        _status(WARN, "security", warning)

    # Workspace --------------------------------------------------------- #
    workspace = config.workspace_root
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        probe = workspace / ".agentlite-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        _status(OK, "workspace", str(workspace))
    except OSError as exc:
        _status(FAIL, "workspace", f"{workspace}: {exc}")
        problems += 1

    # Provider ---------------------------------------------------------- #
    from ..providers import build_provider
    from ..providers.openai_compatible import resolve_api_key

    provider = build_provider(config)
    description = provider.describe()
    if config.provider.name == "mock":
        _status(WARN, "provider", "mock (scripted, for tests/demos - not a real model)")
    else:
        _status(
            OK,
            "provider",
            f"{description.get('name')} preset={config.provider.name} "
            f"model={config.provider.model}",
        )
        _status(OK, "base_url", str(description.get("base_url")))
    key = resolve_api_key(config.provider)
    if config.provider.name == "mock":
        pass
    elif key:
        _status(OK, "api key", f"found in ${config.provider.api_key_env}")
    else:
        _status(
            WARN,
            "api key",
            f"${config.provider.api_key_env} is not set (required for real tasks"
            + (", optional for Ollama/local servers" if config.provider.name == "ollama" else "")
            + ")",
        )
        problems += 1

    # Tools -------------------------------------------------------------- #
    from ..core.registry import build_registry

    registry = build_registry(config)
    _print("")
    for entry in registry:
        if entry.enabled:
            _status(OK, f"tool {entry.tool.name}", entry.reason or "enabled")
        else:
            _status(WARN, f"tool {entry.tool.name}", entry.reason or "disabled")
    _print("")
    _status(OK, "implemented providers", ", ".join(IMPLEMENTED_PROVIDERS))
    _status(OK, "compatible presets", ", ".join(sorted(PROVIDER_PRESETS)))

    # Audit log ---------------------------------------------------------- #
    if config.security.audit_log:
        try:
            config.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config.log_path, "a", encoding="utf-8"):
                pass
            _status(OK, "audit log", str(config.log_path))
        except OSError as exc:
            _status(FAIL, "audit log", str(exc))
            problems += 1
    else:
        _status(WARN, "audit log", "disabled (security.audit_log=false)")

    _print("")
    if problems:
        _print(f"{problems} problem(s) found.")
        return 1
    _print('Everything looks good. Try: agentlite run "list the workspace"')
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    config = _load(args)
    from ..core.registry import build_registry

    registry = build_registry(config)
    if args.json:
        _print(json.dumps(registry.describe(), indent=2))
        return 0

    _print(f"AgentLite {__version__} - tools\n")
    width = max([len(entry.tool.name) for entry in registry] + [10])
    blank = " " * width
    for entry in registry:
        marker = "enabled " if entry.enabled else "disabled"
        risk = entry.tool.risk.value
        summary = entry.tool.description.splitlines()[0] if entry.tool.description else ""
        _print(f"  {entry.tool.name:<{width}}  {marker}  risk={risk}")
        _print(f"  {blank}  {summary}")
        if not entry.enabled and entry.reason and entry.reason != "ok":
            _print(f"  {blank}  -> {entry.reason}")
    _print("")
    _print(f"{len(registry)} tool(s) registered.")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if args.init:
        target = Path(args.init)
        if target.exists() and not args.force:
            _status(FAIL, "init", f"{target} already exists (use --force)")
            return 1
        target.write_text(default_config_path().read_text(encoding="utf-8"), encoding="utf-8")
        _status(OK, "init", f"wrote {target}")
        return 0

    config = _load(args)
    if args.path:
        found = find_config_file(getattr(args, "config", None))
        _print(str(found) if found else f"(no config file; defaults: {default_config_path()})")
        return 0

    if args.check:
        problems = validate_config(config)
        for problem in problems:
            _status(FAIL, "config", problem)
        if not problems:
            _status(OK, "config", "valid")
        return 1 if problems else 0

    import yaml

    _print(yaml.safe_dump(config.to_dict(), sort_keys=False, default_flow_style=False))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    if args.provider:
        config.provider.name = args.provider
    if args.model:
        config.provider.model = args.model
    config.ensure_directories()

    from ..core.agent import Agent
    from ..core.confirmation import AutoAllowHandler, CliConfirmationHandler, DenyHandler

    if args.yes:
        config.security.confirmation_mode = "allow"
        handler: Any = AutoAllowHandler()
    elif sys.stdin.isatty():
        handler = CliConfirmationHandler()
    elif config.security.confirmation_mode == "prompt":
        # Nobody can answer a prompt in a pipe or a cron job: refuse instead of
        # parking a run that the CLI has no way to resume.
        _print("note: not a terminal - actions needing confirmation will be refused.")
        handler = DenyHandler()
    else:
        handler = None  # -> the handler for allow / deny mode

    agent = Agent.from_config(config, confirmation_handler=handler)
    _require_credentials(config)
    try:
        result = agent.run(args.task, max_steps=args.max_steps)
    finally:
        closer = getattr(agent.provider, "close", None)
        if callable(closer):
            closer()

    if args.json:
        _print(json.dumps(result.to_dict(), indent=2))
    else:
        _print("")
        for action in result.actions:
            marker = "+" if action.ok else "!"
            preview = action.output_preview.splitlines()[0] if action.output_preview else ""
            _print(f"  {marker} [{action.decision}] {action.tool} ({action.duration_ms}ms)")
            _print(f"    {preview}")
        _print("")
        if result.result:
            _print(result.result)
        if result.error:
            _status(FAIL, "error", result.error)
        if result.status is RunStatus.NEEDS_CONFIRMATION:
            pending = result.pending or {}
            _status(WARN, "waiting for confirmation", pending.get("summary", ""))
            _print("  Re-run with --yes to approve, or use the API to answer it.")

    return 0 if result.status is RunStatus.COMPLETED else 1


def _require_credentials(config: Config) -> None:
    """Exit early with a useful message instead of a provider 401 later."""
    from ..providers.openai_compatible import resolve_api_key

    preset = (config.provider.name or "").lower()
    if preset in {"mock"} or resolve_api_key(config.provider):
        return
    local = preset in {"ollama", "lmstudio", "vllm"}
    _status(FAIL, "api key", f"${config.provider.api_key_env} is not set")
    if local:
        _print(f"  Expected a local server at {config.provider.effective_base_url}")
        _print("  Start it (e.g. 'ollama serve') or export the key if it needs one.")
    else:
        _print(f"  export {config.provider.api_key_env}=...   # then re-run")
        _print("  Or try AgentLite without a key:")
        _print("    python examples/fake_llm_server.py &")
        _print("    AGENTLITE_BASE_URL=http://127.0.0.1:8100/v1 agentlite run 'list the workspace'")
    raise SystemExit(2)


def cmd_version(args: argparse.Namespace) -> int:
    _print(f"agentlite {__version__} (python {sys.version.split()[0]})")
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentlite",
        description=(
            "AgentLite - a lightweight runtime that gives AI models controlled "
            "access to real computers."
        ),
    )
    parser.add_argument("--version", action="version", version=f"agentlite {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--config", "-c", help="path to a configuration file")
        return sub

    start = add("start", "start the local API server")
    start.add_argument("--host", help="override server.host")
    start.add_argument("--port", type=int, help="override server.port")
    start.set_defaults(func=cmd_start)

    doctor = add("doctor", "check the installation and configuration")
    doctor.set_defaults(func=cmd_doctor)

    tools = add("tools", "list the available tools")
    tools.add_argument("--json", action="store_true", help="machine readable output")
    tools.set_defaults(func=cmd_tools)

    config_cmd = add("config", "show or write the configuration")
    config_cmd.add_argument("--show", action="store_true", help="print the merged configuration")
    config_cmd.add_argument("--path", action="store_true", help="print the config file in use")
    config_cmd.add_argument("--check", action="store_true", help="validate the configuration")
    config_cmd.add_argument("--init", metavar="FILE", help="write an example configuration file")
    config_cmd.add_argument("--force", action="store_true", help="allow --init to overwrite")
    config_cmd.set_defaults(func=cmd_config)

    run = add("run", "run a single task in the terminal")
    run.add_argument("task", help="what the agent should do")
    run.add_argument(
        "--provider", help="override provider.name (openai, openrouter, ollama, mock...)"
    )
    run.add_argument("--model", help="override provider.model")
    run.add_argument("--max-steps", type=int, help="maximum number of LLM round-trips")
    run.add_argument("--yes", "-y", action="store_true", help="auto-approve confirmations")
    run.add_argument("--json", action="store_true", help="machine readable output")
    run.set_defaults(func=cmd_run)

    version = add("version", "print the version")
    version.set_defaults(func=cmd_version)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
