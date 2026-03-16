# CodiLay — New Features Implementation Guide (Part 3)

This document concludes the feature implementation documentation, covering features 9–10. See FEATURES.md for features 1–5 and FEATURES2.md for features 6–8.

---

## Table of Contents

9. [Conversation Search](#9-conversation-search) *(covered in Part 2)*
10. [Scheduled Re-runs](#9-scheduled-re-runs)
11. [Multi-user Web UI](#10-multi-user-web-ui)

---

## 9. Scheduled Re-runs

**Module:** `src/codilay/scheduler.py` (361 lines)
**CLI commands:** `codilay schedule set <path> [--cron "..."] [--on-commit] [--branch main]`, `codilay schedule disable <path>`, `codilay schedule status <path>`, `codilay schedule start <path>`, `codilay schedule stop <path>`

### Purpose

Automatically trigger documentation re-generation on a cron schedule or when new commits land on a branch. This keeps docs fresh without requiring anyone to manually run `codilay` after every merge.

### Architecture

```
User configures schedule (CLI)
        │
        ▼
ScheduleConfig  →  codilay/schedule.json
        │
        ▼  codilay schedule start
    Scheduler (foreground polling loop)
        │
        ├─ CronExpression.matches(now)  →  time-based trigger
        └─ _check_new_commits(branch)   →  git-based trigger
        │
        ▼
    _trigger_update()  →  subprocess: python -m codilay <path>
        │
        ▼
    PID file  →  codilay/.scheduler.pid
```

### Key Classes

#### `CronExpression` — Minimal Cron Parser

A self-contained 5-field cron parser with no external dependencies. Supports the full standard syntax:

| Syntax | Example | Meaning |
|---|---|---|
| `*` | `* * * * *` | Every minute |
| `N` | `30 * * * *` | At minute 30 |
| `N-M` | `0 9-17 * * *` | Hours 9 through 17 |
| `*/N` | `*/15 * * * *` | Every 15 minutes |
| `N,M,O` | `0 9,12,18 * * *` | At hours 9, 12, and 18 |

Fields: minute (0–59), hour (0–23), day-of-month (1–31), month (1–12), day-of-week (0–6, Mon=0).

**Implementation detail:** Each field is parsed into a `set` of valid integer values via `_parse_field()`. The `matches(dt)` method simply checks membership in each set. This is O(1) per match — no regex evaluation at match time.

**Validation:** The constructor raises `ValueError` if the expression doesn't have exactly 5 fields. Invalid integer values in fields propagate as `ValueError` from `int()`.

#### `ScheduleConfig` — Persistent Configuration

Manages the schedule configuration stored in `codilay/schedule.json`.

**Methods:**

| Method | Description |
|---|---|
| `load()` | Returns the current config dict, or sensible defaults if the file doesn't exist |
| `save(config)` | Persists config to disk, adding an `updated_at` timestamp |
| `set_cron(cron_expr, branch)` | Validates the cron expression (via constructing a `CronExpression`), enables the schedule, stores the expression and branch |
| `set_on_commit(branch)` | Enables commit-triggered updates for the specified branch |
| `disable()` | Sets `enabled: False` |
| `record_run(commit)` | Increments `run_count`, updates `last_run` timestamp and optionally `last_commit_checked` |

**Config schema:**

```json
{
  "enabled": true,
  "cron": "0 */6 * * *",
  "on_commit": true,
  "branch": "main",
  "last_run": "2025-01-15T10:30:00+00:00",
  "last_commit_checked": "abc123def456...",
  "run_count": 42,
  "created_at": "2025-01-01T00:00:00+00:00",
  "updated_at": "2025-01-15T10:30:00+00:00"
}
```

`created_at` is preserved across updates (set only on first `set_cron` or `set_on_commit` call). This lets users see when the schedule was originally configured.

#### `Scheduler` — Background Polling Daemon

The main daemon that runs in the foreground and checks for trigger conditions.

**Constructor:** Takes `target_path`, optional `output_dir`, and `verbose` flag. Sets a 60-second poll interval.

**`start()` — The Main Loop:**

1. Loads config. If not enabled, prints a warning and returns.
2. Parses the cron expression (if configured).
3. Displays a Rich panel showing the project name, schedule description, and poll interval.
4. Enters a `while self._running` loop:
   - **Cron check:** If a `CronExpression` is configured and `matches(now)` returns True, and the current minute differs from the last triggered minute (same-minute deduplication), triggers an update.
   - **Commit check:** If `on_commit` is enabled, calls `_check_new_commits(branch)`. If a new commit is detected, triggers an update and records the commit hash.
   - Sleeps for `_poll_interval` seconds (60s).
5. Catches `KeyboardInterrupt` for graceful shutdown.

**`_check_new_commits(branch)` — Git Polling:**

1. Runs `git fetch origin <branch>` (30s timeout, non-blocking — output captured and discarded).
2. Runs `git rev-parse origin/<branch>` to get the current HEAD hash. Falls back to `git rev-parse <branch>` (without `origin/` prefix) for local-only repos.
3. Compares against `last_commit_checked` from config.
4. If they differ and there was a previous reference, returns the new commit hash.
5. On first run (no previous reference), stores the current HEAD but does *not* trigger — this prevents an unnecessary update on scheduler start when nothing actually changed.
6. All git failures (timeout, missing branch, not a git repo) return `None` silently — the scheduler continues polling.

**`_trigger_update()` — Subprocess Invocation:**

```python
subprocess.run(
    [sys.executable, "-m", "codilay", self.target_path],
    cwd=self.target_path,
    capture_output=True,
    text=True,
    timeout=600,  # 10 minute timeout
)
```

Key design choices:
- Uses `sys.executable` to ensure the same Python interpreter (and virtualenv) runs the update.
- Uses `-m codilay` module invocation rather than the `codilay` entry point script — more reliable across environments.
- 10-minute timeout prevents a stuck LLM call from blocking the scheduler forever.
- Output is captured (not displayed) in normal mode. In `verbose` mode, stderr is shown on failure (first 500 chars).
- On success, calls `record_run()` to update stats.

### PID File Management

Three module-level functions manage a PID file to prevent duplicate scheduler instances:

**`write_pid_file(output_dir)`** — Writes the current process ID to `codilay/.scheduler.pid`. Called when the scheduler starts.

**`read_pid_file(output_dir)`** — Reads the PID file and validates the process is still alive via `os.kill(pid, 0)` (sends signal 0 — a no-op that checks process existence without actually signaling it). Returns `None` if:
- The PID file doesn't exist
- The PID can't be parsed as an integer
- The process is no longer running (`ProcessLookupError`)
- Permission is denied (`PermissionError`)

On any of these conditions, the stale PID file is automatically cleaned up.

**`remove_pid_file(output_dir)`** — Deletes the PID file. Called on scheduler shutdown.

### Same-Minute Deduplication

Cron expressions match at the minute level, but the polling loop runs every 60 seconds. Without deduplication, a cron match at e.g., 09:00 could fire twice if the loop happens to check at both 09:00:05 and 09:00:55.

The fix is simple: track `last_cron_minute` and only trigger if `now.minute != last_cron_minute`. After triggering, set `last_cron_minute = now.minute`.

### Design Decisions

- **Polling over inotify/webhooks** — Polling is universally portable (works on any OS, any git host, behind any firewall). It's less efficient than webhooks but perfectly adequate at a 60-second interval for a documentation tool.
- **Subprocess delegation** — The scheduler invokes the CLI as a subprocess rather than importing and calling internals directly. This keeps the scheduler decoupled from the documentation engine — if the engine crashes, the scheduler survives. It also means the scheduler runs with a fresh Python process each time, avoiding memory leaks from long-running processes.
- **Self-contained cron parser** — Adding `croniter` or `APScheduler` as dependencies for a 50-line cron parser would be overkill. The custom parser covers the syntax subset that 99% of users need.
- **No daemonization** — The scheduler runs in the foreground. Users who want it in the background can use `nohup`, `tmux`, `systemd`, or Docker. This avoids the complexity of double-forking, signal handling, and log file management.
- **First-run skip for commits** — Without this, starting the scheduler would always trigger an immediate update (since there's no previous commit to compare against). The skip ensures updates only fire on actual new commits.

---

## 10. Multi-user Web UI

**Module:** `src/codilay/server.py` (1129 lines)
**CLI command:** `codilay serve <path> [--host 127.0.0.1] [--port 8484]`
**Stack:** FastAPI + Uvicorn, single-page HTML frontend

### Purpose

Provide a hosted web interface where a whole team shares one live documentation view, a chat interface (with conversation management), and a knowledge base. This is the glue layer that exposes all other features via HTTP.

### Architecture

```
Browser (single-page HTML app)
        │
        ▼  HTTP / REST
    FastAPI Application (server.py)
        │
        ├─ Layer 1: Reader    (sections, document, links, stats, file viewer)
        ├─ Layer 2: Chatbot   (TF-IDF retrieval → LLM answer)
        ├─ Layer 3: Deep Agent (source file reading → LLM answer)
        │
        ├─ Conversation CRUD  (create, list, get, delete, rename, branch, export)
        ├─ Message Controls    (pin, edit, promote-to-doc)
        ├─ Memory Management   (view, clear, delete facts/preferences, extract)
        │
        ├─ Feature Endpoints:
        │   ├─ AI Export       (/api/export)
        │   ├─ Doc Diff        (/api/doc-diff, /api/doc-diff/snapshots)
        │   ├─ Triage Feedback (/api/triage-feedback)
        │   ├─ Graph Filters   (/api/graph/filters, /api/graph/filter)
        │   ├─ Team Memory     (/api/team/*)
        │   └─ Search          (/api/search, /api/search/rebuild)
        │
        ▼
    On-disk state (codilay/ directory)
```

### Server Factory Pattern

`create_app(target_path, output_dir)` is a factory function that builds a fully-wired FastAPI app for a specific project. All endpoints, state, and caches are scoped to that project via closure variables. This makes the server stateless at the module level — you could theoretically serve multiple projects from different ports.

### Lazy-Loading Cache

The server uses a `_cache` dictionary with mtime-based invalidation:

```python
def _file_changed(path: str, mtime_key: str) -> bool:
    mtime = os.path.getmtime(path)
    if _cache.get(mtime_key) != mtime:
        _cache[mtime_key] = mtime
        return True
    return False
```

Four items are cached:
- **AgentState** — Section index, section contents, processed file list. Reloaded when `.codilay_state.json` changes.
- **Links** — Wire/dependency data. Reloaded when `links.json` changes.
- **CODEBASE.md** — Full rendered document. Reloaded when the file changes.
- **Retriever** — TF-IDF retriever built from sections. Rebuilt when state changes.

This means the server automatically picks up changes from `codilay run` or `codilay watch` without requiring a restart.

### The Three-Layer Chat System

The chat endpoint (`POST /api/chat`) implements a progressive escalation strategy:

**Layer 1: Reader** — Not a chat layer per se, but the document and section endpoints that the frontend uses for browsing.

**Layer 2: Chatbot** — Answers questions from documentation context:

1. Retrieves the 5 most relevant sections via TF-IDF (`Retriever.search`).
2. Builds context from: relevant sections, cross-session memory, pinned messages, and conversation history (last 3 exchanges).
3. Sends to the LLM with a chat-specific system prompt.
4. Parses the response for a `CONFIDENCE:` line (0.0–1.0).
5. If confidence >= 0.7 and the answer is non-empty, returns it.
6. Otherwise, escalates to Layer 3.

**Layer 3: Deep Agent** — Reads actual source files:

1. Uses the Retriever to find the 5 most relevant source files.
2. Also includes files referenced by the relevant sections.
3. Falls back to keyword matching against file paths in the processed list.
4. Reads up to 5 files (truncated at 10KB each).
5. Sends file contents + doc context + conversation history to the LLM.
6. Returns the answer with the list of source files used.

**Keyword-based escalation:** Certain question patterns bypass Layer 2 entirely and go straight to the Deep Agent:
- "show me the code", "source code", "line by line"
- "read the file", "look at the file", "open the file"
- "implementation detail", "actual code", "exactly how"

### Conversation Management API

Full CRUD for conversations, plus advanced operations:

| Endpoint | Method | Description |
|---|---|---|
| `/api/conversations` | GET | List all conversations, newest first |
| `/api/conversations` | POST | Create a new conversation |
| `/api/conversations/{id}` | GET | Get full conversation with messages |
| `/api/conversations/{id}` | DELETE | Delete a conversation |
| `/api/conversations/{id}/title` | PATCH | Rename a conversation |
| `/api/conversations/{id}/messages/{msg}/pin` | POST | Pin/unpin a message |
| `/api/conversations/{id}/messages/{msg}/edit` | POST | Edit a message (truncates conversation after it) |
| `/api/conversations/{id}/branch/{msg}` | POST | Fork a new conversation from a specific message |
| `/api/conversations/{id}/export` | GET | Export conversation as markdown |
| `/api/conversations/{id}/pinned` | GET | Get pinned messages in conversation |
| `/api/pinned` | GET | Get all pinned messages across all conversations |
| `/api/conversations/{id}/messages/{msg}/promote` | POST | Promote an assistant answer to a doc section |
| `/api/conversations/{id}/extract-memory` | POST | Extract knowledge from conversation into memory |

**Promote-to-doc** is notable: it takes an assistant answer, uses the LLM to refine it into a documentation section, inserts it into the DocStore, re-renders CODEBASE.md, updates state, and invalidates all caches. This bridges the chat and documentation systems.

### Feature Endpoints

Each of the new features is exposed via REST endpoints:

**AI Export** (`/api/export`):
- `GET` and `POST` variants for flexibility (GET for curl/browser, POST for structured requests).
- Parameters: `format`, `max_tokens`, `include_graph`, `include_unresolved`.
- Uses `asyncio.to_thread` to run the (synchronous) exporter without blocking the event loop.

**Doc Diff** (`/api/doc-diff`, `/api/doc-diff/snapshots`):
- Diff endpoint returns a full `DocDiffResult.to_dict()`.
- Snapshots endpoint lists available snapshots with metadata.
- Returns a descriptive message if fewer than 2 snapshots exist.

**Triage Feedback** (`/api/triage-feedback`):
- GET returns all entries + project hints.
- POST adds a new correction (via `TriageFeedbackRequest` Pydantic model).
- DELETE removes a specific entry by file path.

**Graph Filters** (`/api/graph/filters`, `/api/graph/filter`):
- Filters endpoint returns available wire types, layers, and files.
- Filter endpoint accepts a `GraphFilterRequest` and returns a filtered graph.
- Both lazy-import `graph_filter` module to avoid loading it when unused.

**Team Memory** (`/api/team/*`):
- Full CRUD for facts, decisions, conventions, annotations, and users.
- Each entity type has its own Pydantic request model.
- Facts support voting (`/api/team/facts/{id}/vote`).
- Decisions support status updates (`PATCH /api/team/decisions/{id}`).
- Context endpoint (`/api/team/context`) returns the LLM-ready text block.

**Conversation Search** (`/api/search`, `/api/search/rebuild`):
- Search auto-loads the index, building it if needed (`asyncio.to_thread` for the blocking build).
- Supports `q`, `top_k`, `role`, and `conversation_id` query parameters.
- Rebuild endpoint forces a full re-index.

### Security

**Path traversal protection** on the file viewer endpoint:

```python
real_target = os.path.realpath(target_path)
real_file = os.path.realpath(full_path)
if not real_file.startswith(real_target):
    raise HTTPException(status_code=403, detail="Access denied")
```

This resolves symlinks and `..` traversal before checking that the requested file is within the project directory. Prevents reading arbitrary files on the host.

### Frontend

The frontend is a self-contained single-page HTML file served from `src/codilay/web/index.html`. The server falls back to a minimal placeholder if the file is missing.

The frontend is loaded by `GET /` and communicates entirely via the REST API. This decoupled architecture means:
- The API can be used independently (curl, scripts, other tools).
- The frontend can be replaced or extended without touching the server.
- The VSCode extension uses the same API endpoints.

### Design Decisions

- **Factory function over class** — `create_app()` uses closures to scope all state. This avoids the need for a global singleton or dependency injection framework, and makes testing easy (create an app, hit it with `TestClient`).
- **Mtime-based cache invalidation** — Simpler than file watchers and sufficient for a documentation tool. The tradeoff is that changes are detected on the next request, not instantly — typically within seconds.
- **Progressive chat escalation** — Layer 2 (doc context) is fast and cheap (no file I/O, small LLM prompt). Layer 3 (source files) is slower and more expensive. Escalating only when needed saves cost and latency for most questions.
- **Lazy imports for feature modules** — Feature modules (`exporter`, `doc_differ`, `triage_feedback`, etc.) are imported inside their endpoint handlers, not at module level. This keeps server startup fast and avoids import errors if a feature module has issues.
- **`asyncio.to_thread` for blocking operations** — The LLM client, file I/O, and search indexing are synchronous. Wrapping them in `to_thread` prevents them from blocking the async event loop, keeping the server responsive for concurrent requests.
- **Single port, full API** — Rather than splitting features across microservices, everything is served from one FastAPI app on one port. For a documentation tool used by a small-to-medium team, this simplicity far outweighs any scaling concerns.

---

## Cross-Feature Integration Summary

All 10 features are integrated at three levels:

### CLI Level (`cli.py`)
Every feature has at least one CLI command. Features that hook into `codilay run` (doc snapshots, triage feedback) are wired directly into the run flow.

### Server Level (`server.py`)
Every feature has REST endpoints, enabling the web UI, VSCode extension, and external scripts to use them programmatically.

### Data Level (`codilay/` directory)
All features store data within the project's `codilay/` directory, keeping everything portable and version-controllable:

```
codilay/
├── CODEBASE.md              # Generated documentation
├── .codilay_state.json      # Agent state (sections, wires, processed files)
├── links.json               # Wire/dependency data
├── history/                  # Feature 4: Doc snapshots
│   └── snapshot_*.json
├── triage_feedback.json      # Feature 5: Triage corrections
├── team/                     # Feature 8: Team memory
│   ├── memory.json
│   └── users.json
├── chat/                     # Features 6, 9: Conversations + search
│   ├── conversations/
│   │   └── *.json
│   └── search_index.json
├── schedule.json             # Feature 10: Scheduler config
└── .scheduler.pid            # Feature 10: Daemon PID
```
