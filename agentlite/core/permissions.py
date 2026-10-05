"""The permission layer.

The permission engine is deliberately independent from the tools: a tool
describes *what it wants to do* (:class:`PermissionRequest`) and the engine
answers with an allow / deny / confirm decision. Tools never decide for
themselves, and the engine never imports a tool.

Effect ladder
-------------
``DENY``    -> the call is refused, the model is told why.
``CONFIRM`` -> the call needs a human (CLI prompt, API round-trip) unless the
               operator set ``security.confirmation_mode`` to allow/deny.
``ALLOW``   -> the call runs.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .config import Config
from .models import RiskLevel


class Effect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


@dataclass
class PermissionRequest:
    """A tool's description of an intended action."""

    tool: str
    action: str  # execute | read | write | list | navigate | click | type | screenshot
    #: policy family (terminal / filesystem / browser), so a custom tool with an
    #: arbitrary name can still be judged by an existing rule set.
    family: Optional[str] = None
    risk: RiskLevel = RiskLevel.LOW
    summary: str = ""
    command: Optional[str] = None
    argv: Optional[Sequence[str]] = None
    path: Optional[Path] = None
    url: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "action": self.action,
            "family": self.family,
            "risk": self.risk.value,
            "summary": self.summary,
            "command": self.command,
            "argv": list(self.argv) if self.argv else None,
            "path": str(self.path) if self.path else None,
            "url": self.url,
            "meta": self.meta,
        }


@dataclass
class Decision:
    effect: Effect
    reason: str
    rule: str = ""

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW


def _compile(patterns: Sequence[str]) -> List[re.Pattern]:
    compiled = []
    for pattern in patterns or []:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE | re.DOTALL))
        except re.error:
            # A broken pattern must fail closed, not silently disable a rule.
            compiled.append(re.compile(re.escape(pattern), re.IGNORECASE))
    return compiled


def _glob_match(path: Path, patterns: Sequence[str]) -> Optional[str]:
    """Match a path against glob patterns (checked against abs + name)."""
    text = str(path)
    for pattern in patterns or []:
        candidate = str(Path(pattern).expanduser())
        if not Path(candidate).is_absolute():
            # Relative patterns are matched on the tail of the path too.
            if fnmatch.fnmatch(text, f"*{candidate.lstrip('./')}") or fnmatch.fnmatch(
                path.name, candidate
            ):
                return pattern
            continue
        if fnmatch.fnmatch(text, candidate):
            return pattern
    return None


