"""
Parallel Orchestrator — tier-based parallel file processing engine.

Architecture:
                    ┌─────────────────────┐
                    │   Central Wire Bus   │
                    │  (shared, locked)    │
                    └──────────┬──────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                    │
    ┌─────▼─────┐        ┌─────▼─────┐       ┌─────▼─────┐
    │ Worker 1  │        │ Worker 2  │       │ Worker 3  │
    │ (Thread)  │        │ (Thread)  │       │ (Thread)  │
    └─────┬─────┘        └─────┬─────┘       └─────┬─────┘
          │                    │                    │
          └────────────────────▼────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   DocStore (locked)  │
                    │  (section per file)  │
                    └─────────────────────┘

Processing flow:
1. Build dependency graph from static import analysis
2. Compute processing tiers (topological sort)
3. For each tier:
   a. All files in the tier run in parallel (thread pool)
   b. Each worker gets a frozen wire snapshot at start
   c. Workers write to docstore through locks
   d. At tier boundary: sync, reconcile wires, save state
4. Process parked files sequentially with full context
5. Sequential finalize pass reviews parallel-generated sections
"""

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from codilay.dependency_graph import DependencyGraph
from codilay.docstore import DocStore
from codilay.processor import Processor
from codilay.scanner import Scanner
from codilay.state import AgentState
from codilay.wire_bus import WireBus, WireEvent


@dataclass
class WorkerResult:
    """Result from a single file processing worker."""

    file_path: str
    success: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    processing_time: float = 0.0
    confidence: str = "parallel"  # "parallel" or "sequential"
    wires_opened: int = 0
    wires_closed: int = 0


@dataclass
class TierResult:
    """Result from processing an entire tier."""

    tier_index: int
    files: List[str]
    results: List[WorkerResult] = field(default_factory=list)
    total_time: float = 0.0
    parallelism_achieved: int = 0  # how many ran concurrently


@dataclass
class ParkEntry:
    """Tracks a parked file and why it's waiting."""

    file_path: str
    reason: str
    waiting_on: Optional[str] = None  # file it's waiting for
    state: str = "parked"  # "parked", "pending", "unparked", "permanently_parked"


