"""Tests for codilay.triage_feedback — triage correction store."""

import os
import tempfile
from dataclasses import dataclass, field
from typing import List

from codilay.triage_feedback import TriageFeedbackEntry, TriageFeedbackStore


# ── Mock TriageResult to mimic triage.py's dataclass ─────────────────────────


@dataclass
class MockTriageResult:
    core: List[str] = field(default_factory=list)
    skim: List[str] = field(default_factory=list)
    skip: List[str] = field(default_factory=list)


# ── TriageFeedbackEntry ─────────────────────────────────────────────────────


def test_entry_to_dict():
    entry = TriageFeedbackEntry(
        file_path="src/auth.py",
        original_category="skip",
        corrected_category="core",
        reason="Important auth logic",
        is_pattern=False,
    )
    d = entry.to_dict()
    assert d["file_path"] == "src/auth.py"
    assert d["original_category"] == "skip"
    assert d["corrected_category"] == "core"
    assert d["reason"] == "Important auth logic"
    assert d["is_pattern"] is False
    assert d["created_at"]  # Should have a timestamp


def test_entry_from_dict():
    data = {
        "file_path": "tests/*.py",
        "original_category": "core",
        "corrected_category": "skip",
        "reason": "Tests should be skipped",
        "created_at": "2025-01-01T00:00:00",
        "is_pattern": True,
    }
    entry = TriageFeedbackEntry.from_dict(data)
    assert entry.file_path == "tests/*.py"
    assert entry.is_pattern is True
    assert entry.corrected_category == "skip"


def test_entry_from_dict_defaults():
    entry = TriageFeedbackEntry.from_dict({})
    assert entry.file_path == ""
    assert entry.reason == ""
    assert entry.is_pattern is False


# ── TriageFeedbackStore ──────────────────────────────────────────────────────


def test_store_add_and_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        store.add_feedback("src/auth.py", "skip", "core", reason="Auth is core")

        entries = store.list_feedback()
        assert len(entries) == 1
        assert entries[0].file_path == "src/auth.py"
        assert entries[0].corrected_category == "core"


def test_store_replaces_existing():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        store.add_feedback("src/auth.py", "skip", "core")
        store.add_feedback("src/auth.py", "skip", "skim")  # Override

        entries = store.list_feedback()
        assert len(entries) == 1
        assert entries[0].corrected_category == "skim"


def test_store_remove():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        store.add_feedback("src/auth.py", "skip", "core")
        store.add_feedback("src/db.py", "skim", "core")

        assert store.remove_feedback("src/auth.py") is True
        assert len(store.list_feedback()) == 1
        assert store.remove_feedback("nonexistent.py") is False


def test_store_clear():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        store.add_feedback("a.py", "skip", "core")
        store.add_feedback("b.py", "skip", "core")
        store.clear_feedback()

        assert len(store.list_feedback()) == 0


def test_store_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        store1 = TriageFeedbackStore(tmpdir)
        store1.add_feedback("src/auth.py", "skip", "core", reason="Important")

        # Reload from disk
        store2 = TriageFeedbackStore(tmpdir)
        entries = store2.list_feedback()
        assert len(entries) == 1
        assert entries[0].reason == "Important"


def test_store_project_hints():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        store.set_project_hint("react", "Treat hooks/ as core")
        store.set_project_hint("flutter", "Skip ios/ android/")

        hints = store.get_project_hints()
        assert hints["react"] == "Treat hooks/ as core"
        assert hints["flutter"] == "Skip ios/ android/"


def test_store_project_hints_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        store1 = TriageFeedbackStore(tmpdir)
        store1.set_project_hint("django", "Skip migrations/")

        store2 = TriageFeedbackStore(tmpdir)
        assert store2.get_project_hints()["django"] == "Skip migrations/"


# ── apply_to_triage ─────────────────────────────────────────────────────────


def test_apply_exact_path_override():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        store.add_feedback("src/auth.py", "skip", "core")

        triage = MockTriageResult(core=[], skim=[], skip=["src/auth.py", "src/other.py"])
        overrides = store.apply_to_triage(triage)

        assert overrides == 1
        assert "src/auth.py" in triage.core
        assert "src/auth.py" not in triage.skip
        assert "src/other.py" in triage.skip


def test_apply_pattern_override():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        store.add_feedback("tests/*.py", "core", "skip", is_pattern=True)

        triage = MockTriageResult(
            core=["tests/test_a.py", "tests/test_b.py", "src/main.py"],
            skim=[],
            skip=[],
        )
        overrides = store.apply_to_triage(triage)

        assert overrides == 2
        assert "tests/test_a.py" in triage.skip
        assert "tests/test_b.py" in triage.skip
        assert "src/main.py" in triage.core


def test_apply_no_move_if_already_correct():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        store.add_feedback("src/auth.py", "skip", "core")

        triage = MockTriageResult(core=["src/auth.py"], skim=[], skip=[])
        overrides = store.apply_to_triage(triage)

        assert overrides == 0


def test_apply_file_not_in_triage():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        store.add_feedback("missing.py", "skip", "core")

        triage = MockTriageResult(core=["other.py"], skim=[], skip=[])
        overrides = store.apply_to_triage(triage)

        assert overrides == 0


def test_apply_skim_to_core():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        store.add_feedback("src/utils.py", "skim", "core")

        triage = MockTriageResult(core=[], skim=["src/utils.py"], skip=[])
        overrides = store.apply_to_triage(triage)

        assert overrides == 1
        assert "src/utils.py" in triage.core
        assert "src/utils.py" not in triage.skim


# ── build_prompt_context ─────────────────────────────────────────────────────


def test_prompt_context_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        assert store.build_prompt_context() == ""


def test_prompt_context_with_entries():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        store.add_feedback("src/auth.py", "skip", "core", reason="Critical auth")

        context = store.build_prompt_context()
        assert "src/auth.py" in context
        assert "skip" in context
        assert "core" in context
        assert "Critical auth" in context


def test_prompt_context_with_hints():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TriageFeedbackStore(tmpdir)
        store.set_project_hint("react", "Treat hooks/ as core")

        context = store.build_prompt_context()
        assert "react" in context
        assert "Treat hooks/ as core" in context