class PermissionEngine:
    """Evaluates :class:`PermissionRequest` objects against the configuration."""

    def __init__(self, config: Config):
        self.config = config
        self._pattern_cache: Dict[tuple, List[re.Pattern]] = {}

    def _patterns(self, values: Sequence[str]) -> List[re.Pattern]:
        """Compile (and memoise) a rule set, so config edits take effect."""
        key = tuple(values or ())
        cached = self._pattern_cache.get(key)
        if cached is None:
            cached = _compile(key)
            self._pattern_cache[key] = cached
        return cached

    # -- public API -------------------------------------------------------- #

    def evaluate(self, request: PermissionRequest) -> Decision:
        tool = request.family or request.tool.split(".", 1)[0]
        if tool == "terminal":
            return self._evaluate_terminal(request)
        if tool == "filesystem":
            return self._evaluate_filesystem(request)
        if tool == "browser":
            return self._evaluate_browser(request)
        return Decision(Effect.DENY, f"unknown tool family: {tool}", rule="unknown_tool")

    # -- terminal ---------------------------------------------------------- #

    def _evaluate_terminal(self, request: PermissionRequest) -> Decision:
        policy = self.config.permissions.terminal
        if not policy.enabled:
            return Decision(Effect.DENY, "terminal tool is disabled", rule="terminal.enabled")

        command = request.command or " ".join(request.argv or [])
        if not command.strip():
            return Decision(Effect.DENY, "empty command", rule="terminal.empty")

        requested_cwd = request.meta.get("cwd")
        if requested_cwd:
            cwd_decision = self.check_cwd(Path(requested_cwd))
            if cwd_decision.effect is Effect.DENY:
                return cwd_decision

        for pattern in self._patterns(policy.denied_commands):
            if pattern.search(command):
                return Decision(
                    Effect.DENY,
                    f"command matches deny rule /{pattern.pattern}/",
                    rule="terminal.denied_commands",
                )

        allowed = self._patterns(policy.allowed_commands)
        if allowed:
            if not any(p.search(command) for p in allowed):
                return Decision(
                    Effect.DENY,
                    "command is not in the allow-list (permissions.terminal.allowed_commands)",
                    rule="terminal.allowed_commands",
                )

        for pattern in self._patterns(policy.confirm_patterns):
            if pattern.search(command):
                if not policy.require_confirmation:
                    return Decision(
                        Effect.ALLOW,
                        f"high-risk command allowed because terminal.require_confirmation=false "
                        f"(matched /{pattern.pattern}/)",
                        rule="terminal.require_confirmation",
                    )
                return Decision(
                    Effect.CONFIRM,
                    f"command needs confirmation (matched /{pattern.pattern}/)",
                    rule="terminal.confirm_patterns",
                )

        return Decision(Effect.ALLOW, "command allowed", rule="terminal.default")

    def check_cwd(self, cwd: Path) -> Decision:
        """Is the process allowed to start in `cwd`?"""
        policy = self.config.permissions.terminal
        if policy.allow_outside_cwd:
            return Decision(Effect.ALLOW, "cwd unrestricted by configuration")
        root = self.config.terminal_cwd
        if cwd == root or root in cwd.parents:
            return Decision(Effect.ALLOW, "cwd inside terminal.cwd")
        return Decision(
            Effect.DENY,
            f"cwd {cwd} is outside {root} (set terminal.allow_outside_cwd to override)",
            rule="terminal.allow_outside_cwd",
        )

    # -- filesystem -------------------------------------------------------- #

    def check_path(self, path: Path, action: str) -> Decision:
        policy = self.config.permissions.filesystem
        if not policy.enabled:
            return Decision(Effect.DENY, "filesystem tool is disabled", rule="filesystem.enabled")

        denied = _glob_match(path, policy.denied_paths)
        if denied:
            return Decision(
                Effect.DENY, f"path matches deny rule {denied!r}", rule="filesystem.denied_paths"
            )

        roots = self.config.allowed_roots
        inside = any(path == root or root in path.parents for root in roots)
        if not inside:
            roots_text = ", ".join(str(r) for r in roots)
            return Decision(
                Effect.DENY,
                f"path {path} is outside the allowed paths ({roots_text})",
                rule="filesystem.allowed_paths",
            )

        if not policy.allow_hidden and _has_hidden_part(path, roots):
            return Decision(
                Effect.DENY,
                "hidden files are not accessible (filesystem.allow_hidden=false)",
                rule="filesystem.allow_hidden",
            )

        if action in {"write", "delete"} and policy.read_only:
            return Decision(Effect.DENY, "filesystem is read-only", rule="filesystem.read_only")

        if not policy.follow_symlinks and path.is_symlink():
            return Decision(
                Effect.DENY,
                "symlinks are not followed (filesystem.follow_symlinks=false)",
                rule="filesystem.follow_symlinks",
            )

        if action in {"write", "delete"} and policy.require_confirmation:
            return Decision(
                Effect.CONFIRM,
                "write actions require confirmation",
                rule="filesystem.require_confirmation",
            )

        return Decision(Effect.ALLOW, "path allowed", rule="filesystem.default")

    def _evaluate_filesystem(self, request: PermissionRequest) -> Decision:
        if request.path is None:
            return Decision(Effect.DENY, "no path supplied", rule="filesystem.no_path")
        return self.check_path(request.path, request.action)

    # -- browser ----------------------------------------------------------- #

    def check_url(self, url: str) -> Decision:
        policy = self.config.permissions.browser
        if not policy.enabled:
            return Decision(Effect.DENY, "browser tool is disabled", rule="browser.enabled")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return Decision(
                Effect.DENY, f"unsupported URL scheme: {parsed.scheme!r}", rule="browser.scheme"
            )
        host = (parsed.hostname or "").lower()
        for domain in policy.denied_domains:
            if _domain_matches(host, domain):
                return Decision(
                    Effect.DENY, f"domain {host} is denied", rule="browser.denied_domains"
                )
        if policy.allowed_domains:
            if not any(_domain_matches(host, d) for d in policy.allowed_domains):
                return Decision(
                    Effect.DENY,
                    f"domain {host} is not in browser.allowed_domains",
                    rule="browser.allowed_domains",
                )
        return Decision(Effect.ALLOW, "url allowed", rule="browser.default")

    def _evaluate_browser(self, request: PermissionRequest) -> Decision:
        policy = self.config.permissions.browser
        if request.url:
            decision = self.check_url(request.url)
            if decision.effect is not Effect.ALLOW:
                return decision
        if policy.require_confirmation and request.risk.rank >= RiskLevel.MEDIUM.rank:
            return Decision(
                Effect.CONFIRM,
                f"browser action {request.action!r} requires confirmation",
                rule="browser.require_confirmation",
            )
        return Decision(Effect.ALLOW, "browser action allowed", rule="browser.default")


def _domain_matches(host: str, domain: str) -> bool:
    domain = (domain or "").lower().lstrip("*.")
    if not domain:
        return False
    return host == domain or host.endswith("." + domain)


def _has_hidden_part(path: Path, roots: Sequence[Path]) -> bool:
    """True if any path component below an allowed root starts with a dot."""
    for root in roots:
        if path == root or root in path.parents:
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            return any(part.startswith(".") for part in relative.parts)
    return any(part.startswith(".") for part in path.parts)