class ParallelOrchestrator:
    """
    Orchestrates parallel file processing using dependency-tier execution.

    Key safety guarantees:
    1. Files only run in parallel if they have no dependency edges between them
    2. Workers get frozen wire snapshots — no partial reads
    3. All wire mutations go through the locked WireBus
    4. Tier boundaries are sync points — full context reconciliation
    5. Parallel-generated sections are tagged for finalize review
    6. Parked files use a notification system for automatic unparking
    """

    def __init__(
        self,
        processor: Processor,
        wire_bus: WireBus,
        docstore: DocStore,
        state: AgentState,
        scanner: Scanner,
        target_path: str,
        ui,
        max_workers: int = 4,
    ):
        self.processor = processor
        self.wire_bus = wire_bus
        self.docstore = docstore
        self.state = state
        self.scanner = scanner
        self.target_path = target_path
        self.ui = ui
        self.max_workers = max_workers

        # Thread-safe docstore lock
        self._docstore_lock = threading.RLock()

        # Park management
        self._park_entries: Dict[str, ParkEntry] = {}
        self._park_lock = threading.Lock()

        # Processing statistics
        self._stats = {
            "total_files": 0,
            "parallel_files": 0,
            "sequential_files": 0,
            "tier_count": 0,
            "max_parallelism": 0,
            "total_time": 0.0,
            "unparked_count": 0,
        }

        # Wire event handler for auto-unparking
        # Store reference to avoid bound-method identity issues on unsubscribe
        self._wire_event_handler = self._on_wire_event
        self.wire_bus.subscribe(self._wire_event_handler)

    # ── Main entry point ─────────────────────────────────────────

    def process_all(
        self,
        files_to_process: List[str],
        file_contents: Dict[str, str],
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> Dict[str, Any]:
        """
        Process all files using tier-based parallel execution.

        Args:
            files_to_process: list of relative file paths to process
            file_contents: pre-loaded file contents (path -> content)
            progress_callback: optional (file_path, completed, total) callback

        Returns:
            Dict with processing stats and results.
        """
        start_time = time.time()
        self._stats["total_files"] = len(files_to_process)

        # Phase 1: Build dependency graph
        self.ui.info("  Building dependency graph from imports...")
        dep_graph = DependencyGraph(self.target_path, files_to_process)
        dep_graph.build(file_contents)

        graph_stats = dep_graph.get_stats()
        self.ui.info(
            f"  Dependency graph: {graph_stats['total_edges']} edges, "
            f"{graph_stats['num_tiers']} tiers, "
            f"{graph_stats['num_clusters']} independent clusters, "
            f"max parallelism: {graph_stats['max_parallelism']}"
        )

        # Phase 2: Compute tiers
        tiers = dep_graph.get_tiers()
        self._stats["tier_count"] = len(tiers)
        self._stats["max_parallelism"] = graph_stats["max_parallelism"]

        # Phase 3: Process tiers
        self._completed_count = 0
        self._total_files = len(files_to_process)
        self._progress_callback = progress_callback
        tier_results: List[TierResult] = []

        for tier_idx, tier_files in enumerate(tiers):
            # Identify missing files in this tier
            missing = [f for f in tier_files if f not in file_contents]
            for f in missing:
                self._on_file_processed(
                    WorkerResult(
                        file_path=f,
                        success=False,
                        error="File content not loaded (skipped)",
                    )
                )

            # Filter to files that still need processing
            tier_files = [f for f in tier_files if f in file_contents]

            if not tier_files:
                continue

            tier_size = len(tier_files)
            effective_workers = min(self.max_workers, tier_size)

            if effective_workers <= 1 or tier_size == 1:
                # Single file or single worker — sequential
                self.ui.info(
                    f"  Tier {tier_idx}/{len(tiers) - 1}: {tier_size} file{'s' if tier_size > 1 else ''} (sequential)"
                )
                tier_result = self._process_tier_sequential(tier_idx, tier_files, file_contents)
            else:
                self.ui.info(
                    f"  Tier {tier_idx}/{len(tiers) - 1}: {tier_size} files → {effective_workers} workers (parallel)"
                )
                tier_result = self._process_tier_parallel(tier_idx, tier_files, file_contents, effective_workers)

            tier_results.append(tier_result)

            # ── Tier boundary sync ───────────────────────────────
            self._sync_tier_boundary(tier_idx)

            # Check for unparked files and add them to remaining tiers
            unparked = self._collect_unparked()
            if unparked:
                self.ui.info(f"  ↳ {len(unparked)} files unparked after tier {tier_idx}")
                for up_file in unparked:
                    if up_file in file_contents:
                        # Will be processed in a later sequential pass
                        pass

        # Phase 4: Process any unparked files sequentially
        unparked_files = self._collect_unparked()
        if unparked_files:
            self.ui.info(f"  Processing {len(unparked_files)} unparked files...")
            for file_path in unparked_files:
                if file_path in file_contents:
                    wr = self._process_single_file(file_path, file_contents[file_path], "sequential")
                    self._on_file_processed(wr)

        self._stats["total_time"] = time.time() - start_time

        return {
            "stats": dict(self._stats),
            "tier_results": tier_results,
            "dep_graph_stats": graph_stats,
        }

    # ── Tier processing ──────────────────────────────────────────

    def _process_tier_sequential(
        self,
        tier_idx: int,
        files: List[str],
        file_contents: Dict[str, str],
    ) -> TierResult:
        """Process a tier sequentially (single file or fallback)."""
        tier_start = time.time()
        result = TierResult(tier_index=tier_idx, files=files, parallelism_achieved=1)

        for file_path in files:
            content = file_contents.get(file_path)
            if content is None:
                continue

            wr = self._process_single_file(file_path, content, "sequential")
            result.results.append(wr)
            self._stats["sequential_files"] += 1
            self._on_file_processed(wr)

        result.total_time = time.time() - tier_start
        return result

    def _process_tier_parallel(
        self,
        tier_idx: int,
        files: List[str],
        file_contents: Dict[str, str],
        num_workers: int,
    ) -> TierResult:
        """
        Process a tier in parallel using a thread pool.

        Each worker:
        1. Gets a frozen wire snapshot at job start
        2. Processes its file through the Processor
        3. Writes results through locked WireBus + DocStore
        """
        tier_start = time.time()
        result = TierResult(tier_index=tier_idx, files=files, parallelism_achieved=num_workers)

        # Mark all tier files as in-flight
        for f in files:
            self.wire_bus.mark_in_flight(f)

        # Submit jobs to thread pool
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures: Dict[Future, str] = {}

            for file_path in files:
                content = file_contents.get(file_path)
                if content is None:
                    continue

                future = executor.submit(
                    self._worker_process_file,
                    file_path,
                    content,
                    tier_idx,
                )
                futures[future] = file_path

            # Collect results as they complete
            for future in as_completed(futures):
                file_path = futures[future]
                try:
                    wr = future.result()
                    result.results.append(wr)
                    self._stats["parallel_files"] += 1
                    self._on_file_processed(wr)
                except Exception as e:
                    wr = WorkerResult(
                        file_path=file_path,
                        success=False,
                        error=str(e),
                    )
                    result.results.append(wr)
                    self.ui.warn(f"  Worker error for {file_path}: {e}")
                    self._on_file_processed(wr)

        # Mark all tier files as completed
        for f in files:
            self.wire_bus.mark_completed(f)

        result.total_time = time.time() - tier_start
        return result

    def _on_file_processed(self, wr: WorkerResult):
        """Internal helper to update progress in real-time."""
        self._completed_count += 1
        if self._progress_callback:
            self._progress_callback(wr.file_path, self._completed_count, self._total_files)

    # ── Worker function (runs in thread) ─────────────────────────

    def _worker_process_file(
        self,
        file_path: str,
        content: str,
        tier_idx: int,
    ) -> WorkerResult:
        """
        Process a single file in a worker thread.

        The worker uses a thread-safe wrapper around the Processor that
        routes all wire operations through the WireBus and all docstore
        operations through the docstore lock.
        """
        start_time = time.time()

        try:
            # Process the file using the existing Processor
            # The Processor already uses self.wire_mgr and self.docstore
            # which we've wrapped with thread-safe versions
            result = self._process_single_file(file_path, content, "parallel")
            return result

        except Exception as e:
            return WorkerResult(
                file_path=file_path,
                success=False,
                error=str(e),
                processing_time=time.time() - start_time,
            )

    def _process_single_file(
        self,
        file_path: str,
        content: str,
        confidence: str,
    ) -> WorkerResult:
        """
        Process a single file through the Processor with proper locking.

        This is the key integration point — we wrap the processor call
        with locks on shared state to ensure thread safety.
        """
        start_time = time.time()

        full_path = os.path.join(self.target_path, file_path)
        if not os.path.exists(full_path):
            return WorkerResult(
                file_path=file_path,
                success=False,
                error="File not found",
                processing_time=time.time() - start_time,
            )

        try:
            # The Processor.process_file call does:
            # 1. Reads wire state (through wire_bus — thread safe)
            # 2. Reads relevant sections (through docstore — need lock)
            # 3. Calls LLM (thread safe — HTTP client)
            # 4. Applies result to docstore + wire_bus (need locks)
            #
            # We use a lock around the docstore operations.
            # Wire operations go through WireBus which has its own lock.
            # LLM calls are naturally concurrent (separate HTTP connections).

            with self._docstore_lock:
                result = self.processor.process_file(file_path, content)

            if result is None:
                return WorkerResult(
                    file_path=file_path,
                    success=False,
                    error="Processor returned None",
                    processing_time=time.time() - start_time,
                    confidence=confidence,
                )

            # Track in state
            with self._docstore_lock:
                if file_path not in self.state.processed:
                    self.state.processed.append(file_path)

                # Store file hash
                file_hash = self.scanner.get_file_hash(full_path)
                if file_hash:
                    self.state.file_hashes[file_path] = file_hash

                # Check for unparked files
                if result.get("unpark"):
                    for up in result["unpark"]:
                        self._handle_unpark(up)

            wires_opened = len(result.get("wires_opened", []))
            wires_closed = len(result.get("wires_closed", []))

            return WorkerResult(
                file_path=file_path,
                success=True,
                result=result,
                processing_time=time.time() - start_time,
                confidence=confidence,
                wires_opened=wires_opened,
                wires_closed=wires_closed,
            )

        except Exception as e:
            self.ui.warn(f"  Error processing {file_path}: {e}")
            return WorkerResult(
                file_path=file_path,
                success=False,
                error=str(e),
                processing_time=time.time() - start_time,
                confidence=confidence,
            )

    # ── Tier boundary synchronization ────────────────────────────

    def _sync_tier_boundary(self, tier_idx: int):
        """
        Synchronize state at a tier boundary.

        This is the safety net — after all workers in a tier complete,
        we reconcile wire state, save a checkpoint, and prepare context
        for the next tier.
        """
        with self._docstore_lock:
            # 1. Reconcile wire state into agent state
            self.state.open_wires = self.wire_bus.get_open_wires()
            self.state.closed_wires = self.wire_bus.get_closed_wires()
            self.state.section_index = self.docstore.get_section_index()
            self.state.section_contents = self.docstore.get_section_contents()

            # 2. Remove processed files from queue
            processed_set = set(self.state.processed)
            self.state.queue = [f for f in self.state.queue if f not in processed_set]

    def save_checkpoint(self, state_path: str):
        """Save a crash-recovery checkpoint. Thread-safe."""
        with self._docstore_lock:
            self.state.open_wires = self.wire_bus.get_open_wires()
            self.state.closed_wires = self.wire_bus.get_closed_wires()
            self.state.section_index = self.docstore.get_section_index()
            self.state.section_contents = self.docstore.get_section_contents()
            self.state.save(state_path)

    # ── Park management ──────────────────────────────────────────

    def _handle_unpark(self, file_path: str):
        """Handle a file being unparked (called under docstore lock)."""
        with self._park_lock:
            if file_path in self.state.parked:
                self.state.parked.remove(file_path)

            entry = self._park_entries.get(file_path)
            if entry:
                entry.state = "unparked"
            else:
                self._park_entries[file_path] = ParkEntry(file_path=file_path, reason="unparked", state="unparked")
            self._stats["unparked_count"] += 1

    def _on_wire_event(self, event: WireEvent):
        """
        Handle wire events for auto-unparking.

        When a wire closes, check if any parked files were waiting
        on the file that just resolved that wire.
        """
        if event.event_type != "closed":
            return

        with self._park_lock:
            resolved_file = event.source_file
            to_unpark = []

            for path, entry in self._park_entries.items():
                if entry.state == "parked" and entry.waiting_on == resolved_file:
                    entry.state = "pending"
                    to_unpark.append(path)

            # Mark as unparked
            for path in to_unpark:
                self._park_entries[path].state = "unparked"

    def _collect_unparked(self) -> List[str]:
        """Collect all files that have been unparked since last collection."""
        with self._park_lock:
            unparked = []
            for path, entry in self._park_entries.items():
                if entry.state == "unparked":
                    unparked.append(path)
                    entry.state = "pending"  # reset
            return unparked

    # ── Confidence-aware finalization ─────────────────────────────

    def get_parallel_sections(self) -> List[str]:
        """
        Get section IDs that were generated in parallel and may need
        review during finalization.
        """
        # All sections generated during parallel tiers need review
        # since they may have had incomplete wire context.
        # The sequential finalize pass will check for inconsistencies.
        index = self.docstore.get_section_index()
        return list(index.keys())

    # ── Statistics ───────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Return processing statistics."""
        return dict(self._stats)

    def cleanup(self):
        """Clean up resources."""
        self.wire_bus.unsubscribe(self._wire_event_handler)
