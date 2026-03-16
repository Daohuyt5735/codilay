"""
Doc Differ — compares two versions of CodiLay documentation and produces
a human-readable changelog of what shifted between runs.

Usage:
    codilay diff-doc .
    codilay diff-doc . --json

Shows:
- New sections added
- Sections removed
- Sections whose content changed (with a summary of what changed)
- Wire changes (opened/closed between versions)
- Statistics delta (more files processed, etc.)
"""

import difflib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SectionChange:
    """Represents a change to a single documentation section."""

    section_id: str
    title: str
    change_type: str  # "added", "removed", "modified", "renamed"
    old_content: str = ""
    new_content: str = ""
    diff_lines: List[str] = field(default_factory=list)
    summary: str = ""  # Human-readable summary


@dataclass
class DocDiffResult:
    """Complete result of comparing two documentation versions."""

    # Section changes
    added_sections: List[SectionChange] = field(default_factory=list)
    removed_sections: List[SectionChange] = field(default_factory=list)
    modified_sections: List[SectionChange] = field(default_factory=list)

    # Wire changes
    new_closed_wires: int = 0
    lost_closed_wires: int = 0
    new_open_wires: int = 0
    resolved_open_wires: int = 0

    # Stats delta
    files_processed_delta: int = 0
    sections_delta: int = 0
    old_run_time: Optional[str] = None
    new_run_time: Optional[str] = None

    @property
    def has_changes(self) -> bool:
        return bool(
            self.added_sections
            or self.removed_sections
            or self.modified_sections
            or self.new_closed_wires
            or self.lost_closed_wires
            or self.new_open_wires
            or self.resolved_open_wires
        )

    @property
    def total_section_changes(self) -> int:
        return len(self.added_sections) + len(self.removed_sections) + len(self.modified_sections)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "added_sections": [
                {"id": s.section_id, "title": s.title, "summary": s.summary} for s in self.added_sections
            ],
            "removed_sections": [{"id": s.section_id, "title": s.title} for s in self.removed_sections],
            "modified_sections": [
                {"id": s.section_id, "title": s.title, "summary": s.summary, "diff": s.diff_lines[:20]}
                for s in self.modified_sections
            ],
            "wire_changes": {
                "new_closed": self.new_closed_wires,
                "lost_closed": self.lost_closed_wires,
                "new_open": self.new_open_wires,
                "resolved_open": self.resolved_open_wires,
            },
            "stats_delta": {
                "files_processed": self.files_processed_delta,
                "sections": self.sections_delta,
            },
            "old_run_time": self.old_run_time,
            "new_run_time": self.new_run_time,
        }


class DocDiffer:
    """
    Compares two CodiLay state snapshots and produces a DocDiffResult
    showing what changed between documentation runs.
    """

    def __init__(
        self,
        old_index: Dict[str, Dict[str, Any]],
        old_contents: Dict[str, str],
        old_closed_wires: List[Dict],
        old_open_wires: List[Dict],
        new_index: Dict[str, Dict[str, Any]],
        new_contents: Dict[str, str],
        new_closed_wires: List[Dict],
        new_open_wires: List[Dict],
    ):
        self._old_idx = old_index
        self._old_cnt = old_contents
        self._old_closed = old_closed_wires
        self._old_open = old_open_wires
        self._new_idx = new_index
        self._new_cnt = new_contents
        self._new_closed = new_closed_wires
        self._new_open = new_open_wires

    def diff(self) -> DocDiffResult:
        """Compute the full diff between old and new state."""
        result = DocDiffResult()

        old_ids = set(self._old_idx.keys())
        new_ids = set(self._new_idx.keys())

        # Skip meta sections for cleaner output
        skip = {"dependency-graph", "unresolved-references"}
        old_ids -= skip
        new_ids -= skip

        # Added sections
        for sid in sorted(new_ids - old_ids):
            meta = self._new_idx[sid]
            content = self._new_cnt.get(sid, "")
            result.added_sections.append(
                SectionChange(
                    section_id=sid,
                    title=meta.get("title", sid),
                    change_type="added",
                    new_content=content,
                    summary=self._summarize_content(content),
                )
            )

        # Removed sections
        for sid in sorted(old_ids - new_ids):
            meta = self._old_idx[sid]
            result.removed_sections.append(
                SectionChange(
                    section_id=sid,
                    title=meta.get("title", sid),
                    change_type="removed",
                    old_content=self._old_cnt.get(sid, ""),
                )
            )

        # Modified sections
        for sid in sorted(old_ids & new_ids):
            old_content = self._old_cnt.get(sid, "")
            new_content = self._new_cnt.get(sid, "")

            if old_content == new_content:
                continue

            diff_lines = list(
                difflib.unified_diff(
                    old_content.splitlines(),
                    new_content.splitlines(),
                    fromfile=f"old/{sid}",
                    tofile=f"new/{sid}",
                    lineterm="",
                    n=2,
                )
            )

            result.modified_sections.append(
                SectionChange(
                    section_id=sid,
                    title=self._new_idx[sid].get("title", sid),
                    change_type="modified",
                    old_content=old_content,
                    new_content=new_content,
                    diff_lines=diff_lines,
                    summary=self._describe_diff(old_content, new_content),
                )
            )

        # Wire changes
        old_closed_ids = {self._wire_key(w) for w in self._old_closed}
        new_closed_ids = {self._wire_key(w) for w in self._new_closed}
        old_open_ids = {self._wire_key(w) for w in self._old_open}
        new_open_ids = {self._wire_key(w) for w in self._new_open}

        result.new_closed_wires = len(new_closed_ids - old_closed_ids)
        result.lost_closed_wires = len(old_closed_ids - new_closed_ids)
        result.new_open_wires = len(new_open_ids - old_open_ids)
        result.resolved_open_wires = len(old_open_ids - new_open_ids)

        # Stats
        result.sections_delta = len(new_ids) - len(old_ids)

        return result

    def _wire_key(self, wire: Dict) -> str:
        """Create a comparable key for a wire."""
        return f"{wire.get('from', '?')}|{wire.get('to', '?')}|{wire.get('type', '?')}"

    def _summarize_content(self, content: str) -> str:
        """Create a brief summary of section content."""
        # Take first meaningful line
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith(("#", ">", "|", "*", "-", "<!--")):
                if len(stripped) > 100:
                    return stripped[:97] + "..."
                return stripped
        return "(empty section)"

    def _describe_diff(self, old: str, new: str) -> str:
        """Describe what changed between two content versions."""
        old_lines = old.strip().splitlines()
        new_lines = new.strip().splitlines()

        added_count = 0
        removed_count = 0

        matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op == "insert":
                added_count += j2 - j1
            elif op == "delete":
                removed_count += i2 - i1
            elif op == "replace":
                added_count += j2 - j1
                removed_count += i2 - i1

        parts = []
        if added_count:
            parts.append(f"+{added_count} lines")
        if removed_count:
            parts.append(f"-{removed_count} lines")

        # Check for specific content-level changes
        old_refs = set(re.findall(r"`([^`]+)`", old))
        new_refs = set(re.findall(r"`([^`]+)`", new))
        new_code_refs = new_refs - old_refs
        if new_code_refs:
            parts.append(f"new refs: {', '.join(list(new_code_refs)[:3])}")

        return ", ".join(parts) if parts else "minor edits"


