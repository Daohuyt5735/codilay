"""Main agent processing loop with chunked file support."""

import os
import re
from typing import Dict, Any, Optional, List

from codilay.llm_client import LLMClient
from codilay.wire_manager import WireManager
from codilay.docstore import DocStore
from codilay.state import AgentState
from codilay.chunker import Chunker, ChunkType
from codilay.prompts import (
    system_prompt,
    processing_prompt,
    finalize_prompt,
    skeleton_prompt,
    detail_prompt,
)


class Processor:
    """The core agent loop — handles both normal and chunked files."""

    def __init__(
        self,
        llm: LLMClient,
        config,
        wire_mgr: WireManager,
        docstore: DocStore,
        state: AgentState,
        ui,
    ):
        self.llm = llm
        self.config = config
        self.wire_mgr = wire_mgr
        self.docstore = docstore
        self.state = state
        self.ui = ui
        self._sys_prompt = system_prompt(config)
        self.chunker = Chunker(
            token_counter=llm.count_tokens,
            config=config,
        )

    def process_file(self, file_path: str, content: str) -> Optional[Dict[str, Any]]:
        """
        Process a file — automatically handles chunking for large files.
        """
        # Truncate absurdly large files
        max_chars = self.config.max_file_size
        if len(content) > max_chars:
            content = (
                content[:max_chars]
                + f"\n\n... [TRUNCATED — {len(content)} chars, showing first {max_chars}]"
            )

        # Create chunking plan
        plan = self.chunker.plan(file_path, content)

        if not plan.needs_chunking:
            # Normal single-pass processing
            return self._process_single(file_path, content)
        else:
            # Multi-pass: skeleton + detail chunks
            return self._process_chunked(file_path, plan)

    def _process_single(self, file_path: str, content: str) -> Optional[Dict[str, Any]]:
        """Process a file in a single LLM call (normal case)."""
        wires_to_this = self.wire_mgr.find_wires_to(file_path)
        file_imports = self._extract_imports(content, file_path)
        open_wires = self.wire_mgr.get_open_wires()

        relevant_sections = self.docstore.get_relevant_sections(
            file_path=file_path,
            file_imports=file_imports,
            open_wires=open_wires,
        )
        section_index = self.docstore.get_section_index()

        user_prompt = processing_prompt(
            file_path=file_path,
            file_content=content,
            relevant_sections=relevant_sections,
            open_wires=open_wires,
            section_index=section_index,
        )

        # Check token budget
        total_tokens = self.llm.count_tokens(self._sys_prompt + user_prompt)
        if total_tokens > 100000:
            self.ui.warn(f"  Context too large ({total_tokens} tokens), reducing")
            relevant_sections = self._reduce_sections(relevant_sections)
            user_prompt = processing_prompt(
                file_path=file_path,
                file_content=content,
                relevant_sections=relevant_sections,
                open_wires=open_wires[:20],
                section_index=section_index,
            )

        result = self.llm.call(self._sys_prompt, user_prompt)

        if "error" in result:
            self.ui.warn(f"  LLM error for {file_path}: {result.get('error')}")
            return None

        self._apply_result(file_path, result, wires_to_this)
        self.state.queue = self.wire_mgr.reprioritize_queue(self.state.queue)

        new_sec = result.get("new_section")
        self.ui.file_processed(
            file_path,
            new_section=new_sec.get("title") if isinstance(new_sec, dict) else None,
            wires_closed=len(result.get("wires_closed", [])),
            wires_opened=len(result.get("wires_opened", [])),
        )

        return result

    def _process_chunked(self, file_path: str, plan) -> Optional[Dict[str, Any]]:
        """
        Process a large file in multiple passes:
        1. Skeleton pass — imports + signatures
        2. Detail passes — one per structural chunk
        """
        self.ui.info(
            f"  [cyan]Large file ({plan.total_tokens} tokens) → "
            f"skeleton + {plan.chunk_count} detail passes[/cyan]"
        )

        # ── Pass 1: Skeleton ─────────────────────────────────────
        open_wires = self.wire_mgr.get_open_wires()
        section_index = self.docstore.get_section_index()

        skeleton_user_prompt = skeleton_prompt(
            file_path=file_path,
            skeleton_content=plan.skeleton.content,
            section_index=section_index,
            open_wires=open_wires,
        )

        skeleton_result = self.llm.call(self._sys_prompt, skeleton_user_prompt)

        if "error" in skeleton_result:
            self.ui.warn(f"  Skeleton pass failed for {file_path}: {skeleton_result.get('error')}")
            return None

        # Apply skeleton result
        wires_to_this = self.wire_mgr.find_wires_to(file_path)
        self._apply_result(file_path, skeleton_result, wires_to_this)

        # Get the section ID that was created
        new_sec = skeleton_result.get("new_section", {})
        skeleton_section_id = new_sec.get("id", self._path_to_id(file_path)) if isinstance(new_sec, dict) else self._path_to_id(file_path)
        skeleton_content = new_sec.get("content", "") if isinstance(new_sec, dict) else ""

        # Get interesting symbols for prioritization
        interesting = skeleton_result.get("interesting_symbols", [])

        self.ui.info(
            f"    Skeleton done — section: [cyan]{skeleton_section_id}[/cyan], "
            f"{len(skeleton_result.get('wires_opened', []))} wires opened"
        )

        # ── Pass 2+: Detail passes ──────────────────────────────
        total_closed = len(skeleton_result.get("wires_closed", []))
        total_opened = len(skeleton_result.get("wires_opened", []))

        # Prioritize chunks that contain interesting symbols
        prioritized_chunks = self._prioritize_chunks(plan.chunks, interesting)

        for i, chunk in enumerate(prioritized_chunks):
            open_wires = self.wire_mgr.get_open_wires()

            # Filter to wires relevant to this chunk
            chunk_symbols = set(s.lower() for s in chunk.symbols)
            relevant_wires = [
                w for w in open_wires
                if w.get("from") == file_path or
                w.get("to") == file_path or
                any(s in w.get("to", "").lower() for s in chunk_symbols) or
                any(s in w.get("context", "").lower() for s in chunk_symbols)
            ][:15]

            # Get current skeleton content (may have been patched)
            current_skeleton = self.docstore.get_section_contents().get(
                skeleton_section_id, skeleton_content
            )

            detail_user_prompt = detail_prompt(
                file_path=file_path,
                chunk_content=chunk.content,
                chunk_label=chunk.label,
                chunk_index=i,
                total_chunks=len(prioritized_chunks),
                skeleton_section_id=skeleton_section_id,
                skeleton_content=current_skeleton[:2000],  # Truncate if huge
                open_wires=relevant_wires,
            )

            detail_result = self.llm.call(self._sys_prompt, detail_user_prompt)

            if "error" in detail_result:
                self.ui.warn(f"    Detail pass {i+1} failed: {detail_result.get('error')}")
                continue

            # Apply detail result
            self._apply_detail_result(file_path, detail_result)

            closed = len(detail_result.get("wires_closed", []))
            opened = len(detail_result.get("wires_opened", []))
            total_closed += closed
            total_opened += opened

            self.ui.debug(
                f"    Detail {i+1}/{len(prioritized_chunks)}: {chunk.label} "
                f"(↓{closed} ↑{opened})"
            )

        # Reprioritize queue after all passes
        self.state.queue = self.wire_mgr.reprioritize_queue(self.state.queue)

        self.ui.file_processed(
            file_path,
            new_section=new_sec.get("title") if isinstance(new_sec, dict) else None,
            wires_closed=total_closed,
            wires_opened=total_opened,
        )

        return skeleton_result

    def _prioritize_chunks(self, chunks, interesting_symbols: list) -> list:
        """Reorder chunks so those containing interesting symbols come first."""
        if not interesting_symbols:
            return chunks

        interesting_set = set(s.lower() for s in interesting_symbols)

        def score(chunk):
            chunk_symbols = set(s.lower() for s in chunk.symbols)
            return len(chunk_symbols & interesting_set)

        return sorted(chunks, key=score, reverse=True)

    def _apply_result(
        self, file_path: str, result: Dict, wires_to_this: List[Dict]
    ):
        """Apply LLM processing result to docstore and wire manager."""
        new_section = result.get("new_section")
        if new_section and isinstance(new_section, dict):
            section_id = new_section.get("id", self._path_to_id(file_path))
            self.docstore.add_section(
                section_id=section_id,
                title=new_section.get("title", file_path),
                content=new_section.get("content", ""),
                tags=new_section.get("tags", []),
                file=file_path,
                insert_after=new_section.get("insert_after"),
            )

        patches = result.get("patches", [])
        if isinstance(patches, list):
            for patch in patches:
                if isinstance(patch, dict):
                    self.docstore.patch_section(
                        section_id=patch.get("section_id", ""),
                        update_type=patch.get("update_type", "append"),
                        content=patch.get("content", ""),
                    )

        wires_closed = result.get("wires_closed", [])
        if isinstance(wires_closed, list):
            self.wire_mgr.close_wires_by_ids(wires_closed, resolved_in=file_path)

        for w in wires_to_this:
            if w["id"] not in (wires_closed or []):
                self.wire_mgr.close_wire(
                    w["id"], resolved_in=file_path,
                    summary=f"Resolved by processing {file_path}",
                )

        wires_opened = result.get("wires_opened", [])
        if isinstance(wires_opened, list):
            for wire_data in wires_opened:
                if isinstance(wire_data, dict):
                    to_target = wire_data.get("to", "")
                    if to_target:
                        self.wire_mgr.open_wire(
                            from_file=file_path,
                            to_target=to_target,
                            wire_type=wire_data.get("type", "unknown"),
                            context=wire_data.get("context", ""),
                        )

        park_new = result.get("park_new", [])
        if isinstance(park_new, list):
            for item in park_new:
                if isinstance(item, dict):
                    path = item.get("path", "")
                    reason = item.get("reason", "")
                    if path and path not in self.state.parked:
                        self.state.parked.append(path)
                        self.state.park_reasons[path] = reason

    def _apply_detail_result(self, file_path: str, result: Dict):
        """Apply a detail pass result — patches only, no new sections."""
        patches = result.get("patches", [])
        if isinstance(patches, list):
            for patch in patches:
                if isinstance(patch, dict):
                    self.docstore.patch_section(
                        section_id=patch.get("section_id", ""),
                        update_type=patch.get("update_type", "append"),
                        content=patch.get("content", ""),
                    )

        wires_closed = result.get("wires_closed", [])
        if isinstance(wires_closed, list):
            self.wire_mgr.close_wires_by_ids(wires_closed, resolved_in=file_path)

        wires_opened = result.get("wires_opened", [])
        if isinstance(wires_opened, list):
            for wire_data in wires_opened:
                if isinstance(wire_data, dict):
                    to_target = wire_data.get("to", "")
                    if to_target:
                        self.wire_mgr.open_wire(
                            from_file=file_path,
                            to_target=to_target,
                            wire_type=wire_data.get("type", "unknown"),
                            context=wire_data.get("context", ""),
                        )

    def finalize(self, file_tree: str):
        """Run the finalization pass."""
        section_index = self.docstore.get_section_index()
        open_wires = self.wire_mgr.get_open_wires()

        parked_summaries = {}
        for path in self.state.parked:
            full_path = os.path.join(self.config.target_path, path)
            if os.path.exists(full_path):
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(2000)
                    parked_summaries[path] = content
                except (IOError, OSError):
                    parked_summaries[path] = "(Could not read file)"

        user_prompt = finalize_prompt(file_tree, section_index, open_wires, parked_summaries)

        result = self.llm.call(self._sys_prompt, user_prompt)

        if "error" in result:
            self.ui.warn(f"Finalization error: {result.get('error')}")
            return

        overview = result.get("overview", "")
        if overview:
            self.docstore.patch_section("overview", "replace", overview)

        updates = result.get("section_updates", [])
        if isinstance(updates, list):
            for update in updates:
                if isinstance(update, dict):
                    self.docstore.patch_section(
                        section_id=update.get("section_id", ""),
                        update_type=update.get("update_type", "append"),
                        content=update.get("content", ""),
                    )

        classifications = result.get("wire_classifications", [])
        if isinstance(classifications, list):
            for cls_data in classifications:
                if isinstance(cls_data, dict):
                    wire_id = cls_data.get("wire_id", "")
                    for w in open_wires:
                        if w.get("id") == wire_id:
                            w["classification"] = cls_data.get("classification", "unknown")
                            w["classification_note"] = cls_data.get("note", "")

    # ── Helpers ──────────────────────────────────────────────────

    def _extract_imports(self, content: str, file_path: str) -> List[str]:
        imports = []
        for match in re.finditer(r"(?:from\s+(\S+)\s+import|import\s+(\S+))", content):
            imp = match.group(1) or match.group(2)
            if imp:
                imports.append(imp)
        for match in re.finditer(
            r"(?:import\s+.*?from\s+['\"]([^'\"]+)['\"]"
            r"|require\s*\(\s*['\"]([^'\"]+)['\"]\s*\))", content,
        ):
            imp = match.group(1) or match.group(2)
            if imp:
                imports.append(imp)
        for match in re.finditer(r'"([^"]+)"', content):
            if "/" in match.group(1):
                imports.append(match.group(1))
        for match in re.finditer(r"import\s+([\w.]+)", content):
            imports.append(match.group(1))
        for match in re.finditer(r"use\s+([\w:]+)", content):
            imports.append(match.group(1))
        return imports

    def _reduce_sections(self, sections: Dict) -> Dict:
        reduced = {}
        for sid, sec in sections.items():
            reduced[sid] = dict(sec)
            content = sec.get("content", "")
            if len(content) > 1000:
                reduced[sid]["content"] = content[:1000] + "\n\n… [TRUNCATED]"
        return reduced

    def _path_to_id(self, file_path: str) -> str:
        name = file_path.replace("\\", "/").rsplit(".", 1)[0]
        parts = name.split("/")
        skip = {"src", "lib", "app", "pkg", "internal", "cmd"}
        parts = [p for p in parts if p.lower() not in skip] or parts
        slug = "-".join(parts).lower()
        slug = re.sub(r"[^a-z0-9-]", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug or "unnamed-section"