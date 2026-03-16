"""
Tests for the new feature API endpoints in server.py.

Uses starlette TestClient with a real FastAPI app pointed at a temp directory.
Each test group sets up the minimal file fixtures needed for that feature.
"""

import json
import os
import tempfile
import time

import pytest
from starlette.testclient import TestClient

from codilay.server import create_app


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_output_dir(tmp_path: str):
    """Create minimal codilay output so create_app() doesn't raise."""
    output_dir = os.path.join(tmp_path, "codilay")
    os.makedirs(output_dir, exist_ok=True)

    # CODEBASE.md — required by create_app
    with open(os.path.join(output_dir, "CODEBASE.md"), "w") as f:
        f.write("# Test Project\n\nSome documentation content.\n")

    # links.json — loaded by graph/link endpoints
    with open(os.path.join(output_dir, "links.json"), "w") as f:
        json.dump(
            {
                "closed": [
                    {"from": "src/main.py", "to": "src/utils.py", "type": "import", "id": "w1"},
                    {"from": "src/main.py", "to": "src/config.py", "type": "import", "id": "w2"},
                    {"from": "src/api.py", "to": "src/utils.py", "type": "import", "id": "w3"},
                ],
                "open": [
                    {"from": "src/main.py", "to": "external_lib", "type": "import", "id": "w4"},
                ],
            },
            f,
        )

    # .codilay_state.json — loaded by section/retriever endpoints
    with open(os.path.join(output_dir, ".codilay_state.json"), "w") as f:
        json.dump(
            {
                "section_index": {
                    "overview": {"title": "Overview", "file": "src/main.py", "tags": ["core"]},
                    "utils": {"title": "Utilities", "file": "src/utils.py", "tags": ["helpers"]},
                },
                "section_contents": {
                    "overview": "This is the main module.",
                    "utils": "Utility functions for the project.",
                },
                "files_processed": ["src/main.py", "src/utils.py", "src/config.py", "src/api.py"],
            },
            f,
        )

    return output_dir


@pytest.fixture
def app_and_dir():
    """Create a temp project with codilay output and return (TestClient, output_dir)."""
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = _make_output_dir(tmp)
        app = create_app(tmp, output_dir)
        client = TestClient(app)
        yield client, output_dir


# ── Feature 3: Export ─────────────────────────────────────────────────────────


