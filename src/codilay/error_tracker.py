"""Centralized error/warning collector for a CodiLay run.

Every error surfaced during a run goes through here so the CLI can
show a persistent error panel at the end (and the web UI can expose
the same data via /api/errors).

Each entry records three things:
  what   — specific description of what went wrong
  why    — root cause (exception message, OS error code, etc.)
  action — what CodiLay did in response (skipped, retried, paused…)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Severity(str, Enum):
    CRITICAL = "CRITICAL"  # run stopped or output may be incomplete
    WARNING = "WARNING"  # run continued but something was degraded
    SKIPPED = "SKIPPED"  # file/section intentionally excluded with reason
    INFO = "INFO"  # noteworthy event that required no action


@dataclass
class ErrorEntry:
    severity: Severity
    what: str
    why: str = ""
    action: str = ""
    file: Optional[str] = None


class ErrorTracker:
    """Collects errors, warnings, and skips during a single run."""

    def __init__(self):
        self._entries: List[ErrorEntry] = []

    # ── Convenience adders ────────────────────────────────────────

    def critical(self, what: str, why: str = "", action: str = "", file: Optional[str] = None):
        self._entries.append(ErrorEntry(Severity.CRITICAL, what, why, action, file))

    def warning(self, what: str, why: str = "", action: str = "", file: Optional[str] = None):
        self._entries.append(ErrorEntry(Severity.WARNING, what, why, action, file))

    def skipped(self, what: str, why: str = "", action: str = "", file: Optional[str] = None):
        self._entries.append(ErrorEntry(Severity.SKIPPED, what, why, action, file))

    def info(self, what: str, why: str = "", action: str = "", file: Optional[str] = None):
        self._entries.append(ErrorEntry(Severity.INFO, what, why, action, file))

    # ── Accessors ─────────────────────────────────────────────────

    @property
    def entries(self) -> List[ErrorEntry]:
        return list(self._entries)

    def counts(self) -> dict:
        result = {s: 0 for s in Severity}
        for e in self._entries:
            result[e.severity] += 1
        return result

    def has_issues(self) -> bool:
        return any(e.severity in (Severity.CRITICAL, Severity.WARNING) for e in self._entries)

    def is_empty(self) -> bool:
        return len(self._entries) == 0
