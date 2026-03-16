"""Tests for codilay.watcher — file watch mode components."""

import os
import tempfile
import threading
import time

from codilay.watcher import ChangeAccumulator, CodiLayEventHandler, HAS_WATCHDOG


# ── ChangeAccumulator ────────────────────────────────────────────────────────


def test_accumulator_collects_changes():
    collected = {}
    event = threading.Event()

    def callback(changes):
        collected.update(changes)
        event.set()

    acc = ChangeAccumulator(debounce_seconds=0.1, callback=callback)
    acc.add_change("src/main.py", "modified")
    acc.add_change("src/utils.py", "added")

    event.wait(timeout=2.0)
    acc.stop()

    assert "src/main.py" in collected
    assert collected["src/main.py"] == "modified"
    assert "src/utils.py" in collected
    assert collected["src/utils.py"] == "added"


def test_accumulator_debounces():
    call_count = [0]
    event = threading.Event()

    def callback(changes):
        call_count[0] += 1
        event.set()

    acc = ChangeAccumulator(debounce_seconds=0.2, callback=callback)

    # Rapid changes should be debounced into one callback
    for i in range(10):
        acc.add_change(f"file{i}.py", "modified")
        time.sleep(0.02)

    event.wait(timeout=2.0)
    time.sleep(0.3)  # Wait for any extra firings
    acc.stop()

    assert call_count[0] == 1


def test_accumulator_latest_change_type_wins():
    collected = {}
    event = threading.Event()

    def callback(changes):
        collected.update(changes)
        event.set()

    acc = ChangeAccumulator(debounce_seconds=0.1, callback=callback)
    acc.add_change("src/main.py", "modified")
    acc.add_change("src/main.py", "deleted")  # Should override

    event.wait(timeout=2.0)
    acc.stop()

    assert collected["src/main.py"] == "deleted"


def test_accumulator_stop_prevents_callback():
    called = [False]

    def callback(changes):
        called[0] = True

    acc = ChangeAccumulator(debounce_seconds=0.5, callback=callback)
    acc.add_change("file.py", "modified")
    acc.stop()

    time.sleep(0.7)
    assert called[0] is False


def test_accumulator_no_callback():
    acc = ChangeAccumulator(debounce_seconds=0.1, callback=None)
    acc.add_change("file.py", "modified")
    time.sleep(0.3)
    acc.stop()
    # Should not crash


# ── CodiLayEventHandler._should_watch ────────────────────────────────────────


def test_should_watch_source_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        acc = ChangeAccumulator(debounce_seconds=1.0)
        handler = CodiLayEventHandler(project_root=tmpdir, accumulator=acc)

        assert handler._should_watch(os.path.join(tmpdir, "src", "main.py")) is True
        assert handler._should_watch(os.path.join(tmpdir, "src", "app.ts")) is True
        assert handler._should_watch(os.path.join(tmpdir, "lib", "utils.go")) is True
        acc.stop()


def test_should_watch_rejects_non_source():
    with tempfile.TemporaryDirectory() as tmpdir:
        acc = ChangeAccumulator(debounce_seconds=1.0)
        handler = CodiLayEventHandler(project_root=tmpdir, accumulator=acc)

        assert handler._should_watch(os.path.join(tmpdir, "image.png")) is False
        assert handler._should_watch(os.path.join(tmpdir, "archive.zip")) is False
        assert handler._should_watch(os.path.join(tmpdir, "binary.exe")) is False
        acc.stop()


def test_should_watch_rejects_output_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = os.path.join(tmpdir, "codilay")
        os.makedirs(output_dir)
        acc = ChangeAccumulator(debounce_seconds=1.0)
        handler = CodiLayEventHandler(project_root=tmpdir, accumulator=acc, output_dir=output_dir)

        assert handler._should_watch(os.path.join(output_dir, "CODEBASE.md")) is False
        acc.stop()


def test_should_watch_rejects_hidden_dirs():
    with tempfile.TemporaryDirectory() as tmpdir:
        acc = ChangeAccumulator(debounce_seconds=1.0)
        handler = CodiLayEventHandler(project_root=tmpdir, accumulator=acc)

        assert handler._should_watch(os.path.join(tmpdir, ".git", "config")) is False
        assert handler._should_watch(os.path.join(tmpdir, ".vscode", "settings.json")) is False
        acc.stop()


def test_should_watch_rejects_skip_dirs():
    with tempfile.TemporaryDirectory() as tmpdir:
        acc = ChangeAccumulator(debounce_seconds=1.0)
        handler = CodiLayEventHandler(project_root=tmpdir, accumulator=acc)

        assert handler._should_watch(os.path.join(tmpdir, "node_modules", "pkg", "index.js")) is False
        assert handler._should_watch(os.path.join(tmpdir, "__pycache__", "mod.cpython.pyc")) is False
        assert handler._should_watch(os.path.join(tmpdir, "dist", "bundle.js")) is False
        acc.stop()


def test_should_watch_custom_ignore():
    with tempfile.TemporaryDirectory() as tmpdir:
        acc = ChangeAccumulator(debounce_seconds=1.0)
        handler = CodiLayEventHandler(
            project_root=tmpdir,
            accumulator=acc,
            ignore_patterns=["*.log", "generated/*"],
        )

        assert handler._should_watch(os.path.join(tmpdir, "app.log")) is False
        assert handler._should_watch(os.path.join(tmpdir, "generated", "types.ts")) is False
        assert handler._should_watch(os.path.join(tmpdir, "src", "main.py")) is True
        acc.stop()


# ── Watchdog import check ────────────────────────────────────────────────────


def test_has_watchdog_flag():
    # HAS_WATCHDOG should be a boolean
    assert isinstance(HAS_WATCHDOG, bool)


# ── WATCH_EXTENSIONS ─────────────────────────────────────────────────────────


def test_watch_extensions_coverage():
    exts = CodiLayEventHandler.WATCH_EXTENSIONS
    # Core languages
    assert ".py" in exts
    assert ".js" in exts
    assert ".ts" in exts
    assert ".go" in exts
    assert ".rs" in exts
    assert ".java" in exts
    # Config files
    assert ".json" in exts
    assert ".yaml" in exts
    assert ".toml" in exts
    # Frontend
    assert ".vue" in exts
    assert ".svelte" in exts
    assert ".jsx" in exts
    assert ".tsx" in exts
