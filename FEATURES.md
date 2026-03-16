# CodiLay — New Features Implementation Guide

This document describes the architecture, design decisions, and implementation details for the 10 new features added to CodiLay. This first half covers features 1–5.

---

## Table of Contents

1. [Watch Mode](#1-watch-mode)
2. [IDE Integration (VSCode Extension)](#2-ide-integration-vscode-extension)
3. [AI Context Export](#3-ai-context-export)
4. [Doc Diff View](#4-doc-diff-view)
5. [Triage Tuning](#5-triage-tuning)
6. Graph Filters *(documented separately)*
7. Team Memory *(documented separately)*
8. Conversation Search *(documented separately)*
9. Scheduled Re-runs *(documented separately)*
10. Multi-user Web UI *(documented separately)*

---

## 1. Watch Mode

**Module:** `src/codilay/watcher.py` (436 lines)
**CLI command:** `codilay watch <path>`
**Optional dependency:** `watchdog>=3.0.0` (install via `pip install codilay[watch]`)

### Purpose

Run CodiLay in the background so that documentation is incrementally updated every time a source file is saved, without requiring a full manual re-run.

### Architecture

The watch system has three layers:

```
Filesystem Events (watchdog Observer)
        │
        ▼
CodiLayEventHandler (filters irrelevant events)
        │
        ▼
ChangeAccumulator (debounces into batches)
        │
        ▼
Watcher._run_incremental_update (processes only changed files)
```

### Key Classes

**`ChangeAccumulator`** — Collects file-change events and debounces them. After a configurable quiet period (default 2 seconds) with no new events, it fires a callback with the accumulated batch. Thread-safe via `threading.Lock` and `threading.Timer`.

- `add_change(path, change_type)` — Records a change (`added`, `modified`, `deleted`) and resets the debounce timer.
- `stop()` — Cancels any pending timer and prevents future callbacks.

**`CodiLayEventHandler`** (extends `watchdog.events.FileSystemEventHandler`) — Filters filesystem events down to relevant source files. The filtering logic in `_should_watch(path)` checks:

- File extension is in `WATCH_EXTENSIONS` (45+ extensions covering Python, JS/TS, Rust, Go, Java, C/C++, Ruby, etc.)
- Path does not contain hidden directories (`.git`, `.vscode`, etc.)
- Path does not contain skip directories (`node_modules`, `__pycache__`, `venv`, `dist`, etc.)
- Path is not inside the CodiLay output directory itself
- Path does not match any custom ignore patterns from `CodiLayConfig.ignore_patterns`

**`Watcher`** — The top-level controller. Orchestrates the watchdog `Observer`, handles batched changes, and triggers incremental documentation updates.

- `start()` — Blocking call that starts the observer and loops until Ctrl+C.
- `stop()` — Graceful shutdown of the observer and accumulator.
- `_run_incremental_update(changes)` — The core integration point.

### Incremental Update Flow

When a batch of changes fires, `_run_incremental_update` does the following:

1. Lazy-imports `CodiLayConfig`, `DocStore`, `LLMClient`, `Processor`, `Settings`, `AgentState`, `UI`, and `WireManager` to avoid circular dependencies at module load time.
2. Loads existing agent state from `.codilay_state.json`.
3. For **deleted** files: removes sections from the DocStore, removes wires from WireManager, and removes the file from agent state's file list.
4. For **added/modified** files: feeds them through the `Processor` for re-analysis with the LLM.
5. Invalidates any doc sections that reference changed files.
6. Re-renders `CODEBASE.md` with updated content.
7. Saves updated state back to `.codilay_state.json`.

An `_update_lock` (threading Lock) prevents overlapping updates. If a new batch arrives while an update is in progress, those changes are re-queued into the accumulator to be processed after the current update finishes.

### Graceful Degradation

The module checks for watchdog availability at import time:

```python
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False
```

When watchdog is not installed, stub classes are defined so the module can still be imported. The CLI command checks `HAS_WATCHDOG` and prints an install instruction if it's missing.

### Design Decisions

- **Debounce over per-event processing** — Editors often trigger multiple filesystem events for a single save (write temp file, rename, delete old). Debouncing collapses these into one batch.
- **Lazy imports** — The watcher module imports heavy dependencies (LLM client, processor) only when an update actually triggers, keeping startup fast.
- **Lock-based concurrency** — Simpler than a queue-based approach and sufficient for the single-update-at-a-time guarantee we need.

---

## 2. IDE Integration (VSCode Extension)

**Directory:** `vscode-extension/`
**Files:** `package.json`, `tsconfig.json`, `src/extension.ts` (410 lines)

### Purpose

Surface CodiLay documentation inline alongside the file being edited in VSCode, with commands for chat, search, and graph visualization — all powered by the `codilay serve` HTTP backend.

### Architecture

The extension is a thin API client. All data comes from the CodiLay HTTP API — the extension does no file I/O or documentation generation itself.

```
VSCode Extension (TypeScript)
        │
        ▼ HTTP requests
CodiLay Server (codilay serve, FastAPI)
        │
        ▼
DocStore, ChatStore, WireManager, etc.
```

### Key Components

**`SectionTreeProvider`** (implements `vscode.TreeDataProvider`) — Fetches documentation sections from `GET /api/sections` and presents them as a sidebar tree view. Each node is a `SectionTreeItem` showing the section title and associated file path.

- `load()` — Fetches sections from the server.
- `refresh()` — Fires the tree data change event to trigger a re-render.
- `getSectionForFile(filePath)` — Maps an open editor's file path to its corresponding documentation section.

**`showDocPanel(context, content, title)`** — Creates or reveals a side-panel webview to display documentation. Uses a simple markdown-to-HTML converter (`getDocHtml`) that handles headings, bold text, inline code, code blocks, and list items.

**`updateInlineHints(editor, sectionProvider)`** — When `codilay.inlineHints` is enabled (default), decorates line 0 of the active editor with the CodiLay section title as a dimmed, italic text decoration. This gives at-a-glance context without being intrusive.

### Registered Commands

| Command | Action |
|---|---|
| `codilay.showDocPanel` | Fetches full doc from `GET /api/document`, displays in webview |
| `codilay.showFileDoc` | Shows documentation for the currently active file |
| `codilay.openSection` | Opens a specific section (triggered by tree item click) |
| `codilay.askQuestion` | Input box → `POST /api/chat` → shows answer with sources and escalation indicator |
| `codilay.searchConversations` | Input box → `GET /api/search` → QuickPick list of results |
| `codilay.showGraph` | `POST /api/graph/filter` → shows node/edge summary |
| `codilay.refresh` | Reloads sections from the server |

### Configuration

Two settings exposed via `package.json`:

- `codilay.serverUrl` (default `http://127.0.0.1:8484`) — URL of the running CodiLay server.
- `codilay.inlineHints` (default `true`) — Toggle inline documentation decorations.

### Design Decisions

- **Client-server, not embedded** — The extension delegates all computation to the already-running `codilay serve` process. This avoids duplicating logic in TypeScript and ensures the extension always shows the same data as the web UI.
- **Webview for doc display** — VSCode's webview API allows rich HTML rendering, which is necessary for formatted documentation with code blocks, tables, and headings.
- **Singleton panel pattern** — The doc panel is reused (revealed) if already open, rather than creating a new panel for each request.

---

## 3. AI Context Export

**Module:** `src/codilay/exporter.py` (356 lines)
**CLI command:** `codilay export <path> [--format markdown|xml|json] [--max-tokens N] [--no-graph] [--include-unresolved]`
**Server endpoint:** `GET /api/export?format=...&max_tokens=...`

### Purpose

Produce a compact, token-efficient representation of the codebase documentation optimized for feeding into another LLM's context window (e.g., when asking Claude or GPT about your codebase).

### Architecture

```
DocStore (section index + contents)  +  links.json (wires)
        │
        ▼
    AIExporter
        │
        ├─ _export_markdown()  → compact markdown
        ├─ _export_xml()       → structured XML
        └─ _export_json()      → structured JSON
        │
        ▼
    _compress_content()  →  _truncate_to_tokens()
        │
        ▼
    Final output string
```

### Key Class

**`AIExporter`** — Constructed with the project name, section index, section contents, and wire data. Produces output in one of three formats.

### Output Formats

**Markdown** — Compact, human-readable. Sections are rendered as `## Title` headings with compressed content. Dependencies are listed as a bullet list (capped at 50 entries to avoid blowing token budget).

**XML** — Structured with semantic tags. Each section becomes:
```xml
<section id="..." title="..." file="..." tags="...">
  compressed content
</section>
```
Dependencies use `<dep from="..." to="..." type="..." />` tags. XML entities (`&`, `<`, `>`) are properly escaped.

**JSON** — Machine-readable. Produces a JSON object with `project`, `exported` timestamp, `sections` array (each with `id`, `title`, `file`, `tags`, `content`), and `dependencies` array.

### Content Compression

`_compress_content(content)` aggressively strips tokens that don't carry semantic value:

1. Removes horizontal rules (`---`, `***`, `===`)
2. Collapses multiple blank lines into one
3. Strips `<details>` / `<summary>` HTML wrappers (keeps inner content)
4. Removes table column-alignment rows (`| --- | :---: |`)
5. Compacts table cell padding
6. Removes stale-section markers like `<!-- stale -->`

### Token Budget Truncation

`_truncate_to_tokens(text, max_tokens)` enforces an approximate token limit:

- Uses a 3.5 characters-per-token estimate (reasonable for English prose + code mixed content)
- Attempts to truncate at the nearest section boundary (`## ` heading) to avoid cutting mid-section
- Falls back to a hard character-limit cut if no clean boundary is found
- Appends a `[truncated]` marker when truncation occurs

### Convenience Function

`export_for_ai(output_dir, fmt, max_tokens, include_graph)` is the high-level entry point used by the CLI. It:

1. Loads `AgentState` from `.codilay_state.json`
2. Loads wire data from `links.json`
3. Constructs an `AIExporter` with the loaded data
4. Returns the exported string

### Design Decisions

- **Three formats** — Markdown is best for human review, XML works well with LLMs that understand structured markup (Claude), JSON is ideal for programmatic consumption.
- **Approximate token counting** — We use a 3.5 chars/token heuristic instead of importing tiktoken. This keeps the exporter dependency-free and fast, at the cost of ~10% accuracy. Good enough for budget enforcement.
- **50-dependency cap** — Large codebases can have thousands of wires. Dumping all of them wastes tokens. The cap of 50 prioritizes closed (resolved) wires, which are more informative.

---

## 4. Doc Diff View

**Module:** `src/codilay/doc_differ.py` (400 lines)
**CLI command:** `codilay diff-doc <path> [--format text|json] [--verbose]`
**Server endpoints:** `GET /api/doc-diff`, `GET /api/doc-diff/snapshots`

### Purpose

On re-runs, show a changelog of what shifted in the documentation between versions — not just which files changed (that's `git diff`), but which *documentation sections* were added, removed, or modified, and how the dependency graph evolved.

### Architecture

```
codilay run  →  _finalize_and_write()
                    │
                    ▼
            DocVersionStore.save_snapshot()  →  codilay/history/snapshot_*.json
                    │
                    ▼  (next run, or explicit diff-doc command)
            DocVersionStore.diff_latest()
                    │
                    ▼
            DocDiffer.diff()  →  DocDiffResult
```

### Key Data Structures

**`SectionChange`** (dataclass) — Represents a single section change:

| Field | Type | Description |
|---|---|---|
| `section_id` | `str` | Section identifier |
| `title` | `str` | Human-readable section title |
| `change_type` | `str` | One of `added`, `removed`, `modified`, `renamed` |
| `old_content` | `str?` | Previous content (for modified/removed) |
| `new_content` | `str?` | New content (for modified/added) |
| `diff_lines` | `list[str]` | Unified diff output lines |
| `summary` | `str` | Auto-generated human-readable summary |

**`DocDiffResult`** (dataclass) — Complete diff result:

| Field | Type | Description |
|---|---|---|
| `added_sections` | `list[SectionChange]` | Newly added sections |
| `removed_sections` | `list[SectionChange]` | Sections that were removed |
| `modified_sections` | `list[SectionChange]` | Sections with changed content |
| `new_closed_wires` | `int` | Dependencies newly resolved |
| `lost_closed_wires` | `int` | Previously resolved dependencies now lost |
| `new_open_wires` | `int` | New unresolved dependencies |
| `resolved_open_wires` | `int` | Previously open wires now resolved |
| `sections_delta` | `int` | Net change in section count |
| `old_run_time` | `str?` | Timestamp of older snapshot |
| `new_run_time` | `str?` | Timestamp of newer snapshot |

Computed properties: `has_changes`, `total_section_changes`. Serializer: `to_dict()`.

### Diffing Algorithm

`DocDiffer.diff()` works as follows:

1. Collects section IDs from old and new states, skipping meta sections (`dependency-graph`, `unresolved-dependencies`, `project-overview`).
2. **Added sections** = IDs in new but not in old.
3. **Removed sections** = IDs in old but not in new.
4. **Modified sections** = IDs in both, where content differs. Uses `difflib.unified_diff` to generate patch-style output and `difflib.SequenceMatcher` to build a human-readable summary (detecting new code references — backtick-wrapped identifiers — added in modified sections).
5. **Wire changes** = Set-based comparison using composite keys (`"{from}|{to}|{type}"`). Computes newly closed, lost closed, newly open, and resolved open counts.
6. **Sections delta** = `len(new_sections) - len(old_sections)`.

### Snapshot Storage

**`DocVersionStore`** manages snapshots in `codilay/history/`:

- `save_snapshot(section_index, section_contents, closed_wires, open_wires, run_id, commit)` — Saves a timestamped JSON file (`snapshot_YYYYMMDD_HHMMSS_ffffff.json`). Microsecond precision in the filename prevents collisions on rapid successive runs.
- `list_snapshots()` — Returns metadata about all snapshots, sorted newest-first.
- `get_latest_snapshot()` / `get_previous_snapshot()` — Load the most recent or second-most-recent snapshot.
- `diff_latest()` — Loads the two most recent snapshots and runs `DocDiffer.diff()` on them.
- `_cleanup(keep=20)` — Auto-prunes to keep only the last 20 snapshots.

### Integration with `codilay run`

The `_finalize_and_write()` function in `cli.py` calls `DocVersionStore.save_snapshot()` after every successful run. This means snapshots accumulate automatically — users don't need to opt in. The `diff-doc` command then compares the two most recent snapshots.

### Design Decisions

- **Section-level, not line-level** — Git already provides line-level diffs. CodiLay's value-add is understanding *semantic* documentation changes: "section X was added", "the overview was rewritten", "3 new dependencies were resolved".
- **Auto-snapshot on every run** — No opt-in required. Snapshots are cheap (JSON, typically 10–100KB) and auto-pruned.
- **Microsecond timestamps** — Prevents filename collisions when two runs happen within the same second (common in tests and CI).
- **Meta section skipping** — The dependency graph and unresolved dependencies sections are computed outputs, not authored content. Diffing them would just show derivative noise.

---

## 5. Triage Tuning

**Module:** `src/codilay/triage_feedback.py` (238 lines)
**CLI commands:** `codilay triage-feedback add|list|remove|hint|clear`
**Server endpoints:** `GET /api/triage-feedback`, `POST /api/triage-feedback`, `DELETE /api/triage-feedback`

### Purpose

Let users flag incorrect triage decisions (file categorizations) so that future runs on the same project produce better results. Corrections are applied both as direct overrides and as LLM prompt context.

### Architecture

```
User feedback (CLI / API)
        │
        ▼
TriageFeedbackStore  →  codilay/triage_feedback.json
        │
        ├─ apply_to_triage(result)     →  Direct overrides on TriageResult
        └─ build_prompt_context()      →  Text block for LLM triage prompt
```

### Key Data Structures

**`TriageFeedbackEntry`** (dataclass):

| Field | Type | Description |
|---|---|---|
| `file_path` | `str` | Exact path or glob pattern (e.g., `*.proto`, `tests/**`) |
| `original_category` | `str` | What triage assigned (`core`, `skim`, `skip`) |
| `corrected_category` | `str` | What the user wants it to be |
| `reason` | `str` | Why the correction is needed |
| `created_at` | `str` | ISO timestamp |
| `is_pattern` | `bool` | Whether `file_path` is a glob pattern |

Serialization: `to_dict()` / `from_dict()` class method.

### Feedback Store

**`TriageFeedbackStore`** manages the persistent feedback file:

- `add_feedback(file_path, original, corrected, reason, is_pattern)` — Records a correction, replacing any existing entry for the same path.
- `remove_feedback(file_path)` — Removes a specific entry.
- `clear()` — Removes all entries.
- `list_entries()` — Returns all stored entries.
- `set_project_hint(project_type, hint)` — Stores a natural-language hint about triage for a project type (e.g., `"flutter"` → `"Always skip ios/ and android/ but keep lib/generated/"`).

### Two Application Mechanisms

**1. Direct overrides via `apply_to_triage(triage_result)`:**

Called during `codilay run`, after the LLM triage phase completes but before user review. For each feedback entry:

- **Exact path entries**: If the file exists in the triage result under a different category than `corrected_category`, it's moved to the correct list.
- **Pattern entries** (`is_pattern=True`): All files in the triage result are checked against the glob pattern using `fnmatch`. Matching files in the wrong category are moved.

The method returns the count of overrides applied, which is displayed to the user.

**2. LLM prompt injection via `build_prompt_context()`:**

Produces a text block that is injected into the triage LLM prompt for future runs. The format is:

```
## User Triage Corrections (apply these overrides):

- `src/generated/api.py`: Should be "skip" not "core". Reason: Auto-generated file, changes every build
- `*.test.js` (pattern): Should be "skim" not "skip". Reason: Tests contain important behavior specs

## Project Type Hints:

- flutter: Always skip ios/ and android/ platform directories but keep lib/generated/
```

This way, even for *new* files that match described patterns, the LLM is steered toward the user's preferences.

### Storage Format

`codilay/triage_feedback.json`:

```json
{
  "entries": [
    {
      "file_path": "src/generated/api.py",
      "original_category": "core",
      "corrected_category": "skip",
      "reason": "Auto-generated file",
      "created_at": "2025-01-15T10:30:00+00:00",
      "is_pattern": false
    }
  ],
  "project_hints": {
    "flutter": "Always skip ios/ and android/"
  },
  "updated_at": "2025-01-15T10:30:00+00:00"
}
```

### Integration with `codilay run`

In `cli.py`, the triage phase hook:

1. Runs the normal LLM-based triage to produce a `TriageResult`.
2. Loads `TriageFeedbackStore` for the project.
3. Calls `apply_to_triage(triage_result)` to apply stored corrections.
4. Reports override count to the user (e.g., "Applied 3 triage overrides from feedback").
5. Continues with the corrected triage result.

### Design Decisions

- **Dual mechanism (overrides + prompt context)** — Direct overrides guarantee correctness for known files. Prompt context teaches the LLM to make better decisions for unknown files that match described patterns.
- **Glob patterns** — Users shouldn't need to add feedback for every individual test file. A single `tests/**` → `skim` entry covers the whole directory.
- **Project-type hints** — A higher-level abstraction for teams that work on specific frameworks. Instead of many individual entries, a single hint like "this is a Django project, always skip migrations/" captures domain knowledge.
- **Replace-on-duplicate** — Adding feedback for the same path replaces the old entry, preventing conflicting corrections from accumulating.

---

*Features 6–10 (Graph Filters, Team Memory, Conversation Search, Scheduled Re-runs, Multi-user Web UI) will be documented in a subsequent update.*
