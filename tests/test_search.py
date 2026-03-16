"""Tests for codilay.search — full-text conversation search."""

import json
import os
import tempfile

from codilay.search import ConversationSearch, SearchResults, _tokenize


# ── Tokenizer ────────────────────────────────────────────────────────────────


def test_tokenize_basic():
    tokens = _tokenize("Hello world from Python")
    assert "hello" in tokens
    assert "world" in tokens
    assert "python" in tokens


def test_tokenize_filters_stop_words():
    tokens = _tokenize("the quick brown fox is a very fast animal")
    assert "the" not in tokens
    assert "is" not in tokens
    assert "a" not in tokens
    assert "very" not in tokens
    assert "quick" in tokens
    assert "brown" in tokens
    assert "fox" in tokens


def test_tokenize_filters_short_words():
    tokens = _tokenize("I am a x y z big word")
    assert "x" not in tokens
    assert "y" not in tokens
    assert "z" not in tokens
    assert "big" in tokens


def test_tokenize_handles_code():
    tokens = _tokenize("function verify_token(jwt_string)")
    assert "function" in tokens
    assert "verify_token" in tokens
    assert "jwt_string" in tokens


def test_tokenize_empty():
    assert _tokenize("") == []
    assert _tokenize("the is a") == []


# ── Helper to create test conversations ──────────────────────────────────────


def _setup_conversations(tmpdir):
    """Create fake conversation files for testing."""
    conv_dir = os.path.join(tmpdir, "chat", "conversations")
    os.makedirs(conv_dir, exist_ok=True)

    conv1 = {
        "title": "Auth Discussion",
        "created_at": "2025-01-01T00:00:00",
        "messages": [
            {"id": "m1", "role": "user", "content": "How does the authentication flow work?"},
            {
                "id": "m2",
                "role": "assistant",
                "content": "The authentication uses JWT tokens. The verify_token function validates each request.",
            },
            {"id": "m3", "role": "user", "content": "What about refresh tokens?"},
            {
                "id": "m4",
                "role": "assistant",
                "content": "Refresh tokens are stored in the database and rotated on each use.",
            },
        ],
    }

    conv2 = {
        "title": "Database Migration",
        "created_at": "2025-01-02T00:00:00",
        "messages": [
            {"id": "m5", "role": "user", "content": "How do we handle database migrations?"},
            {
                "id": "m6",
                "role": "assistant",
                "content": "We use Alembic for database migrations. Each migration is versioned and reversible.",
            },
        ],
    }

    with open(os.path.join(conv_dir, "conv1.json"), "w") as f:
        json.dump(conv1, f)
    with open(os.path.join(conv_dir, "conv2.json"), "w") as f:
        json.dump(conv2, f)

    return conv_dir


# ── Build index ──────────────────────────────────────────────────────────────


def test_build_index():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)
        search = ConversationSearch(tmpdir)
        search.build_index()

        assert search._doc_count == 6  # 4 + 2 messages
        assert len(search._conv_meta) == 2


def test_build_index_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        search = ConversationSearch(tmpdir)
        search.build_index()
        assert search._doc_count == 0


# ── Search ───────────────────────────────────────────────────────────────────


def test_search_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)
        search = ConversationSearch(tmpdir)
        search.build_index()

        results = search.search("authentication JWT tokens")
        assert len(results.results) > 0
        assert results.total_conversations_searched == 2
        # The auth conversation should rank highest
        assert results.results[0].conversation_id == "conv1"


def test_search_database():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)
        search = ConversationSearch(tmpdir)
        search.build_index()

        results = search.search("database migrations Alembic")
        assert len(results.results) > 0
        top = results.results[0]
        assert top.conversation_id == "conv2"


def test_search_no_results():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)
        search = ConversationSearch(tmpdir)
        search.build_index()

        results = search.search("kubernetes deployment helm")
        assert len(results.results) == 0


def test_search_empty_query():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)
        search = ConversationSearch(tmpdir)
        search.build_index()

        results = search.search("")
        assert len(results.results) == 0


def test_search_stop_words_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)
        search = ConversationSearch(tmpdir)
        search.build_index()

        results = search.search("the is a an")
        assert len(results.results) == 0


# ── Filters ──────────────────────────────────────────────────────────────────


def test_search_role_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)
        search = ConversationSearch(tmpdir)
        search.build_index()

        results = search.search("authentication", role_filter="assistant")
        for r in results.results:
            assert r.role == "assistant"


def test_search_conversation_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)
        search = ConversationSearch(tmpdir)
        search.build_index()

        results = search.search("tokens", conv_id_filter="conv1")
        for r in results.results:
            assert r.conversation_id == "conv1"


def test_search_top_k():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)
        search = ConversationSearch(tmpdir)
        search.build_index()

        results = search.search("tokens", top_k=1)
        assert len(results.results) <= 1


# ── Snippets ─────────────────────────────────────────────────────────────────


def test_snippet_extraction():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)
        search = ConversationSearch(tmpdir)
        search.build_index()

        results = search.search("verify_token")
        assert len(results.results) > 0
        # Snippet should contain the query term
        assert "verify_token" in results.results[0].snippet


# ── SearchResults.to_dict ────────────────────────────────────────────────────


def test_results_to_dict():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)
        search = ConversationSearch(tmpdir)
        search.build_index()

        results = search.search("authentication")
        d = results.to_dict()
        assert d["query"] == "authentication"
        assert "total_results" in d
        assert "results" in d
        assert isinstance(d["results"], list)


# ── Index persistence ────────────────────────────────────────────────────────


def test_save_and_load_index():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)

        # Build and save
        search1 = ConversationSearch(tmpdir)
        search1.build_index()

        # Load from saved
        search2 = ConversationSearch(tmpdir)
        loaded = search2.load_index()
        assert loaded is True
        assert search2._doc_count == search1._doc_count

        # Search should work with loaded index
        results = search2.search("authentication")
        assert len(results.results) > 0


def test_load_index_nonexistent():
    with tempfile.TemporaryDirectory() as tmpdir:
        search = ConversationSearch(tmpdir)
        assert search.load_index() is False


# ── Auto-rebuild on search ───────────────────────────────────────────────────


def test_search_auto_builds_index():
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_conversations(tmpdir)
        search = ConversationSearch(tmpdir)
        # Don't call build_index() explicitly
        results = search.search("authentication")
        assert len(results.results) > 0  # Index was built on-demand