# ── State snapshot management ─────────────────────────────────────────────────


class DocVersionStore:
    """
    Stores historical snapshots of documentation state so we can diff
    across runs. Snapshots are stored in codilay/history/.
    """

    def __init__(self, output_dir: str):
        self._history_dir = os.path.join(output_dir, "history")
        os.makedirs(self._history_dir, exist_ok=True)

    def save_snapshot(
        self,
        section_index: Dict,
        section_contents: Dict,
        closed_wires: List[Dict],
        open_wires: List[Dict],
        run_id: str = "",
        commit: str = "",
    ) -> str:
        """
        Save a snapshot of the current documentation state.
        Returns the snapshot filename.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "commit": commit,
            "section_index": section_index,
            "section_contents": section_contents,
            "closed_wires": closed_wires,
            "open_wires": open_wires,
        }

        filename = f"snapshot_{timestamp}.json"
        filepath = os.path.join(self._history_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=1)

        # Clean up old snapshots (keep last 20)
        self._cleanup()

        return filename

    def list_snapshots(self) -> List[Dict[str, str]]:
        """List available snapshots, newest first."""
        snapshots = []
        if not os.path.exists(self._history_dir):
            return []

        for fname in sorted(os.listdir(self._history_dir), reverse=True):
            if not fname.startswith("snapshot_") or not fname.endswith(".json"):
                continue
            filepath = os.path.join(self._history_dir, fname)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                snapshots.append(
                    {
                        "filename": fname,
                        "timestamp": data.get("timestamp", ""),
                        "run_id": data.get("run_id", ""),
                        "commit": data.get("commit", ""),
                        "sections": len(data.get("section_index", {})),
                    }
                )
            except (json.JSONDecodeError, OSError):
                continue

        return snapshots

    def load_snapshot(self, filename: str) -> Optional[Dict]:
        """Load a specific snapshot."""
        filepath = os.path.join(self._history_dir, filename)
        if not os.path.exists(filepath):
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_latest_snapshot(self) -> Optional[Dict]:
        """Get the most recent snapshot (excluding the current one)."""
        snapshots = self.list_snapshots()
        if not snapshots:
            return None
        return self.load_snapshot(snapshots[0]["filename"])

    def get_previous_snapshot(self) -> Optional[Dict]:
        """Get the second-most-recent snapshot (the one before the current run)."""
        snapshots = self.list_snapshots()
        if len(snapshots) < 2:
            return None
        return self.load_snapshot(snapshots[1]["filename"])

    def diff_latest(self) -> Optional[DocDiffResult]:
        """
        Diff the two most recent snapshots. Returns None if fewer than 2 exist.
        """
        snapshots = self.list_snapshots()
        if len(snapshots) < 2:
            return None

        new_snap = self.load_snapshot(snapshots[0]["filename"])
        old_snap = self.load_snapshot(snapshots[1]["filename"])

        if not new_snap or not old_snap:
            return None

        differ = DocDiffer(
            old_index=old_snap.get("section_index", {}),
            old_contents=old_snap.get("section_contents", {}),
            old_closed_wires=old_snap.get("closed_wires", []),
            old_open_wires=old_snap.get("open_wires", []),
            new_index=new_snap.get("section_index", {}),
            new_contents=new_snap.get("section_contents", {}),
            new_closed_wires=new_snap.get("closed_wires", []),
            new_open_wires=new_snap.get("open_wires", []),
        )

        result = differ.diff()
        result.old_run_time = old_snap.get("timestamp")
        result.new_run_time = new_snap.get("timestamp")

        return result

    def _cleanup(self, keep: int = 20):
        """Remove old snapshots, keeping the most recent `keep`."""
        files = sorted(
            [f for f in os.listdir(self._history_dir) if f.startswith("snapshot_")],
            reverse=True,
        )
        for fname in files[keep:]:
            try:
                os.remove(os.path.join(self._history_dir, fname))
            except OSError:
                pass
