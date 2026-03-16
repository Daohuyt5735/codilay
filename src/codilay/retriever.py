"""
Retriever — targeted, token-efficient section retrieval for chat.

Uses TF-IDF scoring over documentation sections so that chat context
only includes the 3-5 most relevant sections instead of the full
CODEBASE.md. This reduces per-question token costs by ~75-85%.

No external dependencies — pure Python implementation.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Scored result ─────────────────────────────────────────────────────────────


@dataclass
class ScoredSection:
    """A documentation section with a relevance score."""

    section_id: str
    title: str
    file: str
    tags: List[str]
    content: str
    score: float

    @property
    def formatted(self) -> str:
        """Format for LLM context injection."""
        parts = [f"## {self.title}"]
        if self.file:
            parts.append(f"> File: `{self.file}`")
            parts.append("")
        parts.append(self.content)
        return "\n".join(parts)


# ── Tokenizer ────────────────────────────────────────────────────────────────

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "in", "on", "at", "to",
    "for", "of", "with", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "here", "there", "when",
    "where", "why", "how", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "because", "but", "and",
    "or", "if", "while", "about", "what", "which", "who", "whom", "this",
    "that", "these", "those", "it", "its", "i", "me", "my", "we", "our",
    "you", "your", "he", "she", "they", "them", "their",
})

_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]*", re.IGNORECASE)


def _tokenize(text: str) -> List[str]:
    """
    Split text into lowercase tokens, removing stop words.
    Handles camelCase and snake_case splitting.
    """
    # Split camelCase: "getUserById" → "get user by id"
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    # Split snake_case: "get_user_by_id" → "get user by id"
    text = text.replace("_", " ").replace("-", " ").replace("/", " ").replace(".", " ")

    tokens = _TOKEN_RE.findall(text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


# ── TF-IDF Index ─────────────────────────────────────────────────────────────


class Retriever:
    """
    Section-level retrieval for chat context.

    Builds a TF-IDF index over documentation sections (title, file,
    tags, content) and scores queries against it.  Token-budget-aware:
    returns sections that fit within a specified token limit.
    """

    def __init__(self, section_index: Dict[str, Dict[str, Any]], section_contents: Dict[str, str]):
        self._sections: Dict[str, Dict[str, Any]] = {}
        self._idf: Dict[str, float] = {}
        self._tf_cache: Dict[str, Counter] = {}

        # Build internal section representations
        for sid, meta in section_index.items():
            content = section_contents.get(sid, "")
            self._sections[sid] = {
                "title": meta.get("title", sid),
                "file": meta.get("file", ""),
                "tags": meta.get("tags", []),
                "content": content,
            }

        self._build_index()

    # ── Index building ───────────────────────────────────────────

    def _build_index(self):
        """Build TF-IDF index over all sections."""
        n_docs = len(self._sections)
        if n_docs == 0:
            return

        # Document frequency — how many sections contain each term
        df: Counter = Counter()

        for sid, sec in self._sections.items():
            doc_text = self._section_to_text(sec)
            tokens = _tokenize(doc_text)
            tf = Counter(tokens)
            self._tf_cache[sid] = tf
            # DF counts unique terms per document
            for term in set(tokens):
                df[term] += 1

        # IDF = log(N / df)  with smoothing
        for term, freq in df.items():
            self._idf[term] = math.log((n_docs + 1) / (freq + 1)) + 1

    def _section_to_text(self, sec: Dict) -> str:
        """
        Build searchable text for a section.
        Title and tags are repeated to boost their weight.
        """
        title = sec.get("title", "")
        file_ref = sec.get("file", "")
        tags = " ".join(sec.get("tags", []))
        content = sec.get("content", "")

        # Weight: title ×3, tags ×2, file ×2, content ×1
        return f"{title} {title} {title} {tags} {tags} {file_ref} {file_ref} {content}"

    # ── Search ───────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5) -> List[ScoredSection]:
        """
        Score all sections against a query using TF-IDF cosine similarity.
        Returns top-k results sorted by relevance.
        """
        if not self._sections:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        query_tf = Counter(query_tokens)

        scored = []
        for sid, sec in self._sections.items():
            doc_tf = self._tf_cache.get(sid, Counter())
            score = self._cosine_score(query_tf, doc_tf)

            # Boost exact file path matches
            file_ref = sec.get("file", "").lower()
            query_lower = query.lower()
            if file_ref and file_ref in query_lower:
                score *= 2.0
            elif file_ref:
                file_parts = file_ref.replace("/", " ").replace(".", " ").split()
                if any(p in query_lower for p in file_parts if len(p) > 2):
                    score *= 1.5

            if score > 0:
                scored.append(ScoredSection(
                    section_id=sid,
                    title=sec["title"],
                    file=sec.get("file", ""),
                    tags=sec.get("tags", []),
                    content=sec.get("content", ""),
                    score=score,
                ))

        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:top_k]

    def search_by_file(self, file_path: str) -> List[ScoredSection]:
        """Get sections that document a specific file."""
        results = []
        for sid, sec in self._sections.items():
            if sec.get("file") == file_path:
                results.append(ScoredSection(
                    section_id=sid,
                    title=sec["title"],
                    file=sec.get("file", ""),
                    tags=sec.get("tags", []),
                    content=sec.get("content", ""),
                    score=1.0,
                ))
        return results

    def search_by_tags(self, tags: List[str]) -> List[ScoredSection]:
        """Get sections matching specific tags."""
        tag_set = set(t.lower() for t in tags)
        results = []
        for sid, sec in self._sections.items():
            sec_tags = set(t.lower() for t in sec.get("tags", []))
            overlap = sec_tags & tag_set
            if overlap:
                results.append(ScoredSection(
                    section_id=sid,
                    title=sec["title"],
                    file=sec.get("file", ""),
                    tags=sec.get("tags", []),
                    content=sec.get("content", ""),
                    score=len(overlap) / len(tag_set),
                ))
        results.sort(key=lambda x: x.score, reverse=True)
        return results

    # ── Token-budgeted context builder ────────────────────────────

    def build_context(
        self,
        query: str,
        token_counter,
        token_budget: int = 3000,
        top_k: int = 5,
    ) -> str:
        """
        Retrieve sections and build context that fits within a token budget.

        Args:
            query: The user's question.
            token_counter: callable(str) -> int, for counting tokens.
            token_budget: Max tokens for the context block.
            top_k: Max sections to consider.

        Returns:
            Formatted context string ready for LLM injection.
        """
        results = self.search(query, top_k=top_k)
        if not results:
            return ""

        context_parts = []
        tokens_used = 0

        for result in results:
            formatted = result.formatted
            section_tokens = token_counter(formatted)

            if tokens_used + section_tokens > token_budget:
                # Try to fit a truncated version
                remaining = token_budget - tokens_used
                if remaining > 200:
                    # Rough char-to-token ratio
                    max_chars = int(remaining * 3.5)
                    truncated_content = result.content[:max_chars] + "\n\n… [truncated]"
                    formatted = f"## {result.title}\n> File: `{result.file}`\n\n{truncated_content}"
                    context_parts.append(formatted)
                break

            context_parts.append(formatted)
            tokens_used += section_tokens

        return "\n\n---\n\n".join(context_parts)

    def get_source_files(self, query: str, top_k: int = 5) -> List[str]:
        """
        Get the file paths most relevant to a query.
        Useful for deep-agent file selection.
        """
        results = self.search(query, top_k=top_k * 2)
        files = []
        seen = set()
        for r in results:
            if r.file and r.file not in seen:
                files.append(r.file)
                seen.add(r.file)
            if len(files) >= top_k:
                break
        return files

    # ── Scoring internals ─────────────────────────────────────────

    def _cosine_score(self, query_tf: Counter, doc_tf: Counter) -> float:
        """Compute TF-IDF cosine similarity between query and document."""
        # Query TF-IDF vector
        q_tfidf = {}
        for term, tf in query_tf.items():
            idf = self._idf.get(term, 0)
            q_tfidf[term] = tf * idf

        # Doc TF-IDF vector (only for shared terms)
        dot_product = 0.0
        q_norm = 0.0
        d_norm = 0.0

        for term, q_val in q_tfidf.items():
            q_norm += q_val ** 2
            d_tf = doc_tf.get(term, 0)
            d_idf = self._idf.get(term, 0)
            d_val = d_tf * d_idf
            d_norm += d_val ** 2
            dot_product += q_val * d_val

        if q_norm == 0 or d_norm == 0:
            return 0.0

        return dot_product / (math.sqrt(q_norm) * math.sqrt(d_norm))
