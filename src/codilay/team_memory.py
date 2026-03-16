"""
Team Memory — shared cross-session memory across multiple users on the
same project. Extends the existing per-user memory with a team-level
knowledge layer.

Storage: codilay/team/memory.json
         codilay/team/users.json
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_id() -> str:
    return uuid.uuid4().hex[:12]


class TeamMemory:
    """
    Shared knowledge base for teams working on the same project.
    Stores facts, decisions, conventions, and annotations that are
    visible to all team members.
    """

    def __init__(self, output_dir: str):
        self._team_dir = os.path.join(output_dir, "team")
        self._memory_path = os.path.join(self._team_dir, "memory.json")
        self._users_path = os.path.join(self._team_dir, "users.json")
        os.makedirs(self._team_dir, exist_ok=True)

    # ── User management ───────────────────────────────────────────

    def register_user(self, username: str, display_name: str = "") -> Dict[str, Any]:
        """Register a team member. Returns user record."""
        users = self._load_users()
        # Check if already registered
        for u in users:
            if u["username"] == username:
                u["last_seen"] = _now_iso()
                self._save_users(users)
                return u

        user = {
            "id": _make_id(),
            "username": username,
            "display_name": display_name or username,
            "registered_at": _now_iso(),
            "last_seen": _now_iso(),
            "role": "member",  # "admin", "member"
        }
        users.append(user)
        self._save_users(users)
        return user

    def list_users(self) -> List[Dict[str, Any]]:
        return self._load_users()

    def remove_user(self, username: str) -> bool:
        users = self._load_users()
        before = len(users)
        users = [u for u in users if u["username"] != username]
        if len(users) < before:
            self._save_users(users)
            return True
        return False

    # ── Team facts ────────────────────────────────────────────────

    def add_fact(
        self,
        fact: str,
        category: str = "general",
        author: str = "",
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Add a shared fact to team memory."""
        mem = self._load_memory()
        entry = {
            "id": _make_id(),
            "fact": fact,
            "category": category,
            "author": author,
            "tags": tags or [],
            "created_at": _now_iso(),
            "upvotes": 0,
            "downvotes": 0,
        }
        mem["facts"].append(entry)
        self._save_memory(mem)
        return entry

    def remove_fact(self, fact_id: str) -> bool:
        mem = self._load_memory()
        before = len(mem["facts"])
        mem["facts"] = [f for f in mem["facts"] if f.get("id") != fact_id]
        if len(mem["facts"]) < before:
            self._save_memory(mem)
            return True
        return False

    def vote_fact(self, fact_id: str, vote: str) -> bool:
        """Vote on a fact. vote is 'up' or 'down'."""
        mem = self._load_memory()
        for f in mem["facts"]:
            if f.get("id") == fact_id:
                if vote == "up":
                    f["upvotes"] = f.get("upvotes", 0) + 1
                elif vote == "down":
                    f["downvotes"] = f.get("downvotes", 0) + 1
                self._save_memory(mem)
                return True
        return False

    def list_facts(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        mem = self._load_memory()
        facts = mem.get("facts", [])
        if category:
            facts = [f for f in facts if f.get("category") == category]
        # Sort by net votes (upvotes - downvotes), then by recency
        facts.sort(
            key=lambda f: (f.get("upvotes", 0) - f.get("downvotes", 0), f.get("created_at", "")),
            reverse=True,
        )
        return facts

    # ── Team decisions ────────────────────────────────────────────

    def add_decision(
        self,
        title: str,
        description: str,
        author: str = "",
        related_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Record an architectural or design decision."""
        mem = self._load_memory()
        entry = {
            "id": _make_id(),
            "title": title,
            "description": description,
            "author": author,
            "related_files": related_files or [],
            "created_at": _now_iso(),
            "status": "active",  # active, superseded, deprecated
        }
        mem["decisions"].append(entry)
        self._save_memory(mem)
        return entry

    def list_decisions(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        mem = self._load_memory()
        decisions = mem.get("decisions", [])
        if status:
            decisions = [d for d in decisions if d.get("status") == status]
        return decisions

    def update_decision_status(self, decision_id: str, status: str) -> bool:
        mem = self._load_memory()
        for d in mem["decisions"]:
            if d.get("id") == decision_id:
                d["status"] = status
                self._save_memory(mem)
                return True
        return False

    # ── Team conventions ──────────────────────────────────────────

    def add_convention(
        self,
        name: str,
        description: str,
        examples: Optional[List[str]] = None,
        author: str = "",
    ) -> Dict[str, Any]:
        """Record a team coding convention or standard."""
        mem = self._load_memory()
        entry = {
            "id": _make_id(),
            "name": name,
            "description": description,
            "examples": examples or [],
            "author": author,
            "created_at": _now_iso(),
        }
        mem["conventions"].append(entry)
        self._save_memory(mem)
        return entry

    def list_conventions(self) -> List[Dict[str, Any]]:
        mem = self._load_memory()
        return mem.get("conventions", [])

    # ── Annotations (file-level notes) ────────────────────────────

    def add_annotation(
        self,
        file_path: str,
        note: str,
        author: str = "",
        line_range: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add a note/annotation to a specific file."""
        mem = self._load_memory()
        entry = {
            "id": _make_id(),
            "file_path": file_path,
            "note": note,
            "author": author,
            "line_range": line_range,  # e.g., "10-25"
            "created_at": _now_iso(),
        }
        mem["annotations"].append(entry)
        self._save_memory(mem)
        return entry

    def get_annotations(self, file_path: Optional[str] = None) -> List[Dict[str, Any]]:
        mem = self._load_memory()
        annotations = mem.get("annotations", [])
        if file_path:
            annotations = [a for a in annotations if a.get("file_path") == file_path]
        return annotations

    def remove_annotation(self, annotation_id: str) -> bool:
        mem = self._load_memory()
        before = len(mem["annotations"])
        mem["annotations"] = [a for a in mem["annotations"] if a.get("id") != annotation_id]
        if len(mem["annotations"]) < before:
            self._save_memory(mem)
            return True
        return False

    # ── Build context for LLM ─────────────────────────────────────

    def build_context(self) -> str:
        """Build a text block summarizing team knowledge for LLM context."""
        mem = self._load_memory()
        parts = []

        # Facts
        facts = mem.get("facts", [])
        if facts:
            top_facts = sorted(
                facts,
                key=lambda f: f.get("upvotes", 0) - f.get("downvotes", 0),
                reverse=True,
            )[:15]
            lines = [f"- [{f.get('category', 'general')}] {f['fact']}" for f in top_facts]
            parts.append("Team knowledge:\n" + "\n".join(lines))

        # Decisions
        decisions = [d for d in mem.get("decisions", []) if d.get("status") == "active"]
        if decisions:
            lines = [f"- {d['title']}: {d['description'][:100]}" for d in decisions[:10]]
            parts.append("Active decisions:\n" + "\n".join(lines))

        # Conventions
        conventions = mem.get("conventions", [])
        if conventions:
            lines = [f"- {c['name']}: {c['description'][:80]}" for c in conventions[:10]]
            parts.append("Team conventions:\n" + "\n".join(lines))

        return "\n\n".join(parts) if parts else ""

    # ── Merge user memory into team memory ────────────────────────

    def import_from_user_memory(self, user_memory: Dict[str, Any], author: str = "") -> int:
        """
        Import facts from a user's personal memory into team memory.
        Returns number of facts imported.
        """
        imported = 0
        existing_facts = {f["fact"] for f in self.list_facts()}

        for fact_entry in user_memory.get("facts", []):
            fact_text = fact_entry.get("fact", "")
            if fact_text and fact_text not in existing_facts:
                self.add_fact(
                    fact=fact_text,
                    category=fact_entry.get("category", "imported"),
                    author=author,
                )
                imported += 1

        return imported

    # ── Internal ──────────────────────────────────────────────────

    def _load_memory(self) -> Dict[str, Any]:
        if not os.path.exists(self._memory_path):
            return {
                "facts": [],
                "decisions": [],
                "conventions": [],
                "annotations": [],
                "updated_at": None,
            }
        try:
            with open(self._memory_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Ensure all keys exist
            for key in ("facts", "decisions", "conventions", "annotations"):
                if key not in data:
                    data[key] = []
            return data
        except (json.JSONDecodeError, OSError):
            return {
                "facts": [],
                "decisions": [],
                "conventions": [],
                "annotations": [],
                "updated_at": None,
            }

    def _save_memory(self, mem: Dict[str, Any]):
        mem["updated_at"] = _now_iso()
        tmp = self._memory_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mem, f, indent=2)
        os.replace(tmp, self._memory_path)

    def _load_users(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self._users_path):
            return []
        try:
            with open(self._users_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    def _save_users(self, users: List[Dict[str, Any]]):
        tmp = self._users_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=2)
        os.replace(tmp, self._users_path)
