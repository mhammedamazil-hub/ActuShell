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
import ipaddress
import re
import socket
import threading
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
        if not is_inside(path, roots):
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
        """Allow navigation to `url`?

        Three checks, in order: scheme, domain policy, then - unless
        ``browser.allow_private_networks`` is set - the addresses the host
        resolves to. Without the last one the browser is a ready-made SSRF
        client: ``http://localhost:8080``, ``http://169.254.169.254/latest/...``
        (cloud metadata) and ``http://2130706433`` (127.0.0.1 written in
        decimal) all look like ordinary hostnames.
        """
        policy = self.config.permissions.browser
        if not policy.enabled:
            return Decision(Effect.DENY, "browser tool is disabled", rule="browser.enabled")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return Decision(
                Effect.DENY, f"unsupported URL scheme: {parsed.scheme!r}", rule="browser.scheme"
            )
        host = (parsed.hostname or "").lower()
        if not host:
            return Decision(Effect.DENY, "URL has no host", rule="browser.host")

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

        if policy.allow_private_networks:
            return Decision(Effect.ALLOW, "url allowed", rule="browser.default")
        return _check_host_addresses(host)

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


def is_inside(path: Path, roots: Sequence[Path]) -> bool:
    """True when `path` is one of `roots` or lives below one of them.

    Shared by the permission engine (before an action) and by the filesystem
    tools (after opening, when the path may have been swapped underneath us).
    """
    return any(path == root or root in path.parents for root in roots)


# --------------------------------------------------------------------------- #
# SSRF helpers
# --------------------------------------------------------------------------- #

#: How long a hostname lookup may take before we refuse to navigate.
DNS_TIMEOUT_SECONDS = 3.0

#: Hosts that always mean "this machine", whatever they resolve to.
LOCAL_HOST_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _numeric_host_to_ip(host: str) -> Optional[str]:
    """Convert decimal/hex host forms such as ``2130706433`` to dotted IPv4."""
    text = (host or "").strip()
    try:
        if text.lower().startswith("0x"):
            value = int(text, 16)
        elif text.isdigit():
            value = int(text, 10)
        else:
            return None
    except ValueError:
        return None
    if 0 <= value <= 0xFFFFFFFF:
        return str(ipaddress.IPv4Address(value))
    return None


def _address_is_forbidden(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _check_host_addresses(host: str) -> Decision:
    """Refuse hosts that are - or resolve to - private/loopback/link-local IPs."""
    if host in LOCAL_HOST_NAMES or host.endswith(".localhost"):
        return Decision(
            Effect.DENY,
            f"{host} is a loopback name (set browser.allow_private_networks to override)",
            rule="browser.allow_private_networks",
        )

    literal = _numeric_host_to_ip(host) or host
    try:
        parsed = ipaddress.ip_address(literal)
    except ValueError:
        parsed = None
    if parsed is not None and _address_is_forbidden(parsed):
        return Decision(
            Effect.DENY,
            f"{host} is a loopback, private or link-local address "
            "(set browser.allow_private_networks to override)",
            rule="browser.allow_private_networks",
        )

    addresses, error = _resolve_with_timeout(host)
    if addresses is None:
        # Fail closed: a host we cannot resolve cannot be proven public, and a
        # lookup that never returns must not stall the run either.
        return Decision(Effect.DENY, error or f"could not resolve {host!r}", rule="browser.dns")

    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:  # pragma: no cover - exotic sockaddr
            continue
        if _address_is_forbidden(parsed):
            return Decision(
                Effect.DENY,
                f"{host} resolves to {address}, a private or reserved address "
                "(set browser.allow_private_networks to override)",
                rule="browser.allow_private_networks",
            )
    return Decision(Effect.ALLOW, "url allowed", rule="browser.default")


def _resolve_with_timeout(host: str, timeout: float = DNS_TIMEOUT_SECONDS):
    """Resolve ``host`` to IP strings, giving up after ``timeout`` seconds.

    ``socket.getaddrinfo`` has no timeout of its own, so a slow or hostile DNS
    server would otherwise stall the whole run. The lookup happens on a daemon
    thread; if it does not answer in time we fail closed.
    """
    result: Dict[str, Any] = {}

    def lookup() -> None:
        try:
            result["addresses"] = [info[4][0] for info in socket.getaddrinfo(host, None)]
        except BaseException as exc:  # noqa: BLE001 - reported as a refusal
            result["error"] = exc

    worker = threading.Thread(target=lookup, name="agentlite-dns", daemon=True)
    worker.start()
    worker.join(timeout)
    if "addresses" in result:
        return result["addresses"], None
    if "error" in result:
        return None, f"could not resolve {host!r} (refusing to navigate)"
    return None, f"DNS lookup for {host!r} timed out after {timeout}s (refusing to navigate)"


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
