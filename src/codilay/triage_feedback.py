"""
Triage Feedback — lets users flag incorrect triage decisions to improve
future runs on similar project types.

Storage: codilay/triage_feedback.json

When a user flags a file as incorrectly triaged (e.g., marked 'skip' but
should be 'core'), the feedback is persisted and applied as overrides on
subsequent runs. Over time this builds a project-specific correction layer.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set


@dataclass
class TriageFeedbackEntry:
    """A single triage correction from a user."""

    file_path: str  # The file or pattern (e.g., "src/utils.py" or "*.test.ts")
    original_category: str  # What triage assigned: core/skim/skip
    corrected_category: str  # What the user says it should be
    reason: str = ""  # Optional explanation
    created_at: str = ""
    is_pattern: bool = False  # True if file_path is a glob pattern

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "original_category": self.original_category,
            "corrected_category": self.corrected_category,
            "reason": self.reason,
            "created_at": self.created_at or datetime.now(timezone.utc).isoformat(),
            "is_pattern": self.is_pattern,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TriageFeedbackEntry":
        return cls(
            file_path=data.get("file_path", ""),
            original_category=data.get("original_category", ""),
            corrected_category=data.get("corrected_category", ""),
            reason=data.get("reason", ""),
            created_at=data.get("created_at", ""),
            is_pattern=data.get("is_pattern", False),
        )


class TriageFeedbackStore:
    """
    Persistent store for triage corrections. Feedback is stored per-project
    in the codilay output directory.
    """

    def __init__(self, output_dir: str):
        self._path = os.path.join(output_dir, "triage_feedback.json")
        self._entries: List[TriageFeedbackEntry] = []
        self._project_hints: Dict[str, str] = {}  # project_type -> hint text
        self._load()

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._entries = [TriageFeedbackEntry.from_dict(e) for e in data.get("entries", [])]
            self._project_hints = data.get("project_hints", {})
        except (json.JSONDecodeError, OSError):
            self._entries = []

    def _save(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        data = {
            "entries": [e.to_dict() for e in self._entries],
            "project_hints": self._project_hints,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # ── Public API ────────────────────────────────────────────────

    def add_feedback(
        self,
        file_path: str,
        original_category: str,
        corrected_category: str,
        reason: str = "",
        is_pattern: bool = False,
    ) -> TriageFeedbackEntry:
        """
        Record a triage correction. Replaces existing entry for the same path.
        """
        # Remove existing entry for same path
        self._entries = [e for e in self._entries if e.file_path != file_path]

        entry = TriageFeedbackEntry(
            file_path=file_path,
            original_category=original_category,
            corrected_category=corrected_category,
            reason=reason,
            created_at=datetime.now(timezone.utc).isoformat(),
            is_pattern=is_pattern,
        )
        self._entries.append(entry)
        self._save()
        return entry

    def remove_feedback(self, file_path: str) -> bool:
        """Remove feedback for a specific file/pattern."""
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.file_path != file_path]
        if len(self._entries) < before:
            self._save()
            return True
        return False

    def list_feedback(self) -> List[TriageFeedbackEntry]:
        """Return all feedback entries."""
        return list(self._entries)

    def clear_feedback(self):
        """Clear all feedback entries."""
        self._entries = []
        self._save()

    def set_project_hint(self, project_type: str, hint: str):
        """
        Store a hint about how to triage a specific project type.
        E.g., "flutter" -> "Always skip ios/ android/ but keep lib/generated/"
        """
        self._project_hints[project_type] = hint
        self._save()

    def get_project_hints(self) -> Dict[str, str]:
        return dict(self._project_hints)

    # ── Apply to triage result ────────────────────────────────────

    def apply_to_triage(self, triage_result) -> int:
        """
        Apply stored feedback to a TriageResult, overriding the AI's decisions.
        Returns the number of overrides applied.

        Args:
            triage_result: A TriageResult dataclass instance from triage.py
        """
        import fnmatch

        overrides_applied = 0

        for entry in self._entries:
            if entry.is_pattern:
                # Pattern-based: apply to all matching files
                matched = self._match_pattern(entry.file_path, triage_result)
                for path in matched:
                    if self._move_file(triage_result, path, entry.corrected_category):
                        overrides_applied += 1
            else:
                # Exact path
                if self._move_file(triage_result, entry.file_path, entry.corrected_category):
                    overrides_applied += 1

        return overrides_applied

    def _match_pattern(self, pattern: str, triage_result) -> List[str]:
        """Find all files matching a glob pattern across all triage categories."""
        import fnmatch

        all_files = triage_result.core + triage_result.skim + triage_result.skip
        return [f for f in all_files if fnmatch.fnmatch(f, pattern)]

    def _move_file(self, triage_result, file_path: str, target_category: str) -> bool:
        """Move a file to a different triage category. Returns True if moved."""
        current = None
        if file_path in triage_result.core:
            current = "core"
        elif file_path in triage_result.skim:
            current = "skim"
        elif file_path in triage_result.skip:
            current = "skip"
        else:
            return False

        if current == target_category:
            return False

        # Remove from current
        if current == "core":
            triage_result.core.remove(file_path)
        elif current == "skim":
            triage_result.skim.remove(file_path)
        elif current == "skip":
            triage_result.skip.remove(file_path)

        # Add to target
        if target_category == "core":
            triage_result.core.append(file_path)
        elif target_category == "skim":
            triage_result.skim.append(file_path)
        elif target_category == "skip":
            triage_result.skip.append(file_path)

        return True

    # ── Generate triage prompt enhancement ────────────────────────

    def build_prompt_context(self) -> str:
        """
        Build a text block that can be injected into the triage LLM prompt
        to steer future decisions based on past user feedback.
        """
        if not self._entries and not self._project_hints:
            return ""

        parts = []

        if self._entries:
            parts.append("## User Triage Corrections (apply these overrides)")
            parts.append("The user has previously corrected these triage decisions:")
            for entry in self._entries:
                line = f"- `{entry.file_path}`: was '{entry.original_category}', should be '{entry.corrected_category}'"
                if entry.reason:
                    line += f" — reason: {entry.reason}"
                parts.append(line)
            parts.append("")
            parts.append("Apply similar corrections to files matching these patterns.")

        if self._project_hints:
            parts.append("\n## Project-Type Triage Hints")
            for ptype, hint in self._project_hints.items():
                parts.append(f"- {ptype}: {hint}")

        return "\n".join(parts)
