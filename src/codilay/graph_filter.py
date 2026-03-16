"""
Graph Filters — filter the dependency graph by wire type, file layer, or
module to reduce noise on large repos.

Used by both the CLI `codilay graph` command and the web UI's graph view.
"""

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class GraphFilterOptions:
    """Filter options for the dependency graph."""

    # Wire type filters
    wire_types: Optional[List[str]] = None  # e.g., ["import", "call", "reference"]

    # File layer filters (by directory prefix)
    layers: Optional[List[str]] = None  # e.g., ["src/routes", "src/models"]

    # Module filters (by pattern)
    modules: Optional[List[str]] = None  # e.g., ["auth*", "user*"]

    # Exclude patterns
    exclude_files: Optional[List[str]] = None  # e.g., ["*.test.*", "utils/*"]

    # Direction filter
    direction: str = "both"  # "incoming", "outgoing", "both"

    # Depth limit (hops from filtered nodes)
    max_depth: Optional[int] = None

    # Minimum connections to show a node
    min_connections: int = 0


@dataclass
class FilteredNode:
    """A node in the filtered graph."""

    path: str
    label: str  # Short display name
    layer: str  # Inferred layer (directory prefix)
    incoming: int = 0
    outgoing: int = 0


@dataclass
class FilteredEdge:
    """An edge in the filtered graph."""

    source: str
    target: str
    wire_type: str
    summary: str = ""
    wire_id: str = ""


@dataclass
class FilteredGraph:
    """Result of applying filters to the dependency graph."""

    nodes: List[FilteredNode] = field(default_factory=list)
    edges: List[FilteredEdge] = field(default_factory=list)
    total_wires: int = 0  # Total before filtering
    filtered_wires: int = 0  # After filtering
    applied_filters: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [
                {
                    "path": n.path,
                    "label": n.label,
                    "layer": n.layer,
                    "incoming": n.incoming,
                    "outgoing": n.outgoing,
                }
                for n in self.nodes
            ],
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "type": e.wire_type,
                    "summary": e.summary,
                }
                for e in self.edges
            ],
            "stats": {
                "total_wires": self.total_wires,
                "filtered_wires": self.filtered_wires,
                "nodes": len(self.nodes),
                "edges": len(self.edges),
            },
            "filters": self.applied_filters,
        }

    @property
    def available_wire_types(self) -> List[str]:
        return sorted(set(e.wire_type for e in self.edges))

    @property
    def available_layers(self) -> List[str]:
        return sorted(set(n.layer for n in self.nodes))


