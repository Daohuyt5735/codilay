"""Tests for codilay.doc_differ — documentation diffing and version snapshots."""

import json
import os
import tempfile

from codilay.doc_differ import DocDiffer, DocDiffResult, DocVersionStore, SectionChange


# ── DocDiffer ────────────────────────────────────────────────────────────────


def test_diff_detects_added_sections():
    old_idx = {"overview": {"title": "Overview"}}
    old_cnt = {"overview": "Old overview content."}
    new_idx = {"overview": {"title": "Overview"}, "auth": {"title": "Auth"}}
    new_cnt = {"overview": "Old overview content.", "auth": "Auth module docs."}

    differ = DocDiffer(old_idx, old_cnt, [], [], new_idx, new_cnt, [], [])
    result = differ.diff()

    assert len(result.added_sections) == 1
    assert result.added_sections[0].section_id == "auth"
    assert result.added_sections[0].change_type == "added"


def test_diff_detects_removed_sections():
    old_idx = {"overview": {"title": "Overview"}, "legacy": {"title": "Legacy"}}
    old_cnt = {"overview": "Content.", "legacy": "Old stuff."}
    new_idx = {"overview": {"title": "Overview"}}
    new_cnt = {"overview": "Content."}

    differ = DocDiffer(old_idx, old_cnt, [], [], new_idx, new_cnt, [], [])
    result = differ.diff()

    assert len(result.removed_sections) == 1
    assert result.removed_sections[0].section_id == "legacy"


def test_diff_detects_modified_sections():
    old_idx = {"overview": {"title": "Overview"}}
    old_cnt = {"overview": "Version 1 content."}
    new_idx = {"overview": {"title": "Overview"}}
    new_cnt = {"overview": "Version 2 content with more detail."}

    differ = DocDiffer(old_idx, old_cnt, [], [], new_idx, new_cnt, [], [])
    result = differ.diff()

    assert len(result.modified_sections) == 1
    assert result.modified_sections[0].section_id == "overview"
    assert len(result.modified_sections[0].diff_lines) > 0


def test_diff_no_changes():
    idx = {"overview": {"title": "Overview"}}
    cnt = {"overview": "Same content."}

    differ = DocDiffer(idx, cnt, [], [], idx, cnt, [], [])
    result = differ.diff()

    assert not result.has_changes
    assert result.total_section_changes == 0


def test_diff_skips_meta_sections():
    """dependency-graph and unresolved-references should be excluded from diff."""
    old_idx = {"dependency-graph": {"title": "Graph"}, "overview": {"title": "Overview"}}
    old_cnt = {"dependency-graph": "old graph", "overview": "content"}
    new_idx = {"dependency-graph": {"title": "Graph"}, "overview": {"title": "Overview"}}
    new_cnt = {"dependency-graph": "new graph", "overview": "content"}

    differ = DocDiffer(old_idx, old_cnt, [], [], new_idx, new_cnt, [], [])
    result = differ.diff()

    assert result.total_section_changes == 0


def test_diff_wire_changes():
    old_closed = [{"from": "a.py", "to": "b.py", "type": "import"}]
    new_closed = [
        {"from": "a.py", "to": "b.py", "type": "import"},
        {"from": "a.py", "to": "c.py", "type": "call"},
    ]
    old_open = [{"from": "x.py", "to": "y.py", "type": "ref"}]
    new_open = []

    differ = DocDiffer({}, {}, old_closed, old_open, {}, {}, new_closed, new_open)
    result = differ.diff()

    assert result.new_closed_wires == 1
    assert result.lost_closed_wires == 0
    assert result.resolved_open_wires == 1
    assert result.new_open_wires == 0


def test_diff_sections_delta():
    old_idx = {"a": {"title": "A"}}
    new_idx = {"a": {"title": "A"}, "b": {"title": "B"}, "c": {"title": "C"}}

    differ = DocDiffer(old_idx, {"a": "x"}, [], [], new_idx, {"a": "x", "b": "y", "c": "z"}, [], [])
    result = differ.diff()

    assert result.sections_delta == 2


