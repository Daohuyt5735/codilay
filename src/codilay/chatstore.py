"""
ChatStore — persistent conversation history, branching, pinning, and cross-session memory.

Storage layout under <output_dir>/chat/:
    conversations/
        <conv_id>.json        — one file per conversation (messages + metadata)
    memory.json               — cross-session memory facts
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ── Data helpers ──────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_id() -> str:
    return uuid.uuid4().hex[:12]


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")[:60]


# ── Message schema ────────────────────────────────────────────────────────────


def make_message(
    role: str,
    content: str,
    *,
    msg_id: Optional[str] = None,
    sources: Optional[List[str]] = None,
    confidence: Optional[float] = None,
    escalated: bool = False,
    pinned: bool = False,
    parent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a normalised message dict."""
    return {
        "id": msg_id or _make_id(),
        "role": role,  # "user" | "assistant" | "system"
        "content": content,
        "sources": sources or [],
        "confidence": confidence,
        "escalated": escalated,
        "pinned": pinned,
        "parent_id": parent_id,  # for branching — points to the msg this replaced
        "created_at": _now_iso(),
    }


# ── Conversation schema ──────────────────────────────────────────────────────


def make_conversation(
    title: str = "",
    conv_id: Optional[str] = None,
    parent_conv_id: Optional[str] = None,
    branch_point_msg_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new conversation envelope."""
    return {
        "id": conv_id or _make_id(),
        "title": title or "New conversation",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "messages": [],
        "parent_conv_id": parent_conv_id,  # if branched from another convo
        "branch_point_msg_id": branch_point_msg_id,
    }


# ── ChatStore ─────────────────────────────────────────────────────────────────


class ChatStore:
    """File-backed store for conversations and cross-session memory."""

    def __init__(self, output_dir: str):
        self._base = os.path.join(output_dir, "chat")
        self._conv_dir = os.path.join(self._base, "conversations")
        self._memory_path = os.path.join(self._base, "memory.json")
        os.makedirs(self._conv_dir, exist_ok=True)

    # ── Conversation CRUD ─────────────────────────────────────────

    def list_conversations(self) -> List[Dict[str, Any]]:
        """Return summary list sorted by most-recently updated."""
        summaries = []
        for fname in os.listdir(self._conv_dir):
            if not fname.endswith(".json"):
                continue
            conv = self._read_conv(fname[:-5])
            if conv is None:
                continue
            msg_count = len(conv.get("messages", []))
            pinned_count = sum(1 for m in conv.get("messages", []) if m.get("pinned"))
            summaries.append(
                {
                    "id": conv["id"],
                    "title": conv["title"],
                    "created_at": conv["created_at"],
                    "updated_at": conv["updated_at"],
                    "message_count": msg_count,
                    "pinned_count": pinned_count,
                    "parent_conv_id": conv.get("parent_conv_id"),
                    "branch_point_msg_id": conv.get("branch_point_msg_id"),
                    "preview": self._preview(conv),
                }
            )
        summaries.sort(key=lambda c: c["updated_at"], reverse=True)
        return summaries

    def get_conversation(self, conv_id: str) -> Optional[Dict[str, Any]]:
        return self._read_conv(conv_id)

    def create_conversation(
        self,
        title: str = "",
        parent_conv_id: Optional[str] = None,
        branch_point_msg_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        conv = make_conversation(
            title=title,
            parent_conv_id=parent_conv_id,
            branch_point_msg_id=branch_point_msg_id,
        )
        self._write_conv(conv)
        return conv

    def delete_conversation(self, conv_id: str) -> bool:
        path = self._conv_path(conv_id)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def update_title(self, conv_id: str, title: str) -> Optional[Dict[str, Any]]:
        conv = self._read_conv(conv_id)
        if conv is None:
            return None
        conv["title"] = title
        conv["updated_at"] = _now_iso()
        self._write_conv(conv)
        return conv

    # ── Message operations ────────────────────────────────────────

    def add_message(
        self, conv_id: str, message: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Append a message to a conversation. Returns updated conversation."""
        conv = self._read_conv(conv_id)
        if conv is None:
            return None
        conv["messages"].append(message)
        conv["updated_at"] = _now_iso()

        # Auto-title from first user message
        if conv["title"] == "New conversation":
            first_user = next(
                (m for m in conv["messages"] if m["role"] == "user"), None
            )
            if first_user:
                conv["title"] = self._auto_title(first_user["content"])

        self._write_conv(conv)
        return conv

    def edit_message(
        self, conv_id: str, msg_id: str, new_content: str
    ) -> Optional[Dict[str, Any]]:
        """
        Edit a message and truncate everything after it.
        Returns the updated conversation (ready for re-running from that point).
        """
        conv = self._read_conv(conv_id)
        if conv is None:
            return None
        idx = self._find_msg_idx(conv, msg_id)
        if idx is None:
            return None
        # Truncate: keep messages up to and including this one
        conv["messages"] = conv["messages"][: idx + 1]
        conv["messages"][idx]["content"] = new_content
        conv["messages"][idx]["created_at"] = _now_iso()
        conv["updated_at"] = _now_iso()
        self._write_conv(conv)
        return conv

    def pin_message(self, conv_id: str, msg_id: str, pinned: bool = True) -> bool:
        conv = self._read_conv(conv_id)
        if conv is None:
            return False
        idx = self._find_msg_idx(conv, msg_id)
        if idx is None:
            return False
        conv["messages"][idx]["pinned"] = pinned
        conv["updated_at"] = _now_iso()
        self._write_conv(conv)
        return True

    def get_pinned_messages(
        self, conv_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get pinned messages. If conv_id given, only from that conversation.
        Otherwise, from ALL conversations (project-wide pinned knowledge).
        """
        pinned = []
        if conv_id:
            conv = self._read_conv(conv_id)
            if conv:
                pinned = [m for m in conv["messages"] if m.get("pinned")]
        else:
            for fname in os.listdir(self._conv_dir):
                if not fname.endswith(".json"):
                    continue
                conv = self._read_conv(fname[:-5])
                if conv:
                    for m in conv["messages"]:
                        if m.get("pinned"):
                            pinned.append(
                                {
                                    **m,
                                    "_conv_id": conv["id"],
                                    "_conv_title": conv["title"],
                                }
                            )
        return pinned

    # ── Branching ─────────────────────────────────────────────────

    def branch_conversation(
        self, conv_id: str, from_msg_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Create a new conversation that branches from an existing one at
        the given message. Copies messages up to (and including) from_msg_id.
        """
        conv = self._read_conv(conv_id)
        if conv is None:
            return None
        idx = self._find_msg_idx(conv, from_msg_id)
        if idx is None:
            return None

        branch = make_conversation(
            title=f"{conv['title']} (branch)",
            parent_conv_id=conv_id,
            branch_point_msg_id=from_msg_id,
        )
        # Copy messages up to the branch point
        branch["messages"] = [
            {**m, "id": _make_id()} for m in conv["messages"][: idx + 1]
        ]
        self._write_conv(branch)
        return branch

    # ── Export ─────────────────────────────────────────────────────

    def export_markdown(self, conv_id: str) -> Optional[str]:
        """Export a conversation to markdown format."""
        conv = self._read_conv(conv_id)
        if conv is None:
            return None

        lines = [
            f"# {conv['title']}",
            f"> Exported from CodiLay on {_now_iso()}",
            "",
        ]

        for msg in conv["messages"]:
            role = msg["role"].capitalize()
            pin = " [PINNED]" if msg.get("pinned") else ""
            deep = " [Deep Agent]" if msg.get("escalated") else ""

            lines.append(f"### {role}{pin}{deep}")
            lines.append("")
            lines.append(msg["content"])
            lines.append("")

            if msg.get("sources"):
                lines.append(f"*Sources: {', '.join(msg['sources'])}*")
                lines.append("")

            lines.append("---")
            lines.append("")

        return "\n".join(lines)

    # ── Context builder (for LLM calls) ───────────────────────────

    def build_chat_context(
        self, conv_id: str, max_messages: int = 20
    ) -> List[Dict[str, str]]:
        """
        Build an LLM-ready message list from conversation history.
        Includes pinned messages at the top for persistent context.
        """
        conv = self._read_conv(conv_id)
        if conv is None:
            return []

        # Collect project-wide pinned messages (from other conversations)
        project_pinned = []
        for fname in os.listdir(self._conv_dir):
            if not fname.endswith(".json"):
                continue
            cid = fname[:-5]
            if cid == conv_id:
                continue
            other = self._read_conv(cid)
            if other:
                for m in other["messages"]:
                    if m.get("pinned") and m["role"] == "assistant":
                        project_pinned.append(m["content"])

        context = []

        # Inject pinned knowledge as system context
        if project_pinned:
            pinned_text = "\n\n---\n\n".join(project_pinned[:5])
            context.append(
                {
                    "role": "system",
                    "content": (
                        "Previously established knowledge (pinned answers):\n\n"
                        + pinned_text
                    ),
                }
            )

        # Current conversation pinned messages
        conv_pinned = [
            m for m in conv["messages"] if m.get("pinned") and m["role"] == "assistant"
        ]
        recent = conv["messages"][-max_messages:]

        # Merge: pinned first (deduped), then recent
        pinned_ids = {m["id"] for m in conv_pinned}
        for m in conv_pinned:
            context.append({"role": m["role"], "content": m["content"]})
        for m in recent:
            if m["id"] not in pinned_ids:
                context.append({"role": m["role"], "content": m["content"]})

        return context

    # ── Cross-session memory ──────────────────────────────────────

    def load_memory(self) -> Dict[str, Any]:
        """Load cross-session memory facts."""
        if not os.path.exists(self._memory_path):
            return {
                "facts": [],
                "preferences": {},
                "frequent_topics": {},
                "updated_at": None,
            }
        with open(self._memory_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_memory(self, memory: Dict[str, Any]):
        memory["updated_at"] = _now_iso()
        with open(self._memory_path, "w", encoding="utf-8") as f:
            json.dump(memory, f, indent=2)

    def add_memory_fact(self, fact: str, category: str = "general") -> Dict[str, Any]:
        """Add a fact to cross-session memory."""
        mem = self.load_memory()
        entry = {
            "id": _make_id(),
            "fact": fact,
            "category": category,
            "created_at": _now_iso(),
        }
        mem["facts"].append(entry)
        self.save_memory(mem)
        return entry

    def delete_memory_fact(self, fact_id: str) -> bool:
        mem = self.load_memory()
        before = len(mem["facts"])
        mem["facts"] = [f for f in mem["facts"] if f.get("id") != fact_id]
        if len(mem["facts"]) < before:
            self.save_memory(mem)
            return True
        return False

    def set_memory_preference(self, key: str, value: str) -> Dict[str, Any]:
        mem = self.load_memory()
        mem["preferences"][key] = value
        self.save_memory(mem)
        return mem

    def delete_memory_preference(self, key: str) -> bool:
        mem = self.load_memory()
        if key in mem["preferences"]:
            del mem["preferences"][key]
            self.save_memory(mem)
            return True
        return False

    def track_topic(self, topic: str):
        """Increment frequency counter for a topic (used to detect weak doc areas)."""
        mem = self.load_memory()
        topics = mem.get("frequent_topics", {})
        topics[topic] = topics.get(topic, 0) + 1
        mem["frequent_topics"] = topics
        self.save_memory(mem)

    def clear_memory(self):
        """Wipe all cross-session memory."""
        self.save_memory(
            {"facts": [], "preferences": {}, "frequent_topics": {}, "updated_at": None}
        )

    def build_memory_context(self) -> str:
        """Build a text summary of memory for injection into LLM context."""
        mem = self.load_memory()
        parts = []

        if mem.get("facts"):
            facts_text = "\n".join(f"- {f['fact']}" for f in mem["facts"][-20:])
            parts.append(f"Known facts about this user:\n{facts_text}")

        if mem.get("preferences"):
            prefs_text = "\n".join(f"- {k}: {v}" for k, v in mem["preferences"].items())
            parts.append(f"User preferences:\n{prefs_text}")

        if mem.get("frequent_topics"):
            # Top 5 most-asked topics
            sorted_topics = sorted(
                mem["frequent_topics"].items(), key=lambda x: x[1], reverse=True
            )[:5]
            topics_text = "\n".join(
                f"- {t} (asked {c} times)" for t, c in sorted_topics
            )
            parts.append(f"Frequently asked topics:\n{topics_text}")

        return "\n\n".join(parts) if parts else ""

    # ── Message retrieval ─────────────────────────────────────────

    def get_message(self, conv_id: str, msg_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific message from a conversation."""
        conv = self._read_conv(conv_id)
        if conv is None:
            return None
        idx = self._find_msg_idx(conv, msg_id)
        if idx is None:
            return None
        return conv["messages"][idx]

    def get_preceding_question(
        self, conv_id: str, msg_id: str
    ) -> Optional[str]:
        """Find the user question that preceded a given assistant message."""
        conv = self._read_conv(conv_id)
        if conv is None:
            return None
        idx = self._find_msg_idx(conv, msg_id)
        if idx is None:
            return None
        # Walk backwards to find the preceding user message
        for i in range(idx - 1, -1, -1):
            if conv["messages"][i]["role"] == "user":
                return conv["messages"][i]["content"]
        return None

    # ── Promote to doc ────────────────────────────────────────────

    def promote_to_doc(
        self, conv_id: str, msg_id: str, docstore, llm_client
    ) -> Optional[str]:
        """
        Promote a chat answer to a documentation section.

        1. Gets the message and its preceding question
        2. Asks LLM to reformat as a doc section
        3. Adds the section to DocStore
        4. Marks the message as promoted

        Returns the section_id on success, None on failure.
        """
        from codilay.prompts import promote_to_doc_prompt, system_prompt

        msg = self.get_message(conv_id, msg_id)
        if msg is None or msg["role"] != "assistant":
            return None

        question = self.get_preceding_question(conv_id, msg_id) or "N/A"

        # Ask LLM to reformat
        prompt = promote_to_doc_prompt(question, msg["content"])
        result = llm_client.call(
            "You reformat chat Q&A into documentation sections. "
            "Return only valid JSON.",
            prompt,
        )

        if "error" in result:
            return None

        section_id = result.get("id", _slugify(result.get("title", "chat-note")))
        title = result.get("title", "From Chat")
        content = result.get("content", msg["content"])
        tags = result.get("tags", ["from-chat"])

        # Ensure 'from-chat' tag is present
        if "from-chat" not in tags:
            tags.append("from-chat")

        docstore.add_section(
            section_id=section_id,
            title=title,
            content=content,
            tags=tags,
            file="",
        )

        # Mark the message as promoted
        conv = self._read_conv(conv_id)
        if conv:
            idx = self._find_msg_idx(conv, msg_id)
            if idx is not None:
                conv["messages"][idx]["promoted_to"] = section_id
                conv["updated_at"] = _now_iso()
                self._write_conv(conv)

        return section_id

    # ── Memory auto-extraction ────────────────────────────────────

    def extract_and_store_memory(self, conv_id: str, llm_client) -> int:
        """
        Run LLM-powered memory extraction on a conversation.
        Extracts facts, preferences, and topics, then stores them.

        Returns the number of new facts added.
        """
        from codilay.prompts import memory_extraction_prompt

        conv = self._read_conv(conv_id)
        if conv is None or len(conv.get("messages", [])) < 2:
            return 0

        prompt = memory_extraction_prompt(conv["messages"])
        result = llm_client.call(
            "You extract memorable facts from conversations. "
            "Return only valid JSON.",
            prompt,
        )

        if "error" in result:
            return 0

        added = 0

        # Store facts
        facts = result.get("facts", [])
        for fact_data in facts:
            if isinstance(fact_data, dict) and fact_data.get("fact"):
                self.add_memory_fact(
                    fact=fact_data["fact"],
                    category=fact_data.get("category", "general"),
                )
                added += 1

        # Store preferences
        preferences = result.get("preferences", {})
        for key, value in preferences.items():
            if key and value:
                self.set_memory_preference(key, str(value))

        # Track topics
        topics = result.get("topics", [])
        for topic in topics:
            if isinstance(topic, str) and topic:
                self.track_topic(topic)

        return added

    # ── Private helpers ───────────────────────────────────────────

    def _conv_path(self, conv_id: str) -> str:
        # Sanitise to prevent path traversal
        safe = re.sub(r"[^a-zA-Z0-9_-]", "", conv_id)
        return os.path.join(self._conv_dir, f"{safe}.json")

    def _read_conv(self, conv_id: str) -> Optional[Dict[str, Any]]:
        path = self._conv_path(conv_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_conv(self, conv: Dict[str, Any]):
        path = self._conv_path(conv["id"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(conv, f, indent=2)
        os.replace(tmp, path)

    def _find_msg_idx(self, conv: Dict, msg_id: str) -> Optional[int]:
        for i, m in enumerate(conv["messages"]):
            if m["id"] == msg_id:
                return i
        return None

    def _auto_title(self, text: str) -> str:
        """Generate a short title from the first user message."""
        clean = text.strip().split("\n")[0][:80]
        if len(clean) > 60:
            clean = clean[:57] + "..."
        return clean or "New conversation"

    def _preview(self, conv: Dict) -> str:
        """Last user message as preview."""
        for m in reversed(conv.get("messages", [])):
            if m["role"] == "user":
                text = m["content"][:100]
                return text + "..." if len(m["content"]) > 100 else text
        return ""
