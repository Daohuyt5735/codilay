"""Tests for codilay.team_memory — shared team knowledge base."""

import os
import tempfile

from codilay.team_memory import TeamMemory


# ── User management ──────────────────────────────────────────────────────────


def test_register_user():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        user = tm.register_user("alice", "Alice Chen")

        assert user["username"] == "alice"
        assert user["display_name"] == "Alice Chen"
        assert user["role"] == "member"
        assert "id" in user


def test_register_user_idempotent():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        u1 = tm.register_user("alice")
        u2 = tm.register_user("alice")

        assert u1["id"] == u2["id"]
        users = tm.list_users()
        assert len(users) == 1


def test_list_users():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        tm.register_user("alice")
        tm.register_user("bob")

        users = tm.list_users()
        assert len(users) == 2
        names = {u["username"] for u in users}
        assert names == {"alice", "bob"}


def test_remove_user():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        tm.register_user("alice")
        tm.register_user("bob")

        assert tm.remove_user("alice") is True
        assert len(tm.list_users()) == 1
        assert tm.remove_user("nonexistent") is False


# ── Facts ────────────────────────────────────────────────────────────────────


def test_add_fact():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        fact = tm.add_fact("We use PostgreSQL", category="architecture", author="alice")

        assert fact["fact"] == "We use PostgreSQL"
        assert fact["category"] == "architecture"
        assert fact["author"] == "alice"
        assert "id" in fact


def test_list_facts():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        tm.add_fact("Fact 1", category="general")
        tm.add_fact("Fact 2", category="architecture")
        tm.add_fact("Fact 3", category="general")

        all_facts = tm.list_facts()
        assert len(all_facts) == 3

        arch_facts = tm.list_facts(category="architecture")
        assert len(arch_facts) == 1
        assert arch_facts[0]["fact"] == "Fact 2"


def test_remove_fact():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        fact = tm.add_fact("To be removed")

        assert tm.remove_fact(fact["id"]) is True
        assert len(tm.list_facts()) == 0
        assert tm.remove_fact("nonexistent") is False


def test_vote_fact():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        fact = tm.add_fact("Voteable fact")

        assert tm.vote_fact(fact["id"], "up") is True
        assert tm.vote_fact(fact["id"], "up") is True
        assert tm.vote_fact(fact["id"], "down") is True

        facts = tm.list_facts()
        assert facts[0]["upvotes"] == 2
        assert facts[0]["downvotes"] == 1


def test_vote_nonexistent_fact():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        assert tm.vote_fact("nonexistent", "up") is False


def test_facts_sorted_by_votes():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        f1 = tm.add_fact("Low voted")
        f2 = tm.add_fact("High voted")

        tm.vote_fact(f2["id"], "up")
        tm.vote_fact(f2["id"], "up")
        tm.vote_fact(f2["id"], "up")

        facts = tm.list_facts()
        assert facts[0]["fact"] == "High voted"


# ── Decisions ────────────────────────────────────────────────────────────────


def test_add_decision():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        dec = tm.add_decision("Use PostgreSQL", "Better JSON support", author="alice", related_files=["src/db/"])

        assert dec["title"] == "Use PostgreSQL"
        assert dec["status"] == "active"
        assert dec["related_files"] == ["src/db/"]


def test_list_decisions_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        d1 = tm.add_decision("Decision 1", "Active decision")
        d2 = tm.add_decision("Decision 2", "Will be deprecated")
        tm.update_decision_status(d2["id"], "deprecated")

        active = tm.list_decisions(status="active")
        assert len(active) == 1
        assert active[0]["title"] == "Decision 1"

        deprecated = tm.list_decisions(status="deprecated")
        assert len(deprecated) == 1


def test_update_decision_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        dec = tm.add_decision("Test", "Description")

        assert tm.update_decision_status(dec["id"], "superseded") is True
        decisions = tm.list_decisions()
        assert decisions[0]["status"] == "superseded"

        assert tm.update_decision_status("nonexistent", "active") is False


# ── Conventions ──────────────────────────────────────────────────────────────


def test_add_convention():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        conv = tm.add_convention(
            "Error Handling",
            "All API endpoints return structured errors",
            examples=['{"error": "msg", "code": 400}'],
            author="alice",
        )

        assert conv["name"] == "Error Handling"
        assert len(conv["examples"]) == 1


def test_list_conventions():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        tm.add_convention("Convention 1", "Desc 1")
        tm.add_convention("Convention 2", "Desc 2")

        convs = tm.list_conventions()
        assert len(convs) == 2


# ── Annotations ──────────────────────────────────────────────────────────────


def test_add_annotation():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        ann = tm.add_annotation("src/api.py", "This file needs refactoring", author="bob", line_range="10-25")

        assert ann["file_path"] == "src/api.py"
        assert ann["line_range"] == "10-25"


def test_get_annotations_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        tm.add_annotation("src/api.py", "Note 1")
        tm.add_annotation("src/db.py", "Note 2")
        tm.add_annotation("src/api.py", "Note 3")

        all_ann = tm.get_annotations()
        assert len(all_ann) == 3

        api_ann = tm.get_annotations(file_path="src/api.py")
        assert len(api_ann) == 2


def test_remove_annotation():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        ann = tm.add_annotation("src/api.py", "To remove")

        assert tm.remove_annotation(ann["id"]) is True
        assert len(tm.get_annotations()) == 0
        assert tm.remove_annotation("nonexistent") is False


# ── Build context ────────────────────────────────────────────────────────────


def test_build_context_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        assert tm.build_context() == ""


def test_build_context_with_data():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        tm.add_fact("We use Redis for caching", category="infrastructure")
        tm.add_decision("Use Redis", "Better than memcached")
        tm.add_convention("Naming", "Use snake_case for functions")

        context = tm.build_context()
        assert "Redis" in context
        assert "snake_case" in context


# ── Import from user memory ─────────────────────────────────────────────────


def test_import_from_user_memory():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        user_mem = {
            "facts": [
                {"fact": "Uses SQLAlchemy for ORM", "category": "tech"},
                {"fact": "Deployed on AWS", "category": "infra"},
            ]
        }

        imported = tm.import_from_user_memory(user_mem, author="alice")
        assert imported == 2
        assert len(tm.list_facts()) == 2


def test_import_deduplication():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm = TeamMemory(tmpdir)
        tm.add_fact("Existing fact")

        user_mem = {
            "facts": [
                {"fact": "Existing fact"},
                {"fact": "New fact"},
            ]
        }

        imported = tm.import_from_user_memory(user_mem)
        assert imported == 1
        assert len(tm.list_facts()) == 2


# ── Persistence ──────────────────────────────────────────────────────────────


def test_memory_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm1 = TeamMemory(tmpdir)
        tm1.add_fact("Persistent fact", category="test")
        tm1.add_decision("Persistent decision", "Description")

        tm2 = TeamMemory(tmpdir)
        assert len(tm2.list_facts()) == 1
        assert len(tm2.list_decisions()) == 1


def test_users_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        tm1 = TeamMemory(tmpdir)
        tm1.register_user("alice")

        tm2 = TeamMemory(tmpdir)
        assert len(tm2.list_users()) == 1
