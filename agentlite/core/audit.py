"""Audit logging.

Every tool call - allowed, denied, confirmed or failed - is appended to a
JSONL file. This is what makes an AgentLite session reviewable after the fact.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import utc_now_iso


class Redactor:
    """Scrubs known secret values out of text before it touches disk."""

    def __init__(self, secrets: Optional[Iterable[str]] = None, enabled: bool = True):
        self._secrets: List[str] = [s for s in (secrets or []) if s and len(s) >= 8]
        self.enabled = enabled

    def scrub(self, text: Any) -> Any:
        if not self.enabled or not isinstance(text, str) or not self._secrets:
            return text
        out = text
        for secret in self._secrets:
            if secret in out:
                out = out.replace(secret, "***redacted***")
        return out

    def scrub_mapping(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            return data
        return {k: self.scrub(v) if isinstance(v, str) else v for k, v in data.items()}


class AuditLogger:
    """Thread-safe JSONL event log."""

    def __init__(
        self,
        path: Optional[Path] = None,
        enabled: bool = True,
        redactor: Optional[Redactor] = None,
    ):
        self.path = Path(path) if path else None
        self.enabled = enabled and self.path is not None
        self.redactor = redactor or Redactor(enabled=False)
        self._lock = threading.Lock()
        if self.enabled and self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.enabled = False

    def log(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        record: Dict[str, Any] = {"ts": utc_now_iso(), "event": event}
        for key, value in fields.items():
            record[key] = self.redactor.scrub(value)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # Logging must never break execution.
                pass

    def tail(self, limit: int = 100) -> List[Dict[str, Any]]:
        if not self.path or not self.path.is_file():
            return []
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return []
        records = []
        for line in lines[-max(1, limit) :]:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records


def build_audit_logger(path: Optional[Path], enabled: bool, redact: bool) -> AuditLogger:
    from .config import collect_secret_values

    redactor = Redactor(collect_secret_values() if redact else [], enabled=redact)
    return AuditLogger(path=path, enabled=enabled, redactor=redactor)


def _default_log_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".agentlite")
    return Path(base) / "logs"