def test_diff_describe_diff_summary():
    old_idx = {"sec": {"title": "Section"}}
    old_cnt = {"sec": "Line 1\nLine 2\nLine 3"}
    new_idx = {"sec": {"title": "Section"}}
    new_cnt = {"sec": "Line 1\nLine 2 modified\nLine 3\nLine 4 added"}

    differ = DocDiffer(old_idx, old_cnt, [], [], new_idx, new_cnt, [], [])
    result = differ.diff()

    assert len(result.modified_sections) == 1
    assert result.modified_sections[0].summary  # Should have some summary text


def test_diff_to_dict():
    result = DocDiffResult()
    result.added_sections = [SectionChange(section_id="new", title="New", change_type="added", summary="New section")]
    result.new_closed_wires = 5
    result.old_run_time = "2025-01-01T00:00:00"
    result.new_run_time = "2025-01-02T00:00:00"

    d = result.to_dict()
    assert len(d["added_sections"]) == 1
    assert d["wire_changes"]["new_closed"] == 5
    assert d["old_run_time"] == "2025-01-01T00:00:00"


# ── DocVersionStore ──────────────────────────────────────────────────────────


def test_version_store_save_and_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = DocVersionStore(tmpdir)
        fname = store.save_snapshot(
            section_index={"s1": {"title": "S1"}},
            section_contents={"s1": "content"},
            closed_wires=[],
            open_wires=[],
            run_id="run1",
        )
        assert fname.startswith("snapshot_")
        assert fname.endswith(".json")

        snapshots = store.list_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0]["run_id"] == "run1"
        assert snapshots[0]["sections"] == 1


def test_version_store_load_snapshot():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = DocVersionStore(tmpdir)
        fname = store.save_snapshot(
            section_index={"s1": {"title": "S1"}},
            section_contents={"s1": "hello"},
            closed_wires=[{"from": "a", "to": "b", "type": "import"}],
            open_wires=[],
        )
        snap = store.load_snapshot(fname)
        assert snap is not None
        assert snap["section_contents"]["s1"] == "hello"
        assert len(snap["closed_wires"]) == 1


def test_version_store_get_latest():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = DocVersionStore(tmpdir)
        store.save_snapshot({"a": {"title": "A"}}, {"a": "first"}, [], [])

        import time

        time.sleep(0.05)  # Ensure different timestamps
        store.save_snapshot({"b": {"title": "B"}}, {"b": "second"}, [], [])

        latest = store.get_latest_snapshot()
        assert latest is not None
        assert "b" in latest["section_contents"]


def test_version_store_get_previous():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = DocVersionStore(tmpdir)
        store.save_snapshot({"a": {"title": "A"}}, {"a": "first"}, [], [], run_id="r1")

        import time

        time.sleep(0.05)
        store.save_snapshot({"b": {"title": "B"}}, {"b": "second"}, [], [], run_id="r2")

        prev = store.get_previous_snapshot()
        assert prev is not None
        assert "a" in prev["section_contents"]


def test_version_store_diff_latest():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = DocVersionStore(tmpdir)
        store.save_snapshot(
            {"overview": {"title": "Overview"}},
            {"overview": "Version 1"},
            [],
            [],
        )
        import time

        time.sleep(0.05)
        store.save_snapshot(
            {"overview": {"title": "Overview"}, "new": {"title": "New"}},
            {"overview": "Version 2", "new": "Brand new"},
            [],
            [],
        )

        result = store.diff_latest()
        assert result is not None
        assert result.has_changes
        assert len(result.added_sections) == 1
        assert len(result.modified_sections) == 1


def test_version_store_diff_latest_insufficient_snapshots():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = DocVersionStore(tmpdir)
        assert store.diff_latest() is None

        store.save_snapshot({}, {}, [], [])
        assert store.diff_latest() is None


def test_version_store_cleanup():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = DocVersionStore(tmpdir)
        # Save 25 snapshots
        for i in range(25):
            store.save_snapshot({"s": {"title": f"S{i}"}}, {"s": f"c{i}"}, [], [])

        snapshots = store.list_snapshots()
        assert len(snapshots) <= 20


def test_version_store_load_nonexistent():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = DocVersionStore(tmpdir)
        assert store.load_snapshot("doesnt_exist.json") is None
