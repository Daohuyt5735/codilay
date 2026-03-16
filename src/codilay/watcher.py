"""
Watch mode — monitors file changes via watchdog and triggers incremental doc updates.

Usage:
    codilay watch .
    codilay watch . --debounce 3
    codilay watch . --ignore "*.log"

Watches the project directory for file saves/creates/deletes and
automatically re-runs the documentation pipeline on affected files.
"""

import fnmatch
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set

from rich.console import Console
from rich.panel import Panel

# ── Watchdog import (graceful fallback) ───────────────────────────────────────

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

    # Stubs so the module can still be imported for checking HAS_WATCHDOG
    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass

    class Observer:  # type: ignore[no-redef]
        pass


# ── Change accumulator ────────────────────────────────────────────────────────


class ChangeAccumulator:
    """
    Collects file change events and debounces them. After `debounce_seconds`
    of inactivity, fires the callback with the collected set of changed paths.
    """

    def __init__(self, debounce_seconds: float = 2.0, callback: Optional[Callable] = None):
        self._debounce = debounce_seconds
        self._callback = callback
        self._lock = threading.Lock()
        self._changes: Dict[str, str] = {}  # path -> change_type (added/modified/deleted)
        self._timer: Optional[threading.Timer] = None
        self._running = True

    def add_change(self, path: str, change_type: str):
        """Record a file change. Resets the debounce timer."""
        with self._lock:
            self._changes[path] = change_type
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        """Called after the debounce period. Collects changes and invokes callback."""
        with self._lock:
            if not self._changes or not self._running:
                return
            batch = dict(self._changes)
            self._changes.clear()

        if self._callback:
            self._callback(batch)

    def stop(self):
        """Stop the accumulator and cancel any pending timer."""
        self._running = False
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()


# ── File watcher handler ─────────────────────────────────────────────────────


class CodiLayEventHandler(FileSystemEventHandler):
    """
    Watchdog event handler that filters relevant source file changes
    and feeds them to the ChangeAccumulator.
    """

    # Extensions we care about
    WATCH_EXTENSIONS = {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".mjs",
        ".cjs",
        ".java",
        ".kt",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".swift",
        ".lua",
        ".r",
        ".jl",
        ".sh",
        ".sql",
        ".html",
        ".css",
        ".scss",
        ".yaml",
        ".yml",
        ".json",
        ".toml",
        ".xml",
        ".md",
        ".rst",
        ".txt",
        ".vue",
        ".svelte",
        ".dart",
        ".ex",
        ".exs",
        ".zig",
        ".nim",
        ".v",
        ".sol",
    }

    def __init__(
        self,
        project_root: str,
        accumulator: ChangeAccumulator,
        ignore_patterns: Optional[List[str]] = None,
        output_dir: Optional[str] = None,
    ):
        super().__init__()
        self._root = os.path.abspath(project_root)
        self._accumulator = accumulator
        self._ignore_patterns = ignore_patterns or []
        self._output_dir = output_dir or os.path.join(self._root, "codilay")

    def _should_watch(self, path: str) -> bool:
        """Check if this file change is relevant."""
        abs_path = os.path.abspath(path)

        # Skip our own output directory
        if abs_path.startswith(os.path.abspath(self._output_dir)):
            return False

        # Skip hidden directories
        rel = os.path.relpath(abs_path, self._root)
        parts = rel.split(os.sep)
        if any(p.startswith(".") for p in parts):
            return False

        # Skip common non-source directories
        skip_dirs = {"node_modules", "__pycache__", ".git", ".venv", "venv", "dist", "build", ".next"}
        if any(p in skip_dirs for p in parts):
            return False

        # Check extension
        _, ext = os.path.splitext(path)
        if ext.lower() not in self.WATCH_EXTENSIONS:
            return False

        # Check custom ignore patterns
        for pattern in self._ignore_patterns:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(os.path.basename(path), pattern):
                return False

        return True

    def _rel_path(self, path: str) -> str:
        return os.path.relpath(os.path.abspath(path), self._root)

    def on_modified(self, event: "FileSystemEvent"):
        if not event.is_directory and self._should_watch(event.src_path):
            self._accumulator.add_change(self._rel_path(event.src_path), "modified")

    def on_created(self, event: "FileSystemEvent"):
        if not event.is_directory and self._should_watch(event.src_path):
            self._accumulator.add_change(self._rel_path(event.src_path), "added")

    def on_deleted(self, event: "FileSystemEvent"):
        if not event.is_directory and self._should_watch(event.src_path):
            self._accumulator.add_change(self._rel_path(event.src_path), "deleted")

    def on_moved(self, event: "FileSystemEvent"):
        if not event.is_directory:
            if self._should_watch(event.src_path):
                self._accumulator.add_change(self._rel_path(event.src_path), "deleted")
            if self._should_watch(event.dest_path):
                self._accumulator.add_change(self._rel_path(event.dest_path), "added")


