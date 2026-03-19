"""Agent state management — now with git tracking and backup rotation."""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Number of rolling backups to keep alongside the primary state file.
STATE_BACKUP_COUNT = 3


@dataclass
class AgentState:
    run_id: str = ""
    queue: List[str] = field(default_factory=list)
    parked: List[str] = field(default_factory=list)
    park_reasons: Dict[str, str] = field(default_factory=dict)
    open_wires: List[Dict[str, Any]] = field(default_factory=list)
    closed_wires: List[Dict[str, Any]] = field(default_factory=list)
    section_index: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    section_contents: Dict[str, str] = field(default_factory=dict)
    processed: List[str] = field(default_factory=list)

    # ── Git tracking fields ──────────────────────────────────────
    last_commit: Optional[str] = None
    last_commit_short: Optional[str] = None
    last_run: Optional[str] = None
    file_hashes: Dict[str, str] = field(default_factory=dict)  # path → md5

    def save(self, path: str):
        data = {
            "run_id": self.run_id,
            "queue": self.queue,
            "parked": self.parked,
            "park_reasons": self.park_reasons,
            "open_wires": self.open_wires,
            "closed_wires": self.closed_wires,
            "section_index": self.section_index,
            "section_contents": self.section_contents,
            "processed": self.processed,
            "last_commit": self.last_commit,
            "last_commit_short": self.last_commit_short,
            "last_run": self.last_run,
            "file_hashes": self.file_hashes,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        # Rotate backups: .bak.2 → .bak.3, .bak.1 → .bak.2, current → .bak.1
        try:
            for i in range(STATE_BACKUP_COUNT, 1, -1):
                older = f"{path}.bak.{i}"
                newer = f"{path}.bak.{i - 1}"
                if os.path.exists(newer):
                    try:
                        os.replace(newer, older)
                    except OSError:
                        pass
            if os.path.exists(path):
                try:
                    os.replace(path, f"{path}.bak.1")
                except OSError:
                    pass
        except Exception:
            pass  # Backup rotation is best-effort — never block a save

        # Write atomically (write to tmp then rename)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)

    @classmethod
    def load(cls, path: str) -> "AgentState":
        """Load state, falling back to backups if the primary file is corrupt."""
        candidates = [path] + [f"{path}.bak.{i}" for i in range(1, STATE_BACKUP_COUNT + 1)]
        last_err: Optional[Exception] = None

        for candidate in candidates:
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if candidate != path:
                    logger.warning("Primary state corrupt — loaded from backup: %s", candidate)
                return cls._from_dict(data)
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                last_err = exc
                logger.warning("Could not load state from %s: %s", candidate, exc)
                continue

        raise FileNotFoundError(f"Could not load state from {path} or any backup: {last_err}")

    @classmethod
    def _from_dict(cls, data: dict) -> "AgentState":
        state = cls()
        state.run_id = data.get("run_id", "")
        state.queue = data.get("queue", [])
        state.parked = data.get("parked", [])
        state.park_reasons = data.get("park_reasons", {})
        state.open_wires = data.get("open_wires", [])
        state.closed_wires = data.get("closed_wires", [])
        state.section_index = data.get("section_index", {})
        state.section_contents = data.get("section_contents", {})
        state.processed = data.get("processed", [])
        state.last_commit = data.get("last_commit")
        state.last_commit_short = data.get("last_commit_short")
        state.last_run = data.get("last_run")
        state.file_hashes = data.get("file_hashes", {})
        return state
