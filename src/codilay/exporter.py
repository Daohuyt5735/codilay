"""
AI Context Export — produces a compact, token-efficient document optimized
for feeding into another LLM's context window.

Usage:
    codilay export . --for-ai
    codilay export . --for-ai --max-tokens 4000
    codilay export . --for-ai --format xml

The exported doc strips redundant prose, collapses tables, compresses
section content, and optionally uses XML tags for structured context.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class AIExporter:
    """
    Transforms CodiLay documentation into a compact, token-efficient
    format suitable for injecting into another LLM's context window.
    """

    # Approximate tokens per character (conservative estimate for tiktoken cl100k)
    CHARS_PER_TOKEN = 3.5

    def __init__(
        self,
        section_index: Dict[str, Dict[str, Any]],
        section_contents: Dict[str, str],
        closed_wires: List[Dict[str, Any]],
        open_wires: List[Dict[str, Any]],
        project_name: str = "",
    ):
        self._index = section_index
        self._contents = section_contents
        self._closed = closed_wires
        self._open = open_wires
        self._project = project_name

    def export(
        self,
        fmt: str = "markdown",
        max_tokens: Optional[int] = None,
        include_graph: bool = True,
        include_unresolved: bool = False,
    ) -> str:
        """
        Export documentation in a compact format.

        Args:
            fmt: Output format — "markdown", "xml", or "json"
            max_tokens: Approximate token budget. None = no limit.
            include_graph: Include dependency graph.
            include_unresolved: Include unresolved references.
        """
        if fmt == "xml":
            return self._export_xml(max_tokens, include_graph, include_unresolved)
        elif fmt == "json":
            return self._export_json(max_tokens, include_graph, include_unresolved)
        else:
            return self._export_markdown(max_tokens, include_graph, include_unresolved)

    # ── Markdown format (compact) ─────────────────────────────────

    def _export_markdown(
        self,
        max_tokens: Optional[int],
        include_graph: bool,
        include_unresolved: bool,
    ) -> str:
        lines = [
            f"# {self._project or 'Project'} — AI Context",
            f"<!-- Token-optimized export by CodiLay {datetime.now(timezone.utc).strftime('%Y-%m-%d')} -->",
            "",
        ]

        # Compact sections
        sections = self._get_ordered_sections(include_graph, include_unresolved)
        for sid, title, content in sections:
            compressed = self._compress_content(content)
            if not compressed.strip():
                continue
            file_ref = self._index.get(sid, {}).get("file", "")
            header = f"## {title}"
            if file_ref:
                header += f" (`{file_ref}`)"
            lines.append(header)
            lines.append(compressed)
            lines.append("")

        # Compact dependency summary
        if include_graph and self._closed:
            lines.append("## Dependencies")
            dep_lines = []
            for w in self._closed:
                dep_lines.append(f"- `{w.get('from', '?')}` -> `{w.get('to', '?')}` ({w.get('type', '?')})")
            lines.extend(dep_lines[:50])  # Cap at 50 deps
            if len(self._closed) > 50:
                lines.append(f"- ... +{len(self._closed) - 50} more")
            lines.append("")

        result = "\n".join(lines)

        if max_tokens:
            result = self._truncate_to_tokens(result, max_tokens)

        return result

    # ── XML format (structured for LLMs) ──────────────────────────

    def _export_xml(
        self,
        max_tokens: Optional[int],
        include_graph: bool,
        include_unresolved: bool,
    ) -> str:
        lines = [
            f'<codebase project="{self._escape_xml(self._project or "Project")}"'
            f' exported="{datetime.now(timezone.utc).strftime("%Y-%m-%d")}">',
        ]

        sections = self._get_ordered_sections(include_graph, include_unresolved)
        for sid, title, content in sections:
            compressed = self._compress_content(content)
            if not compressed.strip():
                continue
            file_ref = self._index.get(sid, {}).get("file", "")
            tags_str = ",".join(self._index.get(sid, {}).get("tags", []))

            lines.append(
                f'  <section id="{sid}" title="{self._escape_xml(title)}"'
                f"{f' file={chr(34)}{file_ref}{chr(34)}' if file_ref else ''}"
                f"{f' tags={chr(34)}{tags_str}{chr(34)}' if tags_str else ''}>"
            )
            lines.append(f"    {compressed}")
            lines.append("  </section>")

        if include_graph and self._closed:
            lines.append("  <dependencies>")
            for w in self._closed[:50]:
                lines.append(
                    f'    <dep from="{w.get("from", "?")}" to="{w.get("to", "?")}" type="{w.get("type", "?")}" />'
                )
            if len(self._closed) > 50:
                lines.append(f"    <!-- +{len(self._closed) - 50} more -->")
            lines.append("  </dependencies>")

        lines.append("</codebase>")

        result = "\n".join(lines)

        if max_tokens:
            result = self._truncate_to_tokens(result, max_tokens)

        return result

    # ── JSON format (machine-readable) ────────────────────────────

    def _export_json(
        self,
        max_tokens: Optional[int],
        include_graph: bool,
        include_unresolved: bool,
    ) -> str:
        data: Dict[str, Any] = {
            "project": self._project or "Project",
            "exported": datetime.now(timezone.utc).isoformat(),
            "sections": [],
        }

        sections = self._get_ordered_sections(include_graph, include_unresolved)
        for sid, title, content in sections:
            compressed = self._compress_content(content)
            if not compressed.strip():
                continue
            entry: Dict[str, Any] = {
                "id": sid,
                "title": title,
                "content": compressed,
            }
            file_ref = self._index.get(sid, {}).get("file", "")
            if file_ref:
                entry["file"] = file_ref
            tags = self._index.get(sid, {}).get("tags", [])
            if tags:
                entry["tags"] = tags
            data["sections"].append(entry)

        if include_graph and self._closed:
            data["dependencies"] = [
                {
                    "from": w.get("from", "?"),
                    "to": w.get("to", "?"),
                    "type": w.get("type", "?"),
                }
                for w in self._closed[:50]
            ]

        result = json.dumps(data, indent=1)

        if max_tokens:
            result = self._truncate_to_tokens(result, max_tokens)

        return result

    # ── Helpers ───────────────────────────────────────────────────

    def _get_ordered_sections(
        self,
        include_graph: bool,
        include_unresolved: bool,
    ) -> List[tuple]:
        """Return (section_id, title, content) tuples in order."""
        skip_ids = set()
        if not include_graph:
            skip_ids.add("dependency-graph")
        if not include_unresolved:
            skip_ids.add("unresolved-references")

        sections = []
        for sid, meta in self._index.items():
            if sid in skip_ids:
                continue
            content = self._contents.get(sid, "")
            if not content:
                continue
            sections.append((sid, meta.get("title", sid), content))

        return sections

    def _compress_content(self, content: str) -> str:
        """Aggressively compress documentation content to save tokens."""
        if not content:
            return ""

        # Remove markdown horizontal rules
        content = re.sub(r"\n---\n", "\n", content)

        # Collapse multiple blank lines
        content = re.sub(r"\n{3,}", "\n\n", content)

        # Remove HTML details/summary wrappers but keep content
        content = re.sub(r"<details>\s*<summary>.*?</summary>\s*", "", content, flags=re.DOTALL)
        content = re.sub(r"</details>", "", content)

        # Compact table format: remove alignment rows and extra whitespace
        lines = content.split("\n")
        compacted = []
        for line in lines:
            stripped = line.strip()
            # Skip table alignment rows (|---|---|---|)
            if re.match(r"^\|[-:\s|]+\|$", stripped):
                continue
            # Compact table rows
            if stripped.startswith("|") and stripped.endswith("|"):
                # Remove extra whitespace in table cells
                cells = [c.strip() for c in stripped.split("|")]
                compacted.append("|".join(cells))
            else:
                compacted.append(line)

        content = "\n".join(compacted)

        # Remove leading/trailing whitespace per line
        content = "\n".join(line.rstrip() for line in content.split("\n"))

        # Remove stale-section markers
        content = re.sub(r"> ⚠️ \*This section.*?\*\n\n", "", content)

        return content.strip()

    def _truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Truncate text to approximately max_tokens."""
        max_chars = int(max_tokens * self.CHARS_PER_TOKEN)
        if len(text) <= max_chars:
            return text

        # Find a clean break point
        truncated = text[:max_chars]
        # Try to break at a section boundary
        last_section = truncated.rfind("\n## ")
        if last_section > max_chars * 0.5:
            truncated = truncated[:last_section]

        truncated += "\n\n<!-- Truncated to fit token budget -->"
        return truncated

    def _escape_xml(self, text: str) -> str:
        """Escape special XML characters."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )


# ── Convenience function ──────────────────────────────────────────────────────


def export_for_ai(
    output_dir: str,
    fmt: str = "markdown",
    max_tokens: Optional[int] = None,
    include_graph: bool = True,
) -> str:
    """
    Load state from output_dir and produce a token-efficient export.

    Args:
        output_dir: Path to the codilay output directory.
        fmt: "markdown", "xml", or "json".
        max_tokens: Approximate token budget. None = no limit.
        include_graph: Include dependency information.

    Returns:
        The exported string.
    """
    from codilay.state import AgentState

    state_path = os.path.join(output_dir, ".codilay_state.json")
    if not os.path.exists(state_path):
        raise FileNotFoundError(f"No state found at {state_path}")

    links_path = os.path.join(output_dir, "links.json")
    closed_wires: List[Dict] = []
    open_wires: List[Dict] = []
    project_name = ""

    if os.path.exists(links_path):
        with open(links_path, "r", encoding="utf-8") as f:
            links = json.load(f)
        closed_wires = links.get("closed", [])
        open_wires = links.get("open", [])
        project_name = links.get("project", "")

    state = AgentState.load(state_path)

    exporter = AIExporter(
        section_index=state.section_index,
        section_contents=state.section_contents,
        closed_wires=closed_wires,
        open_wires=open_wires,
        project_name=project_name,
    )

    return exporter.export(
        fmt=fmt,
        max_tokens=max_tokens,
        include_graph=include_graph,
    )