# ── Watch runner ──────────────────────────────────────────────────────────────


class Watcher:
    """
    High-level watch mode controller. Sets up watchdog, handles change batches,
    and triggers incremental doc updates.
    """

    def __init__(
        self,
        target_path: str,
        output_dir: Optional[str] = None,
        debounce: float = 2.0,
        ignore_patterns: Optional[List[str]] = None,
        verbose: bool = False,
    ):
        if not HAS_WATCHDOG:
            raise ImportError(
                "Watch mode requires the 'watchdog' package.\nInstall it with: pip install codilay[watch]"
            )

        self.target_path = os.path.abspath(target_path)
        self.output_dir = output_dir or os.path.join(self.target_path, "codilay")
        self.debounce = debounce
        self.ignore_patterns = ignore_patterns or []
        self.verbose = verbose
        self.console = Console()

        self._observer: Optional[Observer] = None
        self._accumulator: Optional[ChangeAccumulator] = None
        self._update_lock = threading.Lock()
        self._update_count = 0
        self._running = False

    def start(self):
        """Start watching for file changes."""
        self._running = True
        self._accumulator = ChangeAccumulator(
            debounce_seconds=self.debounce,
            callback=self._on_changes,
        )

        handler = CodiLayEventHandler(
            project_root=self.target_path,
            accumulator=self._accumulator,
            ignore_patterns=self.ignore_patterns,
            output_dir=self.output_dir,
        )

        self._observer = Observer()
        self._observer.schedule(handler, self.target_path, recursive=True)
        self._observer.start()

        self.console.print(
            Panel(
                f"[bold]CodiLay Watch Mode[/bold]\n\n"
                f"  Project:   [cyan]{os.path.basename(self.target_path)}[/cyan]\n"
                f"  Debounce:  [yellow]{self.debounce}s[/yellow]\n"
                f"  Ignoring:  [dim]{len(self.ignore_patterns)} patterns[/dim]\n\n"
                f"[dim]Watching for file changes... Press Ctrl+C to stop.[/dim]",
                border_style="green",
                title="watch",
            )
        )

        try:
            while self._running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        """Stop the watcher gracefully."""
        self._running = False
        if self._accumulator:
            self._accumulator.stop()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        self.console.print("\n[dim]Watch mode stopped.[/dim]")

    def _on_changes(self, changes: Dict[str, str]):
        """
        Called by the accumulator when a debounced batch of changes is ready.
        Triggers an incremental documentation update.
        """
        if not self._update_lock.acquire(blocking=False):
            self.console.print("[yellow]  Update already in progress, queuing...[/yellow]")
            # Re-queue changes for next cycle
            if self._accumulator:
                for path, change_type in changes.items():
                    self._accumulator.add_change(path, change_type)
            return

        try:
            self._update_count += 1
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")

            added = [p for p, t in changes.items() if t == "added"]
            modified = [p for p, t in changes.items() if t == "modified"]
            deleted = [p for p, t in changes.items() if t == "deleted"]

            change_summary = []
            if added:
                change_summary.append(f"[green]+{len(added)} added[/green]")
            if modified:
                change_summary.append(f"[yellow]~{len(modified)} modified[/yellow]")
            if deleted:
                change_summary.append(f"[red]-{len(deleted)} deleted[/red]")

            self.console.print(f"\n[bold blue][{now}][/bold blue] Changes detected: {', '.join(change_summary)}")

            if self.verbose:
                for path, change_type in changes.items():
                    icon = {"added": "[green]+[/green]", "modified": "[yellow]~[/yellow]", "deleted": "[red]-[/red]"}
                    self.console.print(f"  {icon.get(change_type, '?')} {path}")

            # Run incremental update
            self._run_incremental_update(changes)

        finally:
            self._update_lock.release()

    def _run_incremental_update(self, changes: Dict[str, str]):
        """
        Perform an incremental documentation update for the changed files.
        """
        from codilay.config import CodiLayConfig
        from codilay.docstore import DocStore
        from codilay.llm_client import LLMClient
        from codilay.processor import Processor
        from codilay.settings import Settings
        from codilay.state import AgentState
        from codilay.ui import UI
        from codilay.wire_manager import WireManager

        state_path = os.path.join(self.output_dir, ".codilay_state.json")
        codebase_md_path = os.path.join(self.output_dir, "CODEBASE.md")

        if not os.path.exists(state_path):
            self.console.print("[yellow]  No existing state — run 'codilay .' first for initial docs.[/yellow]")
            return

        try:
            # Load existing state
            state = AgentState.load(state_path)
            settings = Settings.load()
            settings.inject_env_vars()

            cfg = CodiLayConfig.load(self.target_path)
            cfg.llm_provider = settings.default_provider
            cfg.llm_model = settings.default_model
            if settings.custom_base_url:
                cfg.llm_base_url = settings.custom_base_url

            llm = LLMClient(cfg)
            wire_mgr = WireManager()
            wire_mgr.load_state(state.open_wires, state.closed_wires)
            docstore = DocStore()
            docstore.load_from_state(state.section_index, state.section_contents)
            ui = UI(self.console, self.verbose)

            # Handle deletions
            deleted = [p for p, t in changes.items() if t == "deleted"]
            for del_path in deleted:
                docstore.handle_deleted_file(del_path)
                wire_mgr.handle_deleted_file(del_path)
                if del_path in state.processed:
                    state.processed.remove(del_path)

            # Files to process (added + modified)
            to_process = [p for p, t in changes.items() if t in ("added", "modified")]

            if to_process:
                # Invalidate affected sections
                docstore.invalidate_sections_for_files(to_process)

                # Process each file
                processor = Processor(llm, cfg, wire_mgr, docstore, state, ui)

                for file_path in to_process:
                    full_path = os.path.join(self.target_path, file_path)
                    if not os.path.exists(full_path) or not os.path.isfile(full_path):
                        continue

                    try:
                        self.console.print(f"  [dim]Processing {file_path}...[/dim]")
                        processor.process_file(file_path)

                        if file_path not in state.processed:
                            state.processed.append(file_path)
                    except Exception as e:
                        self.console.print(f"  [red]Error processing {file_path}: {e}[/red]")

            # Save updated state and doc
            state.open_wires = wire_mgr.get_open_wires()
            state.closed_wires = wire_mgr.get_closed_wires()
            state.section_index = docstore.get_section_index()
            state.section_contents = docstore.get_section_contents()
            state.last_run = datetime.now(timezone.utc).isoformat()
            state.save(state_path)

            # Re-render CODEBASE.md
            docstore.remove_section("dependency-graph")
            docstore.remove_section("unresolved-references")
            docstore.add_dependency_graph(wire_mgr.get_closed_wires())
            docstore.add_unresolved_references(wire_mgr.get_open_wires())

            final_md = docstore.render_full_document()
            with open(codebase_md_path, "w", encoding="utf-8") as f:
                f.write(final_md)

            self.console.print(
                f"  [green]Updated CODEBASE.md[/green] "
                f"({len(to_process)} files processed, "
                f"{len(deleted)} deletions handled)"
            )

        except Exception as e:
            self.console.print(f"  [red]Update failed: {e}[/red]")
            if self.verbose:
                import traceback

                self.console.print(f"[dim]{traceback.format_exc()}[/dim]")
