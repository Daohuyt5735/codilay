"""Tests for codilay.graph_filter — dependency graph filtering."""

from codilay.graph_filter import FilteredGraph, GraphFilter, GraphFilterOptions


def _make_wires():
    return [
        {"from": "src/api/routes.py", "to": "src/auth/handler.py", "type": "import"},
        {"from": "src/api/routes.py", "to": "src/db/models.py", "type": "import"},
        {"from": "src/auth/handler.py", "to": "src/db/models.py", "type": "call"},
        {"from": "src/auth/handler.py", "to": "src/utils/crypto.py", "type": "call"},
        {"from": "tests/test_auth.py", "to": "src/auth/handler.py", "type": "import"},
        {"from": "src/utils/crypto.py", "to": "src/utils/constants.py", "type": "reference"},
    ]


# ── Available filters ────────────────────────────────────────────────────────


def test_get_available_filters():
    gf = GraphFilter(_make_wires())
    avail = gf.get_available_filters()

    assert "import" in avail["wire_types"]
    assert "call" in avail["wire_types"]
    assert "reference" in avail["wire_types"]
    assert len(avail["files"]) > 0
    assert len(avail["layers"]) > 0


# ── No filters (pass-through) ───────────────────────────────────────────────


def test_filter_no_options():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions())

    assert result.total_wires == 6
    assert result.filtered_wires == 6
    assert len(result.nodes) > 0
    assert len(result.edges) == 6


# ── Wire type filter ────────────────────────────────────────────────────────


def test_filter_by_wire_type():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions(wire_types=["import"]))

    assert result.filtered_wires == 3
    assert all(e.wire_type == "import" for e in result.edges)


def test_filter_multiple_wire_types():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions(wire_types=["import", "call"]))

    assert result.filtered_wires == 5
    assert all(e.wire_type in ("import", "call") for e in result.edges)


# ── Layer filter ─────────────────────────────────────────────────────────────


def test_filter_by_layer():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions(layers=["src/auth"]))

    # Should include any wire where src/auth/ is source or target
    assert result.filtered_wires > 0
    for e in result.edges:
        assert "src/auth" in e.source or "src/auth" in e.target


def test_filter_by_layer_inferred():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions(layers=["tests"]))

    assert result.filtered_wires >= 1


# ── Module filter ────────────────────────────────────────────────────────────


def test_filter_by_module():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions(modules=["handler*"]))

    assert result.filtered_wires > 0
    for e in result.edges:
        assert "handler" in e.source or "handler" in e.target


def test_filter_by_module_path():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions(modules=["src/utils/*"]))

    assert result.filtered_wires > 0


# ── Exclude filter ───────────────────────────────────────────────────────────


def test_filter_exclude():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions(exclude_files=["tests/*"]))

    for e in result.edges:
        assert "tests/" not in e.source
        assert "tests/" not in e.target


def test_filter_exclude_by_basename():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions(exclude_files=["constants.*"]))

    for e in result.edges:
        assert "constants" not in e.source
        assert "constants" not in e.target


# ── Direction filter ─────────────────────────────────────────────────────────


def test_filter_outgoing():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions(layers=["src/auth"], direction="outgoing"))

    for e in result.edges:
        assert "src/auth" in e.source


def test_filter_incoming():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions(layers=["src/auth"], direction="incoming"))

    for e in result.edges:
        assert "src/auth" in e.target


# ── Min connections ──────────────────────────────────────────────────────────


def test_filter_min_connections():
    gf = GraphFilter(_make_wires())
    result_all = gf.filter(GraphFilterOptions())
    result_min3 = gf.filter(GraphFilterOptions(min_connections=3))

    # Fewer nodes when filtering by min connections
    assert len(result_min3.nodes) <= len(result_all.nodes)
    for node in result_min3.nodes:
        assert node.incoming + node.outgoing >= 3


# ── Combined filters ────────────────────────────────────────────────────────


def test_combined_filters():
    gf = GraphFilter(_make_wires())
    result = gf.filter(
        GraphFilterOptions(
            wire_types=["import"],
            exclude_files=["tests/*"],
        )
    )

    for e in result.edges:
        assert e.wire_type == "import"
        assert "tests/" not in e.source


# ── FilteredGraph.to_dict ───────────────────────────────────────────────────


def test_to_dict():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions())
    d = result.to_dict()

    assert "nodes" in d
    assert "edges" in d
    assert "stats" in d
    assert d["stats"]["total_wires"] == 6
    assert d["stats"]["filtered_wires"] == 6


def test_available_wire_types_property():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions())
    assert "import" in result.available_wire_types


def test_available_layers_property():
    gf = GraphFilter(_make_wires())
    result = gf.filter(GraphFilterOptions())
    assert len(result.available_layers) > 0


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_empty_graph():
    gf = GraphFilter([])
    result = gf.filter(GraphFilterOptions())

    assert result.total_wires == 0
    assert result.filtered_wires == 0
    assert len(result.nodes) == 0
    assert len(result.edges) == 0


def test_wires_missing_fields():
    wires = [{"from": "a.py", "to": "", "type": "import"}, {"from": "", "to": "b.py", "type": "call"}]
    gf = GraphFilter(wires)
    result = gf.filter(GraphFilterOptions())
    # Wires with empty from/to should be skipped in edges
    assert result.filtered_wires == 0


def test_open_wires_included():
    closed = [{"from": "a.py", "to": "b.py", "type": "import"}]
    open_w = [{"from": "c.py", "to": "d.py", "type": "reference"}]
    gf = GraphFilter(closed, open_w)
    result = gf.filter(GraphFilterOptions())

    assert result.total_wires == 2
    assert result.filtered_wires == 2
