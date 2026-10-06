"""Configuration loading.

Configuration is layered, later layers win:

    1. packaged defaults            agentlite/config/default.yaml
    2. user config file             ./agentlite.yaml  or  ~/.agentlite/config.yaml
    3. environment variables        AGENTLITE__SECTION__KEY  (plus shortcuts)

Secrets are never stored in the config file: the config only names the
environment variable that holds an API key (`provider.api_key_env`).
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ENV_PREFIX = "AGENTLITE__"
CONFIG_FILENAMES = ("agentlite.yaml", "agentlite.yml", "config.yaml")
USER_CONFIG_PATH = Path.home() / ".agentlite" / "config.yaml"

# --------------------------------------------------------------------------- #
# Default rule sets
# --------------------------------------------------------------------------- #

#: Commands that are *never* executed, whatever the confirmation settings are.
DEFAULT_DENIED_COMMANDS: List[str] = [
    r"\brm\s+-rf\s+/(\s|$)",
    r"\brm\s+-rf\s+/\*",
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\bdd\b.*\bof=/dev/",
    r":\(\)\s*\{.*\}\s*;",  # fork bomb
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r"\bhalt\b",
    r"\binit\s+[06]\b",
    r"\bsudo\b",
    r"\bsu\s+-",
    r"\bvisudo\b",
    r"\bpasswd\b",
    r"\buseradd\b",
    r"\buserdel\b",
    r"\bchown\s+-R\s+/",
    r"\bchmod\s+-R\s+(777|0*7{3})\s+/",
    r"\bcurl\b[^|]*\|\s*(sudo\s+)?(ba|z|fi)?sh",
    r"\bwget\b[^|]*\|\s*(sudo\s+)?(ba|z|fi)?sh",
    r"\biptables\b",
    r"\bufw\b",
    r"\bcrontab\s+-r\b",
    r"\bhistory\s+-c\b",
    r">\s*/dev/(sd|nvme|hd)",
    r"\bsystemctl\s+(stop|disable|mask)\b",
]

#: Commands that are allowed to run only after explicit confirmation.
DEFAULT_CONFIRM_COMMANDS: List[str] = [
    r"\brm\b",
    r"\brmdir\b",
    r"\bmv\b",
    r"\bcp\s+-[rR]",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bkill(all)?\b",
    r"\bpkill\b",
    r"\btar\b.*-[cx]",
    r"\bunzip\b",
    r"\bzip\b",
    r"\bcurl\b",
    r"\bwget\b",
    r"\bssh\b",
    r"\bscp\b",
    r"\brsync\b",
    r"\bgit\s+(push|reset|clean|checkout\s+--)\b",
    r"\bgit\s+commit\b",
    r"\bpip\s+install\b",
    r"\bpip\s+uninstall\b",
    r"\bnpm\s+(install|uninstall|run)\b",
    r"\bapt(-get)?\s+(install|remove|purge)\b",
    r"\bapk\s+add\b",
    r"\bdnf\s+install\b",
    r"\bbrew\s+install\b",
    r"\bdocker\b",
    r"\bsystemctl\b",
    r"\bscreen\b",
    r"\bnohup\b",
    r"\bcrontab\b",
]

#: Environment variable *names* never passed to spawned processes.
DEFAULT_ENV_DENYLIST: List[str] = [
    r".*KEY.*",
    r".*TOKEN.*",
    r".*SECRET.*",
    r".*PASSWORD.*",
    r".*CREDENTIAL.*",
    r".*_AUTH.*",
    r".*SESSION.*",
]

PROVIDER_PRESETS: Dict[str, Dict[str, str]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"},
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GEMINI_API_KEY",
    },
    "ollama": {"base_url": "http://localhost:11434/v1", "api_key_env": "OLLAMA_API_KEY"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY"},
    "together": {"base_url": "https://api.together.xyz/v1", "api_key_env": "TOGETHER_API_KEY"},
    "lmstudio": {"base_url": "http://localhost:1234/v1", "api_key_env": "LMSTUDIO_API_KEY"},
    "vllm": {"base_url": "http://localhost:8000/v1", "api_key_env": "VLLM_API_KEY"},
}

#: Providers implemented in this release. Everything else in PROVIDER_PRESETS is
#: reachable through the OpenAI-compatible provider, but only these are tested.
IMPLEMENTED_PROVIDERS = ("openai-compatible", "mock")


# --------------------------------------------------------------------------- #
# Coercion helpers
# --------------------------------------------------------------------------- #


def _coerce(value: Any, default: Any) -> Any:
    """Best-effort cast of a YAML/env value to the type of `default`."""
    if value is None:
        return value
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, str):
        return str(value)
    if isinstance(default, list):
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]
    if isinstance(default, dict):
        return dict(value) if isinstance(value, Mapping) else {}
    if dataclasses.is_dataclass(default):
        return type(default).from_mapping(value if isinstance(value, Mapping) else {})
    return value


def _from_mapping(cls, data: Mapping[str, Any]):
    kwargs: Dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if value is None:
            continue
        default = getattr(cls(), f.name)
        kwargs[f.name] = _coerce(value, default)
    return cls(**kwargs)


# --------------------------------------------------------------------------- #
# Config sections
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    api_token: Optional[str] = None  # from AGENTLITE_API_TOKEN, never from YAML  # noqa: S105
    allow_insecure_remote: bool = False
    cors_origins: List[str] = dataclasses.field(default_factory=list)
    log_level: str = "info"
    #: How many agent runs may execute at once. Keeps a small machine usable
    #: when several clients call /api/run; extra requests get HTTP 429.
    max_concurrent_runs: int = 4
    #: Maximum length of a task string accepted by /api/run.
    max_task_chars: int = 32768

    @classmethod
    def from_mapping(cls, data):
        return _from_mapping(cls, data)


@dataclasses.dataclass
class ProviderConfig:
    name: str = "openai"  # preset name, or "openai-compatible" / "mock"
    model: str = "gpt-4o-mini"
    base_url: Optional[str] = None
    api_key_env: str = "OPENAI_API_KEY"
    api_key: Optional[str] = None  # env fallback, never read from YAML
    timeout: float = 90.0
    max_retries: int = 2
    temperature: float = 0.0
    max_tokens: Optional[int] = None
    extra_headers: Dict[str, str] = dataclasses.field(default_factory=dict)
    extra_body: Dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data):
        cfg = _from_mapping(cls, data)
        preset = PROVIDER_PRESETS.get(cfg.name.lower())
        if preset and cfg.api_key_env in ("OPENAI_API_KEY", "") and not data.get("api_key_env"):
            cfg.api_key_env = preset["api_key_env"]
        return cfg

    @property
    def effective_base_url(self) -> str:
        """base_url from the config, falling back to the provider preset."""
        if self.base_url:
            return self.base_url
        return PROVIDER_PRESETS.get(self.name.lower(), {}).get("base_url", "")


@dataclasses.dataclass
class TerminalPolicy:
    enabled: bool = True
    require_confirmation: bool = True
    timeout: int = 30
    cwd: Optional[str] = None  # defaults to the workspace
    allow_shell: bool = False
    allow_outside_cwd: bool = False
    #: POSIX resource limits applied to every command (0 = no limit). They stop
    #: one bad command from eating the machine: the timeout is the last resort,
    #: not the first line of defence.
    max_memory_mb: int = 0
    max_cpu_seconds: int = 0
    max_file_size_mb: int = 0
    max_processes: int = 0  # RLIMIT_NPROC is per-user: off by default
    allowed_commands: List[str] = dataclasses.field(default_factory=list)
    denied_commands: List[str] = dataclasses.field(
        default_factory=lambda: list(DEFAULT_DENIED_COMMANDS)
    )
    confirm_patterns: List[str] = dataclasses.field(
        default_factory=lambda: list(DEFAULT_CONFIRM_COMMANDS)
    )
    env_denylist: List[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_ENV_DENYLIST))
    env_allowlist: List[str] = dataclasses.field(default_factory=list)
    max_output_bytes: int = 32768
    max_timeout: int = 600

    @classmethod
    def from_mapping(cls, data):
        return _from_mapping(cls, data)


@dataclasses.dataclass
class FilesystemPolicy:
    enabled: bool = True
    require_confirmation: bool = False
    # Empty means "the workspace directory" - the safe default.
    allowed_paths: List[str] = dataclasses.field(default_factory=list)
    denied_paths: List[str] = dataclasses.field(default_factory=list)
    read_only: bool = False
    allow_hidden: bool = False
    follow_symlinks: bool = False
    max_read_bytes: int = 262144
    max_write_bytes: int = 1048576
    max_list_entries: int = 500

    @classmethod
    def from_mapping(cls, data):
        return _from_mapping(cls, data)


@dataclasses.dataclass
class BrowserPolicy:
    enabled: bool = True
    require_confirmation: bool = False
    headless: bool = True
    timeout_ms: int = 20000
    navigation_timeout_ms: int = 30000
    allowed_domains: List[str] = dataclasses.field(default_factory=list)
    denied_domains: List[str] = dataclasses.field(default_factory=list)
    #: SSRF guard: refuse hosts that resolve to loopback, link-local
    #: (169.254.169.254 = cloud metadata), private or otherwise reserved
    #: addresses. Enable only for local testing against your own servers.
    allow_private_networks: bool = False
    screenshot_dir: str = "./workspace/screenshots"
    max_page_chars: int = 20000
    viewport_width: int = 1280
    viewport_height: int = 800
    user_agent: Optional[str] = None

    @classmethod
    def from_mapping(cls, data):
        return _from_mapping(cls, data)


@dataclasses.dataclass
class SecurityConfig:
    confirmation_mode: str = "prompt"  # prompt | allow | deny
    max_steps: int = 25
    max_run_seconds: int = 600
    audit_log: bool = True
    redact_secrets: bool = True
    log_tool_output: bool = True
    max_output_preview: int = 500
    max_tool_result_chars: int = 8000

    @classmethod
    def from_mapping(cls, data):
        return _from_mapping(cls, data)


@dataclasses.dataclass
class LoggingConfig:
    dir: str = "~/.agentlite/logs"
    level: str = "INFO"
    file: str = "agentlite.jsonl"

    @classmethod
    def from_mapping(cls, data):
        return _from_mapping(cls, data)


@dataclasses.dataclass
class Config:
    workspace: str = "./workspace"
    server: ServerConfig = dataclasses.field(default_factory=ServerConfig)
    provider: ProviderConfig = dataclasses.field(default_factory=ProviderConfig)
    permissions: Permissions = dataclasses.field(default_factory=lambda: Permissions())
    security: SecurityConfig = dataclasses.field(default_factory=SecurityConfig)
    logging: LoggingConfig = dataclasses.field(default_factory=LoggingConfig)

    # Filled in at load time (not part of the YAML schema).
    base_dir: Path = dataclasses.field(default_factory=Path.cwd)
    config_path: Optional[Path] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Config:
        cfg = _from_mapping(cls, data)
        explicit = data.get("permissions") or {}
        if isinstance(explicit, Mapping):
            for sub in ("terminal", "filesystem", "browser"):
                if sub in explicit and explicit[sub]:
                    setattr(
                        cfg.permissions,
                        sub,
                        _coerce(explicit[sub], getattr(Permissions(), sub)),
                    )
        return cfg

    # -- paths ------------------------------------------------------------ #

    @property
    def workspace_root(self) -> Path:
        return (self.base_dir / self.workspace).resolve()

    def resolve_path(self, value: str) -> Path:
        p = Path(os.path.expanduser(value))
        if not p.is_absolute():
            p = self.base_dir / p
        return p.resolve()

    @property
    def allowed_roots(self) -> List[Path]:
        """Directories the filesystem tools may touch (defaults to the workspace)."""
        roots = [self.resolve_path(p) for p in self.permissions.filesystem.allowed_paths]
        return roots or [self.workspace_root]

    @property
    def terminal_cwd(self) -> Path:
        configured = self.permissions.terminal.cwd
        return self.resolve_path(configured) if configured else self.workspace_root

    @property
    def screenshot_dir(self) -> Path:
        return self.resolve_path(self.permissions.browser.screenshot_dir)

    @property
    def log_path(self) -> Path:
        return self.resolve_path(self.logging.dir) / self.logging.file

    def ensure_directories(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    # -- misc ------------------------------------------------------------- #

    def to_dict(self, redact: bool = True) -> Dict[str, Any]:
        data = dataclasses.asdict(self)
        data.pop("base_dir", None)
        data["config_path"] = str(self.config_path) if self.config_path else None
        data["resolved"] = {
            "workspace": str(self.workspace_root),
            "allowed_paths": [str(p) for p in self.allowed_roots],
        }
        if redact and data["server"].get("api_token"):
            data["server"]["api_token"] = REDACTED
        if redact and data["provider"].get("api_key"):
            data["provider"]["api_key"] = REDACTED
        return data


@dataclasses.dataclass
class Permissions:
    terminal: TerminalPolicy = dataclasses.field(default_factory=TerminalPolicy)
    filesystem: FilesystemPolicy = dataclasses.field(default_factory=FilesystemPolicy)
    browser: BrowserPolicy = dataclasses.field(default_factory=BrowserPolicy)

    @classmethod
    def from_mapping(cls, data):
        return _from_mapping(cls, data)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def default_config_path() -> Path:
    """Path of the packaged default configuration."""
    return Path(__file__).resolve().parent.parent / "config" / "default.yaml"


def find_config_file(path: Optional[str] = None) -> Optional[Path]:
    """Locate a config file: explicit path -> cwd -> user home."""
    if path:
        p = Path(os.path.expanduser(path))
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {p}")
        return p.resolve()
    for directory in (Path.cwd(), Path.cwd() / "config"):
        for name in CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate.resolve()
    if USER_CONFIG_PATH.is_file():
        return USER_CONFIG_PATH.resolve()
    return None


def load_config(path: Optional[str] = None, apply_env: bool = True) -> Config:
    """Load configuration, layering defaults < file < environment."""
    data: Dict[str, Any] = {}
    default_file = default_config_path()
    if default_file.is_file():
        data = yaml.safe_load(default_file.read_text(encoding="utf-8")) or {}

    found = find_config_file(path)
    if found is not None:
        file_data = yaml.safe_load(found.read_text(encoding="utf-8")) or {}
        data = _deep_merge(data, file_data)
        base_dir = found.parent if found.name != "config.yaml" else found.parent.parent
        if found.parent.name == "config":
            base_dir = found.parent.parent
    else:
        base_dir = Path.cwd()

    cfg = Config.from_mapping(data)
    cfg.config_path = found
    cfg.base_dir = base_dir
    if apply_env:
        apply_env_overrides(cfg)
    return cfg


def _deep_merge(base: Dict[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def apply_env_overrides(cfg: Config) -> None:
    """Apply AGENTLITE__SECTION__KEY variables plus a few convenient shortcuts."""
    sections = {
        "SERVER": cfg.server,
        "PROVIDER": cfg.provider,
        "SECURITY": cfg.security,
        "LOGGING": cfg.logging,
        "TERMINAL": cfg.permissions.terminal,
        "FILESYSTEM": cfg.permissions.filesystem,
        "BROWSER": cfg.permissions.browser,
    }
    for raw_key, raw_value in os.environ.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        parts = [p for p in raw_key[len(ENV_PREFIX) :].split("__") if p]
        for names in _expand_sections(parts):
            target = sections.get(names[0].upper()) if names else None
            if target is None or len(names) < 2:
                continue
            if _set_dataclass_field(target, names[1], raw_value):
                break

    # Shortcuts ------------------------------------------------------------ #
    if os.environ.get("AGENTLITE_MODEL"):
        cfg.provider.model = os.environ["AGENTLITE_MODEL"]
    if os.environ.get("AGENTLITE_PROVIDER"):
        cfg.provider.name = os.environ["AGENTLITE_PROVIDER"]
    if os.environ.get("AGENTLITE_BASE_URL"):
        cfg.provider.base_url = os.environ["AGENTLITE_BASE_URL"]
    if os.environ.get("AGENTLITE_API_KEY"):
        cfg.provider.api_key = os.environ["AGENTLITE_API_KEY"]
    if os.environ.get("AGENTLITE_API_TOKEN"):
        cfg.server.api_token = os.environ["AGENTLITE_API_TOKEN"]
    if os.environ.get("AGENTLITE_WORKSPACE"):
        cfg.workspace = os.environ["AGENTLITE_WORKSPACE"]
    if os.environ.get("AGENTLITE_CONFIRM"):
        cfg.security.confirmation_mode = os.environ["AGENTLITE_CONFIRM"]
    if os.environ.get("AGENTLITE_LOG_DIR"):
        cfg.logging.dir = os.environ["AGENTLITE_LOG_DIR"]

    # Backwards-compatible: allow AGENTLITE__PERMISSIONS__TERMINAL__ENABLED too.
    for raw_key, raw_value in os.environ.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        parts = [p for p in raw_key[len(ENV_PREFIX) :].split("__") if p]
        if len(parts) == 3 and parts[0].upper() == "PERMISSIONS":
            target = sections.get(parts[1].upper())
            if target is not None:
                _set_dataclass_field(target, parts[2], raw_value)


def _expand_sections(parts: List[str]) -> List[List[str]]:
    """Return candidate [section, field, ...] splits for an env var name."""
    candidates = []
    if len(parts) >= 2 and parts[0].upper() == "PERMISSIONS":
        candidates.append(parts[1:])
    candidates.append(parts)
    return candidates


def _set_dataclass_field(obj: Any, field_name: str, raw_value: str) -> bool:
    name = field_name.lower()
    for f in dataclasses.fields(obj):
        if f.name != name:
            continue
        default = getattr(obj, f.name)
        try:
            setattr(obj, f.name, _coerce(raw_value, default))
            return True
        except (TypeError, ValueError):
            return False
    return False


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #

REDACTED = "***redacted***"
SECRET_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|ACCESS)", re.IGNORECASE)


def collect_secret_values() -> List[str]:
    """Values of environment variables that look like secrets.

    Used by the audit logger to scrub accidental leaks, and by the terminal tool
    to avoid handing API keys to every process the model spawns.
    """
    values = []
    for name, value in os.environ.items():
        if value and len(value) >= 8 and SECRET_NAME_RE.search(name):
            values.append(value)
    return values


def security_warnings(cfg: Config) -> List[str]:
    """Non-fatal but risky settings, surfaced by ``doctor`` and ``/api/status``."""
    warnings: List[str] = []
    terminal = cfg.permissions.terminal
    filesystem = cfg.permissions.filesystem
    browser = cfg.permissions.browser

    if terminal.allow_shell:
        warnings.append(
            "permissions.terminal.allow_shell=true: commands run through /bin/sh, "
            "so pipes, && and command substitution are live"
        )
    if terminal.allow_outside_cwd:
        warnings.append(
            "permissions.terminal.allow_outside_cwd=true: commands may run in any directory"
        )
    if not terminal.require_confirmation:
        warnings.append(
            "permissions.terminal.require_confirmation=false: risky commands run without asking"
        )
    if cfg.security.confirmation_mode == "allow":
        warnings.append("security.confirmation_mode=allow: every action is auto-approved")
    for root in cfg.allowed_roots:
        if str(root) in {"/", str(Path.home())}:
            warnings.append(
                f"filesystem.allowed_paths contains {root}: the agent can reach everything"
            )
    if filesystem.allow_hidden:
        warnings.append("filesystem.allow_hidden=true: dotfiles (keys, .env, .git) are readable")
    if filesystem.follow_symlinks:
        warnings.append("filesystem.follow_symlinks=true: symlinks are followed")
    if browser.allow_private_networks:
        warnings.append(
            "browser.allow_private_networks=true: the browser may reach localhost and "
            "cloud metadata endpoints (SSRF risk)"
        )
    if cfg.server.host not in ("127.0.0.1", "localhost", "::1") and not cfg.server.api_token:
        warnings.append("server is bound to a public interface without an API token")
    return warnings


def validate_config(cfg: Config) -> List[str]:
    """Return a list of human-readable configuration problems (empty == ok)."""
    problems: List[str] = []
    if cfg.security.confirmation_mode not in {"prompt", "allow", "deny"}:
        problems.append(
            "security.confirmation_mode must be prompt|allow|deny "
            f"(got {cfg.security.confirmation_mode!r})"
        )
    if cfg.security.max_steps < 1:
        problems.append("security.max_steps must be >= 1")
    if cfg.permissions.terminal.timeout > cfg.permissions.terminal.max_timeout:
        problems.append("permissions.terminal.timeout exceeds terminal.max_timeout")
    for tool_name in ("terminal", "filesystem", "browser"):
        policy = getattr(cfg.permissions, tool_name)
        if not isinstance(policy.enabled, bool):
            problems.append(f"permissions.{tool_name}.enabled must be a boolean")
    if cfg.server.host not in ("127.0.0.1", "localhost", "::1"):
        if not cfg.server.api_token and not cfg.server.allow_insecure_remote:
            problems.append(
                f"server.host={cfg.server.host!r} is not loopback and no api_token is set: "
                "set AGENTLITE_API_TOKEN or server.allow_insecure_remote (not recommended)"
            )
    return problems
