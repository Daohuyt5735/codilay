"""
Conversation Search — full-text search across all past conversations,
not just the current one.

Uses an inverted index built from conversation messages for fast
keyword-based search. No external dependencies.
"""

import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class SearchResult:
    """A single search result from conversation history."""

    conversation_id: str
    conversation_title: str
    message_id: str
    role: str  # "user" or "assistant"
    content: str  # Full message content
    snippet: str  # Highlighted snippet around match
    score: float
    created_at: str = ""
    escalated: bool = False


@dataclass
class SearchResults:
    """Aggregate search results."""

    query: str
    results: List[SearchResult] = field(default_factory=list)
    total_conversations_searched: int = 0
    total_messages_searched: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "total_results": len(self.results),
            "total_conversations_searched": self.total_conversations_searched,
            "total_messages_searched": self.total_messages_searched,
            "results": [
                {
                    "conversation_id": r.conversation_id,
                    "conversation_title": r.conversation_title,
                    "message_id": r.message_id,
                    "role": r.role,
                    "snippet": r.snippet,
                    "score": round(r.score, 3),
                    "created_at": r.created_at,
                    "escalated": r.escalated,
                }
                for r in self.results
            ],
        }


# ── Stop words ───────────────────────────────────────────────────────────────

_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "need",
        "dare",
        "ought",
        "used",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "don",
        "should",
        "now",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "i",
        "me",
        "my",
        "myself",
        "we",
        "our",
        "ours",
        "ourselves",
        "you",
        "your",
        "yours",
        "yourself",
        "he",
        "him",
        "his",
        "she",
        "her",
        "hers",
        "they",
        "them",
        "their",
        "theirs",
        "what",
        "which",
        "who",
        "whom",
        "and",
        "but",
        "if",
        "or",
        "because",
        "as",
        "until",
        "while",
    }
)


def _tokenize(text: str) -> List[str]:
    """Tokenize text into lowercase terms, filtering stop words."""
    words = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    return [w for w in words if w not in _STOP_WORDS and len(w) > 1]


# ── Search engine ────────────────────────────────────────────────────────────