class GraphFilter:
    """
    Filters the CodiLay dependency graph (from links.json / wire state)
    according to user-specified criteria.
    """

    def __init__(
        self,
        closed_wires: List[Dict[str, Any]],
        open_wires: Optional[List[Dict[str, Any]]] = None,
    ):
        self._closed = closed_wires
        self._open = open_wires or []
        self._all_wires = self._closed + self._open

    def get_available_filters(self) -> Dict[str, List[str]]:
        """Return the available filter values for the current graph."""
        wire_types: Set[str] = set()
        layers: Set[str] = set()
        files: Set[str] = set()

        for w in self._all_wires:
            wire_types.add(w.get("type", "unknown"))
            for key in ("from", "to"):
                path = w.get(key, "")
                if path:
                    files.add(path)
                    layers.add(self._infer_layer(path))

        return {
            "wire_types": sorted(wire_types),
            "layers": sorted(layers),
            "files": sorted(files),
        }

    def filter(self, options: GraphFilterOptions) -> FilteredGraph:
        """Apply filters and return the filtered graph."""
        result = FilteredGraph(
            total_wires=len(self._all_wires),
            applied_filters={},
        )

        # Start with all wires
        wires = list(self._all_wires)

        # Apply wire type filter
        if options.wire_types:
            wires = [w for w in wires if w.get("type", "unknown") in options.wire_types]
            result.applied_filters["wire_types"] = options.wire_types

        # Apply layer filter
        if options.layers:
            wires = [
                w
                for w in wires
                if self._matches_layers(w.get("from", ""), options.layers)
                or self._matches_layers(w.get("to", ""), options.layers)
            ]
            result.applied_filters["layers"] = options.layers

        # Apply module filter
        if options.modules:
            wires = [
                w
                for w in wires
                if self._matches_modules(w.get("from", ""), options.modules)
                or self._matches_modules(w.get("to", ""), options.modules)
            ]
            result.applied_filters["modules"] = options.modules

        # Apply exclude filter
        if options.exclude_files:
            wires = [
                w
                for w in wires
                if not self._matches_exclude(w.get("from", ""), options.exclude_files)
                and not self._matches_exclude(w.get("to", ""), options.exclude_files)
            ]
            result.applied_filters["exclude_files"] = options.exclude_files

        # Apply direction filter
        if options.direction != "both" and options.layers:
            layer_files = set()
            for w in self._all_wires:
                for key in ("from", "to"):
                    path = w.get(key, "")
                    if self._matches_layers(path, options.layers):
                        layer_files.add(path)

            if options.direction == "outgoing":
                wires = [w for w in wires if w.get("from", "") in layer_files]
            elif options.direction == "incoming":
                wires = [w for w in wires if w.get("to", "") in layer_files]
            result.applied_filters["direction"] = options.direction

        # Build edges
        edges = []
        node_connections: Dict[str, Dict[str, int]] = {}  # path -> {incoming: N, outgoing: N}

        for w in wires:
            src = w.get("from", "")
            tgt = w.get("to", "")
            if not src or not tgt:
                continue

            edges.append(
                FilteredEdge(
                    source=src,
                    target=tgt,
                    wire_type=w.get("type", "unknown"),
                    summary=w.get("summary", w.get("context", "")),
                    wire_id=w.get("id", ""),
                )
            )

            if src not in node_connections:
                node_connections[src] = {"incoming": 0, "outgoing": 0}
            if tgt not in node_connections:
                node_connections[tgt] = {"incoming": 0, "outgoing": 0}
            node_connections[src]["outgoing"] += 1
            node_connections[tgt]["incoming"] += 1

        # Apply minimum connections filter
        if options.min_connections > 0:
            valid_nodes = {
                path
                for path, counts in node_connections.items()
                if counts["incoming"] + counts["outgoing"] >= options.min_connections
            }
            edges = [e for e in edges if e.source in valid_nodes and e.target in valid_nodes]
            node_connections = {p: c for p, c in node_connections.items() if p in valid_nodes}

        # Build nodes
        nodes = []
        for path, counts in sorted(node_connections.items()):
            nodes.append(
                FilteredNode(
                    path=path,
                    label=os.path.basename(path),
                    layer=self._infer_layer(path),
                    incoming=counts["incoming"],
                    outgoing=counts["outgoing"],
                )
            )

        result.nodes = nodes
        result.edges = edges
        result.filtered_wires = len(edges)

        return result

    # ── Helpers ───────────────────────────────────────────────────

    def _infer_layer(self, path: str) -> str:
        """Infer the architectural layer from a file path."""
        parts = path.replace("\\", "/").split("/")
        if len(parts) <= 1:
            return "root"
        # Return the first meaningful directory
        return parts[0]

    def _matches_layers(self, path: str, layers: List[str]) -> bool:
        """Check if a file path belongs to any of the specified layers."""
        for layer in layers:
            if path.startswith(layer) or path.startswith(layer + "/"):
                return True
            # Also check by inferred layer name
            if self._infer_layer(path) == layer:
                return True
        return False

    def _matches_modules(self, path: str, modules: List[str]) -> bool:
        """Check if a file matches any module pattern."""
        basename = os.path.basename(path)
        name_no_ext = os.path.splitext(basename)[0]
        for pattern in modules:
            if fnmatch.fnmatch(basename, pattern):
                return True
            if fnmatch.fnmatch(name_no_ext, pattern):
                return True
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    def _matches_exclude(self, path: str, patterns: List[str]) -> bool:
        """Check if a file matches any exclusion pattern."""
        for pattern in patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
            if fnmatch.fnmatch(os.path.basename(path), pattern):
                return True
        return False