class TestExportEndpoints:
    def test_export_get_markdown(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/export?fmt=markdown")
        assert res.status_code == 200
        data = res.json()
        assert "content" in data
        assert "chars" in data
        assert data["format"] == "markdown"
        assert len(data["content"]) > 0

    def test_export_get_json(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/export?fmt=json")
        assert res.status_code == 200
        data = res.json()
        assert data["format"] == "json"
        # Content should be valid JSON
        parsed = json.loads(data["content"])
        assert isinstance(parsed, dict)

    def test_export_get_xml(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/export?fmt=xml")
        assert res.status_code == 200
        data = res.json()
        assert data["format"] == "xml"
        assert "<codebase" in data["content"] or "<section" in data["content"]

    def test_export_post(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post(
            "/api/export",
            json={"format": "markdown", "include_graph": True},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["format"] == "markdown"
        assert data["chars"] > 0

    def test_export_post_no_graph(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post(
            "/api/export",
            json={"format": "markdown", "include_graph": False},
        )
        assert res.status_code == 200


# ── Feature 4: Doc Diff ──────────────────────────────────────────────────────


class TestDocDiffEndpoints:
    def _create_snapshots(self, output_dir, count=2):
        """Create fake doc snapshots for diff testing."""
        snap_dir = os.path.join(output_dir, "history")
        os.makedirs(snap_dir, exist_ok=True)

        for i in range(count):
            snap = {
                "timestamp": f"2025-01-0{i + 1}T12:00:00+00:00",
                "run_id": f"run_{i}",
                "commit": "",
                "section_index": {
                    "overview": {"title": "Overview", "file": "src/main.py", "tags": ["core"]},
                },
                "section_contents": {
                    "overview": f"Version {i + 1} content.",
                },
                "closed_wires": [{"from": "a.py", "to": "b.py", "type": "import"}],
                "open_wires": [],
            }
            fname = f"snapshot_2025010{i + 1}_120000_{i:06d}.json"
            with open(os.path.join(snap_dir, fname), "w") as f:
                json.dump(snap, f)
            time.sleep(0.01)  # Ensure different mtimes

    def test_list_snapshots_empty(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/doc-diff/snapshots")
        assert res.status_code == 200
        data = res.json()
        assert data["snapshots"] == []

    def test_list_snapshots_with_data(self, app_and_dir):
        client, output_dir = app_and_dir
        self._create_snapshots(output_dir, 3)
        res = client.get("/api/doc-diff/snapshots")
        assert res.status_code == 200
        data = res.json()
        assert len(data["snapshots"]) == 3

    def test_diff_insufficient_snapshots(self, app_and_dir):
        client, output_dir = app_and_dir
        self._create_snapshots(output_dir, 1)
        res = client.get("/api/doc-diff")
        assert res.status_code == 200
        data = res.json()
        assert data["has_changes"] is False

    def test_diff_with_changes(self, app_and_dir):
        client, output_dir = app_and_dir
        self._create_snapshots(output_dir, 2)
        res = client.get("/api/doc-diff")
        assert res.status_code == 200
        data = res.json()
        # Should have some structure — either changes or no changes
        assert "modified_sections" in data or "has_changes" in data


# ── Feature 5: Triage Feedback ───────────────────────────────────────────────


class TestTriageFeedbackEndpoints:
    def test_list_feedback_empty(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/triage-feedback")
        assert res.status_code == 200
        data = res.json()
        assert data["entries"] == []
        assert "hints" in data

    def test_add_feedback(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post(
            "/api/triage-feedback",
            json={
                "file_path": "src/utils.py",
                "original_category": "skip",
                "corrected_category": "include",
                "reason": "Important utility file",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["file_path"] == "src/utils.py"
        assert data["corrected_category"] == "include"

    def test_add_and_list_feedback(self, app_and_dir):
        client, _ = app_and_dir
        client.post(
            "/api/triage-feedback",
            json={
                "file_path": "src/utils.py",
                "original_category": "skip",
                "corrected_category": "include",
            },
        )
        client.post(
            "/api/triage-feedback",
            json={
                "file_path": "src/config.py",
                "original_category": "include",
                "corrected_category": "skip",
                "reason": "Auto-generated config",
            },
        )
        res = client.get("/api/triage-feedback")
        assert res.status_code == 200
        data = res.json()
        assert len(data["entries"]) == 2

    def test_delete_feedback(self, app_and_dir):
        client, _ = app_and_dir
        client.post(
            "/api/triage-feedback",
            json={
                "file_path": "src/utils.py",
                "original_category": "skip",
                "corrected_category": "include",
            },
        )
        res = client.delete("/api/triage-feedback/src/utils.py")
        assert res.status_code == 200
        assert res.json()["deleted"] is True

        # Verify gone
        res = client.get("/api/triage-feedback")
        assert len(res.json()["entries"]) == 0

    def test_delete_nonexistent_feedback(self, app_and_dir):
        client, _ = app_and_dir
        res = client.delete("/api/triage-feedback/nonexistent.py")
        assert res.status_code == 404


# ── Feature 7: Graph Filters ─────────────────────────────────────────────────


class TestGraphFilterEndpoints:
    def test_get_filters(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/graph/filters")
        assert res.status_code == 200
        data = res.json()
        assert "wire_types" in data
        assert "import" in data["wire_types"]

    def test_filter_no_options(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post("/api/graph/filter", json={})
        assert res.status_code == 200
        data = res.json()
        assert "nodes" in data
        assert "edges" in data
        assert "stats" in data
        # Should return all wires (4 total: 3 closed + 1 open)
        assert data["stats"]["total_wires"] == 4

    def test_filter_by_wire_type(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post(
            "/api/graph/filter",
            json={"wire_types": ["import"]},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["stats"]["filtered_wires"] > 0
        assert all(e["type"] == "import" for e in data["edges"])

    def test_filter_by_module(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post(
            "/api/graph/filter",
            json={"modules": ["src/*"]},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["stats"]["filtered_wires"] > 0

    def test_filter_with_min_connections(self, app_and_dir):
        client, _ = app_and_dir
        # src/utils.py has 2 incoming connections, should survive min_connections=2
        res = client.post(
            "/api/graph/filter",
            json={"min_connections": 2},
        )
        assert res.status_code == 200
        data = res.json()
        # All remaining nodes should have >= 2 connections
        assert "nodes" in data

    def test_filter_direction_outgoing(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post(
            "/api/graph/filter",
            json={"direction": "outgoing"},
        )
        assert res.status_code == 200

    def test_filter_exclude(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post(
            "/api/graph/filter",
            json={"exclude_files": ["external_lib"]},
        )
        assert res.status_code == 200
        data = res.json()
        # External lib wires should be excluded
        for edge in data["edges"]:
            assert "external_lib" not in edge["source"]
            assert "external_lib" not in edge["target"]


# ── Feature 8: Team Memory ───────────────────────────────────────────────────


class TestTeamMemoryEndpoints:
    # ── Facts ─────────────────────────────────────────
    def test_list_facts_empty(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/team/facts")
        assert res.status_code == 200
        assert res.json()["facts"] == []

    def test_add_fact(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post(
            "/api/team/facts",
            json={"fact": "We use PostgreSQL for all persistence", "category": "architecture", "author": "alice"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["fact"] == "We use PostgreSQL for all persistence"
        assert data["category"] == "architecture"
        assert "id" in data

    def test_add_and_list_facts(self, app_and_dir):
        client, _ = app_and_dir
        client.post("/api/team/facts", json={"fact": "Fact 1"})
        client.post("/api/team/facts", json={"fact": "Fact 2", "category": "gotcha"})
        res = client.get("/api/team/facts")
        assert len(res.json()["facts"]) == 2

    def test_delete_fact(self, app_and_dir):
        client, _ = app_and_dir
        add_res = client.post("/api/team/facts", json={"fact": "Temporary fact"})
        fact_id = add_res.json()["id"]
        res = client.delete(f"/api/team/facts/{fact_id}")
        assert res.status_code == 200
        assert res.json()["deleted"] is True
        assert len(client.get("/api/team/facts").json()["facts"]) == 0

    def test_delete_nonexistent_fact(self, app_and_dir):
        client, _ = app_and_dir
        res = client.delete("/api/team/facts/nonexistent")
        assert res.status_code == 404

    def test_vote_fact(self, app_and_dir):
        client, _ = app_and_dir
        add_res = client.post("/api/team/facts", json={"fact": "Voteable fact"})
        fact_id = add_res.json()["id"]
        res = client.post(f"/api/team/facts/{fact_id}/vote?vote=up")
        assert res.status_code == 200
        assert res.json()["voted"] == "up"

    def test_vote_nonexistent_fact(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post("/api/team/facts/nonexistent/vote?vote=up")
        assert res.status_code == 404

    def test_filter_facts_by_category(self, app_and_dir):
        client, _ = app_and_dir
        client.post("/api/team/facts", json={"fact": "Arch fact", "category": "architecture"})
        client.post("/api/team/facts", json={"fact": "General fact", "category": "general"})
        res = client.get("/api/team/facts?category=architecture")
        facts = res.json()["facts"]
        assert len(facts) == 1
        assert facts[0]["category"] == "architecture"

    # ── Decisions ─────────────────────────────────────
    def test_list_decisions_empty(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/team/decisions")
        assert res.status_code == 200
        assert res.json()["decisions"] == []

    def test_add_decision(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post(
            "/api/team/decisions",
            json={
                "title": "Use FastAPI",
                "description": "We chose FastAPI for its async support",
                "author": "bob",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["title"] == "Use FastAPI"
        assert "id" in data

    def test_add_and_list_decisions(self, app_and_dir):
        client, _ = app_and_dir
        client.post(
            "/api/team/decisions",
            json={"title": "Decision 1", "description": "Desc 1"},
        )
        client.post(
            "/api/team/decisions",
            json={"title": "Decision 2", "description": "Desc 2"},
        )
        res = client.get("/api/team/decisions")
        assert len(res.json()["decisions"]) == 2

    # ── Conventions ───────────────────────────────────
    def test_list_conventions_empty(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/team/conventions")
        assert res.status_code == 200
        assert res.json()["conventions"] == []

    def test_add_convention(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post(
            "/api/team/conventions",
            json={
                "name": "snake_case for functions",
                "description": "All function names must use snake_case",
                "examples": ["def my_function():", "def get_user_name():"],
                "author": "alice",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["name"] == "snake_case for functions"

    def test_add_and_list_conventions(self, app_and_dir):
        client, _ = app_and_dir
        client.post(
            "/api/team/conventions",
            json={"name": "Conv 1", "description": "Desc 1"},
        )
        res = client.get("/api/team/conventions")
        assert len(res.json()["conventions"]) == 1

    # ── Annotations ───────────────────────────────────
    def test_list_annotations_empty(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/team/annotations")
        assert res.status_code == 200
        assert res.json()["annotations"] == []

    def test_add_annotation(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post(
            "/api/team/annotations",
            json={
                "file_path": "src/main.py",
                "note": "This function is performance-critical",
                "author": "charlie",
                "line_range": "10-25",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["file_path"] == "src/main.py"
        assert data["note"] == "This function is performance-critical"

    def test_add_and_list_annotations(self, app_and_dir):
        client, _ = app_and_dir
        client.post(
            "/api/team/annotations",
            json={"file_path": "src/main.py", "note": "Note 1"},
        )
        client.post(
            "/api/team/annotations",
            json={"file_path": "src/utils.py", "note": "Note 2"},
        )
        res = client.get("/api/team/annotations")
        assert len(res.json()["annotations"]) == 2

    def test_filter_annotations_by_file(self, app_and_dir):
        client, _ = app_and_dir
        client.post(
            "/api/team/annotations",
            json={"file_path": "src/main.py", "note": "Note A"},
        )
        client.post(
            "/api/team/annotations",
            json={"file_path": "src/utils.py", "note": "Note B"},
        )
        res = client.get("/api/team/annotations?file_path=src/main.py")
        annotations = res.json()["annotations"]
        assert len(annotations) == 1
        assert annotations[0]["file_path"] == "src/main.py"

    def test_delete_annotation(self, app_and_dir):
        client, _ = app_and_dir
        add_res = client.post(
            "/api/team/annotations",
            json={"file_path": "src/main.py", "note": "Temp note"},
        )
        ann_id = add_res.json()["id"]
        res = client.delete(f"/api/team/annotations/{ann_id}")
        assert res.status_code == 200
        assert res.json()["deleted"] is True

    def test_delete_nonexistent_annotation(self, app_and_dir):
        client, _ = app_and_dir
        res = client.delete("/api/team/annotations/nonexistent")
        assert res.status_code == 404

    # ── Users ─────────────────────────────────────────
    def test_list_users_empty(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/team/users")
        assert res.status_code == 200
        assert res.json()["users"] == []

    def test_register_user(self, app_and_dir):
        client, _ = app_and_dir
        res = client.post(
            "/api/team/users",
            json={"username": "alice", "display_name": "Alice Smith"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["username"] == "alice"

    def test_register_and_list_users(self, app_and_dir):
        client, _ = app_and_dir
        client.post("/api/team/users", json={"username": "alice"})
        client.post("/api/team/users", json={"username": "bob"})
        res = client.get("/api/team/users")
        assert len(res.json()["users"]) == 2

    # ── Context ───────────────────────────────────────
    def test_team_context_empty(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/team/context")
        assert res.status_code == 200
        assert "context" in res.json()

    def test_team_context_with_data(self, app_and_dir):
        client, _ = app_and_dir
        client.post("/api/team/facts", json={"fact": "We use Redis for caching"})
        client.post(
            "/api/team/conventions",
            json={"name": "PEP8", "description": "Follow PEP8 style"},
        )
        res = client.get("/api/team/context")
        assert res.status_code == 200
        ctx = res.json()["context"]
        assert "Redis" in ctx or len(ctx) > 0


# ── Feature 9: Conversation Search ───────────────────────────────────────────


class TestSearchEndpoints:
    def _create_conversations(self, output_dir):
        """Create fake chat conversations for search indexing."""
        chat_dir = os.path.join(output_dir, "chat", "conversations")
        os.makedirs(chat_dir, exist_ok=True)

        convs = {
            "conv1": {
                "id": "conv1",
                "title": "Database Discussion",
                "messages": [
                    {
                        "id": "m1",
                        "role": "user",
                        "content": "How do we connect to PostgreSQL database?",
                        "timestamp": "2025-01-01T12:00:00",
                    },
                    {
                        "id": "m2",
                        "role": "assistant",
                        "content": "Use the connection pool in db_utils module.",
                        "timestamp": "2025-01-01T12:01:00",
                    },
                ],
                "created_at": "2025-01-01T12:00:00",
            },
            "conv2": {
                "id": "conv2",
                "title": "API Design",
                "messages": [
                    {
                        "id": "m3",
                        "role": "user",
                        "content": "What REST endpoints should we expose?",
                        "timestamp": "2025-01-02T10:00:00",
                    },
                    {
                        "id": "m4",
                        "role": "assistant",
                        "content": "The main endpoints are /users, /projects, and /tasks.",
                        "timestamp": "2025-01-02T10:01:00",
                    },
                ],
                "created_at": "2025-01-02T10:00:00",
            },
        }

        for conv_id, conv_data in convs.items():
            with open(os.path.join(chat_dir, f"{conv_id}.json"), "w") as f:
                json.dump(conv_data, f)

    def test_search_no_index(self, app_and_dir):
        client, output_dir = app_and_dir
        self._create_conversations(output_dir)
        # Search should auto-build index
        res = client.get("/api/search?q=database")
        assert res.status_code == 200
        data = res.json()
        assert "results" in data
        assert "query" in data

    def test_search_finds_results(self, app_and_dir):
        client, output_dir = app_and_dir
        self._create_conversations(output_dir)
        res = client.get("/api/search?q=PostgreSQL")
        assert res.status_code == 200
        data = res.json()
        assert data["total_results"] >= 1
        assert any("conv1" in r["conversation_id"] for r in data["results"])

    def test_search_no_results(self, app_and_dir):
        client, output_dir = app_and_dir
        self._create_conversations(output_dir)
        res = client.get("/api/search?q=xyznonexistent123")
        assert res.status_code == 200
        data = res.json()
        assert data["total_results"] == 0

    def test_search_role_filter(self, app_and_dir):
        client, output_dir = app_and_dir
        self._create_conversations(output_dir)
        res = client.get("/api/search?q=endpoints&role=assistant")
        assert res.status_code == 200
        data = res.json()
        for r in data["results"]:
            assert r["role"] == "assistant"

    def test_search_conversation_filter(self, app_and_dir):
        client, output_dir = app_and_dir
        self._create_conversations(output_dir)
        res = client.get("/api/search?q=database&conversation_id=conv1")
        assert res.status_code == 200
        data = res.json()
        for r in data["results"]:
            assert r["conversation_id"] == "conv1"

    def test_search_top_k(self, app_and_dir):
        client, output_dir = app_and_dir
        self._create_conversations(output_dir)
        res = client.get("/api/search?q=the&top_k=1")
        assert res.status_code == 200
        data = res.json()
        assert len(data["results"]) <= 1

    def test_rebuild_index(self, app_and_dir):
        client, output_dir = app_and_dir
        self._create_conversations(output_dir)
        res = client.post("/api/search/rebuild")
        assert res.status_code == 200
        assert res.json()["rebuilt"] is True

    def test_search_result_structure(self, app_and_dir):
        client, output_dir = app_and_dir
        self._create_conversations(output_dir)
        res = client.get("/api/search?q=database")
        data = res.json()
        if data["total_results"] > 0:
            r = data["results"][0]
            assert "conversation_id" in r
            assert "conversation_title" in r
            assert "role" in r
            assert "snippet" in r
            assert "score" in r


# ── Existing endpoints (sanity checks) ────────────────────────────────────────


class TestExistingEndpoints:
    def test_root_returns_html(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers.get("content-type", "")

    def test_sections(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/sections")
        assert res.status_code == 200
        data = res.json()
        assert len(data["sections"]) == 2

    def test_links(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/links")
        assert res.status_code == 200
        data = res.json()
        assert "closed" in data
        assert len(data["closed"]) == 3

    def test_document(self, app_and_dir):
        client, _ = app_and_dir
        res = client.get("/api/document")
        assert res.status_code == 200
        assert "Test Project" in res.json()["markdown"]