class ConversationSearch:
    """
    Full-text search engine over conversation history.
    Uses TF-IDF scoring for relevance ranking.
    """

    def __init__(self, output_dir: str):
        self._conv_dir = os.path.join(output_dir, "chat", "conversations")
        self._index_path = os.path.join(output_dir, "chat", "search_index.json")

        # In-memory inverted index
        # term -> [(conv_id, msg_id, term_frequency)]
        self._inverted_index: Dict[str, List[Tuple[str, str, float]]] = defaultdict(list)
        self._doc_count = 0  # Total documents (messages) indexed
        self._doc_lengths: Dict[str, int] = {}  # (conv_id:msg_id) -> token count

        # Conversation metadata cache
        self._conv_meta: Dict[str, Dict[str, str]] = {}  # conv_id -> {title, ...}

    def build_index(self):
        """Build or rebuild the full search index from conversation files."""
        self._inverted_index.clear()
        self._doc_count = 0
        self._doc_lengths.clear()
        self._conv_meta.clear()

        if not os.path.exists(self._conv_dir):
            return

        for fname in os.listdir(self._conv_dir):
            if not fname.endswith(".json"):
                continue
            conv_id = fname[:-5]
            filepath = os.path.join(self._conv_dir, fname)

            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    conv = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            self._conv_meta[conv_id] = {
                "title": conv.get("title", "Untitled"),
                "created_at": conv.get("created_at", ""),
            }

            for msg in conv.get("messages", []):
                msg_id = msg.get("id", "")
                content = msg.get("content", "")
                if not content or not msg_id:
                    continue

                self._index_message(conv_id, msg_id, content)

        # Persist index for faster subsequent loads
        self._save_index()

    def _index_message(self, conv_id: str, msg_id: str, content: str):
        """Add a single message to the inverted index."""
        tokens = _tokenize(content)
        if not tokens:
            return

        doc_key = f"{conv_id}:{msg_id}"
        self._doc_lengths[doc_key] = len(tokens)
        self._doc_count += 1

        # Count term frequencies
        tf = Counter(tokens)
        max_tf = max(tf.values()) if tf else 1

        for term, count in tf.items():
            normalized_tf = 0.5 + 0.5 * (count / max_tf)
            self._inverted_index[term].append((conv_id, msg_id, normalized_tf))

    def search(
        self,
        query: str,
        top_k: int = 20,
        role_filter: Optional[str] = None,
        conv_id_filter: Optional[str] = None,
    ) -> SearchResults:
        """
        Search across all conversations.

        Args:
            query: Search query string.
            top_k: Maximum results to return.
            role_filter: Filter by message role ("user" or "assistant").
            conv_id_filter: Limit search to a specific conversation.
        """
        # Rebuild index if empty
        if not self._inverted_index:
            self.build_index()

        query_tokens = _tokenize(query)
        if not query_tokens:
            return SearchResults(query=query)

        # Score each document using TF-IDF
        doc_scores: Dict[str, float] = defaultdict(float)

        for term in query_tokens:
            if term not in self._inverted_index:
                continue

            postings = self._inverted_index[term]
            idf = math.log((1 + self._doc_count) / (1 + len(postings))) + 1

            for conv_id, msg_id, tf in postings:
                if conv_id_filter and conv_id != conv_id_filter:
                    continue
                doc_key = f"{conv_id}:{msg_id}"
                doc_scores[doc_key] += tf * idf

        if not doc_scores:
            return SearchResults(
                query=query,
                total_conversations_searched=len(self._conv_meta),
                total_messages_searched=self._doc_count,
            )

        # Normalize scores by document length
        for doc_key in doc_scores:
            length = self._doc_lengths.get(doc_key, 1)
            doc_scores[doc_key] /= math.sqrt(length)

        # Sort and take top_k
        sorted_docs = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)[: top_k * 2]

        # Build results by loading actual messages
        results = []
        for doc_key, score in sorted_docs:
            conv_id, msg_id = doc_key.split(":", 1)
            msg = self._load_message(conv_id, msg_id)
            if msg is None:
                continue

            if role_filter and msg.get("role") != role_filter:
                continue

            content = msg.get("content", "")
            snippet = self._make_snippet(content, query_tokens)
            conv_meta = self._conv_meta.get(conv_id, {})

            results.append(
                SearchResult(
                    conversation_id=conv_id,
                    conversation_title=conv_meta.get("title", "Untitled"),
                    message_id=msg_id,
                    role=msg.get("role", "unknown"),
                    content=content,
                    snippet=snippet,
                    score=score,
                    created_at=msg.get("created_at", ""),
                    escalated=msg.get("escalated", False),
                )
            )

            if len(results) >= top_k:
                break

        return SearchResults(
            query=query,
            results=results,
            total_conversations_searched=len(self._conv_meta),
            total_messages_searched=self._doc_count,
        )

    def _make_snippet(self, content: str, query_tokens: List[str], context_chars: int = 120) -> str:
        """Extract a relevant snippet from content, centered around query terms."""
        content_lower = content.lower()

        # Find the best position (where most query terms cluster)
        best_pos = 0
        best_score = 0

        for token in query_tokens:
            pos = content_lower.find(token)
            if pos >= 0:
                # Count nearby query terms
                window = content_lower[max(0, pos - context_chars) : pos + context_chars]
                score = sum(1 for t in query_tokens if t in window)
                if score > best_score:
                    best_score = score
                    best_pos = pos

        # Extract snippet
        start = max(0, best_pos - context_chars // 2)
        end = min(len(content), best_pos + context_chars)

        snippet = content[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet = snippet + "..."

        return snippet

    def _load_message(self, conv_id: str, msg_id: str) -> Optional[Dict[str, Any]]:
        """Load a specific message from a conversation file."""
        filepath = os.path.join(self._conv_dir, f"{conv_id}.json")
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                conv = json.load(f)
            for msg in conv.get("messages", []):
                if msg.get("id") == msg_id:
                    return msg
        except (json.JSONDecodeError, OSError):
            pass
        return None

    def _save_index(self):
        """Persist the inverted index to disk for faster loads."""
        data = {
            "doc_count": self._doc_count,
            "doc_lengths": self._doc_lengths,
            "conv_meta": self._conv_meta,
            "index": {
                term: [(cid, mid, tf) for cid, mid, tf in postings] for term, postings in self._inverted_index.items()
            },
        }
        os.makedirs(os.path.dirname(self._index_path) or ".", exist_ok=True)
        try:
            with open(self._index_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            pass  # Non-critical — index can be rebuilt

    def load_index(self) -> bool:
        """Load a previously saved index. Returns True if loaded."""
        if not os.path.exists(self._index_path):
            return False
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._doc_count = data.get("doc_count", 0)
            self._doc_lengths = data.get("doc_lengths", {})
            self._conv_meta = data.get("conv_meta", {})
            self._inverted_index = defaultdict(list)
            for term, postings in data.get("index", {}).items():
                self._inverted_index[term] = [(cid, mid, tf) for cid, mid, tf in postings]
            return True
        except (json.JSONDecodeError, OSError):
            return False
