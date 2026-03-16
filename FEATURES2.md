# CodiLay — New Features Implementation Guide (Part 2)

This document continues the feature implementation documentation from FEATURES.md, covering features 6–8. Features 9–10 will be documented separately.

---

## Table of Contents

6. [Graph Filters](#6-graph-filters)
7. [Team Memory](#7-team-memory)
8. [Conversation Search](#8-conversation-search)
9. Scheduled Re-runs *(documented separately)*
10. Multi-user Web UI *(documented separately)*

---

## 6. Graph Filters

**Module:** `src/codilay/graph_filter.py` (303 lines)
**CLI command:** `codilay graph <path> [--wire-type import] [--layer src] [--module "api/*"] [--exclude "*.test.*"] [--direction outgoing] [--min-connections 2] [--format text|json]`
**Server endpoints:** `GET /api/graph/filters`, `POST /api/graph/filter`

### Purpose

CodiLay's Wire Model traces every import, call, and reference across the codebase as "wires". On large repos this graph can have thousands of edges, making it unreadable. Graph filters let users slice the dependency graph by wire type, file layer, module pattern, direction, and minimum connection count to surface only the subgraph they care about.

### Architecture

```
links.json (closed_wires + open_wires)
        │
        ▼
    GraphFilter
        │
        ▼  Sequential filter pipeline
    ┌──────────────────────────────────────┐
    │  wire type → layer → module →        │
    │  exclude → direction → min conns     │
    └──────────────────────────────────────┘
        │
        ▼
    FilteredGraph (nodes + edges + stats)
```

### Key Data Structures

**`GraphFilterOptions`** (dataclass) — The filter specification:

| Field | Type | Default | Description |
|---|---|---|---|
| `wire_types` | `list[str]` | `[]` | Only include wires of these types (e.g., `import`, `call`, `reference`). Empty = all. |
| `layers` | `list[str]` | `[]` | Only include files under these directory prefixes (e.g., `src`, `lib`). |
| `modules` | `list[str]` | `[]` | Only include files matching these glob patterns (e.g., `api/*`, `src/services/**`). |
| `exclude_files` | `list[str]` | `[]` | Exclude files matching these patterns. |
| `direction` | `str` | `"both"` | `"incoming"`, `"outgoing"`, or `"both"`. |
| `max_depth` | `int?` | `None` | Reserved for future graph traversal depth limits. |
| `min_connections` | `int` | `0` | Only include nodes with at least this many total connections. |

**`FilteredNode`** (dataclass) — A node in the filtered result:

| Field | Type | Description |
|---|---|---|
| `path` | `str` | Full file path |
| `label` | `str` | File basename (e.g., `router.py`) |
| `layer` | `str` | Inferred layer from first directory component |
| `incoming` | `int` | Count of incoming edges |
| `outgoing` | `int` | Count of outgoing edges |

**`FilteredEdge`** (dataclass) — An edge in the filtered result:

| Field | Type | Description |
|---|---|---|
| `source` | `str` | Source file path |
| `target` | `str` | Target file path |
| `wire_type` | `str` | Wire type (`import`, `call`, `reference`, etc.) |
| `summary` | `str` | Wire summary or context string |
| `wire_id` | `str` | Original wire ID |

**`FilteredGraph`** (dataclass) — The complete result:

| Field | Type | Description |
|---|---|---|
| `nodes` | `list[FilteredNode]` | Filtered nodes with connection counts |
| `edges` | `list[FilteredEdge]` | Filtered edges |
| `total_wires` | `int` | Total wire count before filtering |
| `filtered_wires` | `int` | Wire count after filtering |
| `filters_applied` | `dict` | Echo of which filters were used |

Computed properties: `available_wire_types` (set of wire types in edges), `available_layers` (set of layers in nodes). Serializer: `to_dict()`.

### Filter Pipeline

**`GraphFilter`** is constructed with two wire lists (closed and open) and applies filters as a sequential pipeline. Each stage reduces the wire set further:

1. **Wire type filter** — If `wire_types` is non-empty, keep only wires whose `type` field matches one of the specified types.

2. **Layer filter** — If `layers` is non-empty, keep only wires where either the `from` or `to` path starts with one of the specified layer prefixes. Layer is inferred from the first directory component of the file path (e.g., `src/api/router.py` → layer `src`).

3. **Module filter** — If `modules` is non-empty, keep only wires where either endpoint matches one of the glob patterns via `fnmatch`. Supports both basename matching (`router.py`) and path matching (`src/api/*`).

4. **Exclude filter** — If `exclude_files` is non-empty, remove wires where either endpoint matches any exclusion pattern. Also uses `fnmatch`, checking both the full path and the basename.

5. **Direction filter** — If `direction` is `"outgoing"`, only keep wires originating from matched files. If `"incoming"`, only keep wires targeting matched files. `"both"` keeps all surviving wires. Direction is determined relative to the set of files that appeared in the module/layer filters.

6. **Node building** — From the surviving wires, build a `FilteredNode` for each unique file path, counting incoming and outgoing edges.

7. **Min-connections pruning** — If `min_connections > 0`, remove nodes (and their edges) where `incoming + outgoing < min_connections`. This eliminates leaf nodes with few dependencies, focusing the graph on high-connectivity hubs.

### Introspection

`GraphFilter.get_available_filters()` scans the full (unfiltered) wire set and returns:

```python
{
    "wire_types": ["import", "call", "reference"],
    "layers": ["src", "lib", "tests"],
    "files": ["src/api/router.py", "src/db/models.py", ...]
}
```

This powers dynamic filter UIs — the web UI and VSCode extension call `GET /api/graph/filters` to populate dropdown options.

### Wire Data Format

The filter reads wire data as `List[Dict[str, Any]]` with these keys:

| Key | Required | Description |
|---|---|---|
| `from` | yes | Source file path |
| `to` | yes | Target file path |
| `type` | no | Wire type string (defaults to `"unknown"`) |
| `summary` or `context` | no | Description of the dependency |
| `id` | no | Wire identifier |

Missing `from` or `to` fields cause the wire to be silently skipped.

### Design Decisions

- **Pipeline over predicate composition** — A sequential pipeline is easier to debug and reason about than a single complex predicate. Each stage logs its input/output count for diagnostics.
- **Layer inference from path** — Rather than requiring explicit layer annotations, the first directory component is used as a heuristic. This works well for standard project layouts (`src/`, `lib/`, `tests/`, `cmd/`).
- **Both open and closed wires** — Open wires (unresolved dependencies) are included in the filter input because they can reveal missing connections. The `include_unresolved` flag on the CLI controls whether they're shown.
- **Min-connections as a noise reducer** — On large repos, most files have 1–2 connections. Filtering to `min_connections=3` or higher reveals the architectural skeleton — the high-traffic modules that everything depends on.

---

## 7. Team Memory

**Module:** `src/codilay/team_memory.py` (342 lines)
**CLI commands:** `codilay team facts|add-fact|vote|decisions|add-decision|conventions|add-convention|annotate|annotations|users|add-user`
**Server endpoints:** Full CRUD at `/api/team/facts`, `/api/team/decisions`, `/api/team/conventions`, `/api/team/annotations`, `/api/team/users`, `/api/team/context`

### Purpose

Provide a shared knowledge base for teams working on the same project. Team members can record facts, architectural decisions, coding conventions, and file-level annotations that persist across sessions and users. This knowledge is also injected into LLM prompts so the AI gives answers that respect team context.

### Architecture

```
CLI / Web UI / API
        │
        ▼
    TeamMemory
        │
        ├─ Facts       (voteable knowledge items)
        ├─ Decisions    (ADR-style records with lifecycle)
        ├─ Conventions  (coding standards with examples)
        └─ Annotations  (file-level notes, optionally line-ranged)
        │
        ▼
    codilay/team/memory.json   (all entities)
    codilay/team/users.json    (user registry)
        │
        ▼
    build_context()  →  Text block for LLM prompt injection
```

### Entity Types

**Facts** — Short knowledge items that can be upvoted or downvoted by the team. Higher-voted facts surface first in LLM context.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | 12-char hex UUID |
| `fact` | `str` | The knowledge statement |
| `category` | `str` | Category tag (e.g., `architecture`, `deployment`, `gotcha`) |
| `author` | `str` | Who added it |
| `tags` | `list[str]` | Searchable tags |
| `upvotes` | `int` | Positive votes |
| `downvotes` | `int` | Negative votes |
| `created_at` | `str` | ISO timestamp |

Facts are sorted by net score (`upvotes - downvotes`), then by recency.

**Decisions** — Architectural Decision Record (ADR) style entries with a lifecycle status.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | 12-char hex UUID |
| `title` | `str` | Decision title |
| `description` | `str` | Full decision description and rationale |
| `author` | `str` | Who recorded it |
| `status` | `str` | One of `active`, `superseded`, `deprecated` |
| `related_files` | `list[str]` | Files affected by this decision |
| `created_at` | `str` | ISO timestamp |
| `updated_at` | `str` | Last status change |

Decisions can be filtered by status. The `update_decision_status(decision_id, status)` method transitions decisions through their lifecycle.

**Conventions** — Coding standards the team has agreed on, with concrete examples.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | 12-char hex UUID |
| `name` | `str` | Convention name (e.g., `"Error handling pattern"`) |
| `description` | `str` | What the convention is |
| `examples` | `list[str]` | Code examples demonstrating the convention |
| `author` | `str` | Who added it |
| `created_at` | `str` | ISO timestamp |

**Annotations** — File-level (and optionally line-ranged) notes.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | 12-char hex UUID |
| `file_path` | `str` | Which file the annotation is about |
| `note` | `str` | The annotation text |
| `author` | `str` | Who wrote it |
| `line_range` | `str?` | Optional line range (e.g., `"10-25"`) |
| `created_at` | `str` | ISO timestamp |

Annotations are filtered by file path via `get_annotations(file_path)`.

### User Management

Team members register via `register_user(username, display_name)`. Each user entry tracks:

- `id`, `username`, `display_name`
- `registered_at`, `last_seen` (updated on each registration call)
- `role` (always `"member"` currently)

Users are stored separately in `codilay/team/users.json` to allow lightweight presence tracking without touching the larger memory file.

### LLM Context Building

`build_context()` produces a curated text block for injection into LLM prompts:

```
## Team Knowledge

### Key Facts (by team consensus):
1. [+5] The payment service uses eventual consistency (architecture)
2. [+3] Never use raw SQL in the API layer (gotcha)
...

### Active Decisions:
- Use PostgreSQL for all new services (related: src/db/*, src/services/*)
- Migrate from REST to gRPC for internal services
...

### Coding Conventions:
- Error handling pattern: All service methods return Result<T, AppError>
- Naming: Use snake_case for Python, camelCase for TypeScript
...
```

The context is capped at the top 15 facts (by vote), top 10 active decisions, and top 10 conventions. This keeps the prompt injection within a reasonable token budget while surfacing the most important team knowledge.

### Importing from User Memory

`import_from_user_memory(user_memory, author)` bridges CodiLay's existing per-user memory system into the shared team memory. It:

1. Reads facts from a user's personal memory store.
2. Deduplicates against existing team facts (by exact text match).
3. Adds new facts to team memory with the specified author.
4. Returns the count of imported facts.

This lets a team member "share" their accumulated knowledge with the team in one operation.

### Storage and Atomicity

Both files use atomic writes to prevent corruption:

```python
tmp_path = filepath + ".tmp"
with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=1)
os.replace(tmp_path, filepath)
```

`os.replace()` is atomic on POSIX systems, ensuring that a crash mid-write doesn't leave a corrupted file. The worst case is losing the most recent write, not corrupting the entire store.

### Storage Format

`codilay/team/memory.json`:
```json
{
  "facts": [...],
  "decisions": [...],
  "conventions": [...],
  "annotations": [...],
  "updated_at": "2025-01-15T10:30:00+00:00"
}
```

`codilay/team/users.json`:
```json
[
  {
    "id": "a1b2c3d4e5f6",
    "username": "alice",
    "display_name": "Alice Chen",
    "registered_at": "2025-01-10T08:00:00+00:00",
    "last_seen": "2025-01-15T14:22:00+00:00",
    "role": "member"
  }
]
```

### Design Decisions

- **Voting on facts** — Not all team knowledge is equally important or correct. Voting lets the team surface high-confidence facts and bury outdated ones, without requiring anyone to delete them.
- **ADR-style decisions with lifecycle** — Architectural decisions evolve. A decision marked `active` today may be `superseded` next quarter. The lifecycle status prevents stale decisions from polluting LLM context.
- **Separate user file** — User presence data changes frequently (every `last_seen` update), while team memory changes infrequently. Separate files prevent unnecessary churn on the larger memory file.
- **Atomic writes** — Team memory is a shared resource. Multiple users may write concurrently (via the web UI). Atomic writes prevent file corruption, though last-write-wins semantics mean concurrent writes can lose data. A future improvement could add optimistic locking.
- **Capped context building** — Injecting thousands of facts into an LLM prompt wastes tokens and dilutes important information. The caps (15 facts, 10 decisions, 10 conventions) are a deliberate trade-off between completeness and focus.

---

## 8. Conversation Search

**Module:** `src/codilay/search.py` (451 lines)
**CLI command:** `codilay search <path> <query> [--top-k 10] [--role user|assistant] [--rebuild]`
**Server endpoints:** `GET /api/search?q=...&top_k=...`, `POST /api/search/rebuild`

### Purpose

Provide full-text search across all past conversations, not just the current one. When you've had dozens of chat sessions about your codebase, finding "that conversation where we discussed the auth flow" becomes critical.

### Architecture

```
codilay/chat/conversations/*.json
        │
        ▼  build_index()
    Tokenizer (regex + stop words)
        │
        ▼
    Inverted Index (term → [(conv_id, msg_id, tf)])
        │
        ▼  persist
    codilay/chat/search_index.json
        │
        ▼  search(query)
    TF-IDF Scoring + Length Normalization
        │
        ▼
    SearchResults (ranked hits with snippets)
```

### Tokenization

`_tokenize(text)` is a simple but effective tokenizer:

1. **Regex extraction** — `[a-zA-Z0-9_]+` captures words and identifiers (including snake_case and camelCase as single tokens).
2. **Lowercasing** — All tokens are lowercased for case-insensitive matching.
3. **Stop word removal** — A frozen set of ~100 common English stop words (`the`, `is`, `and`, `in`, `to`, etc.) is filtered out.
4. **Minimum length** — Tokens shorter than 2 characters are discarded.

This approach handles code identifiers well (preserving `get_user`, `handleClick`, `PostgreSQL`) while filtering noise. No external NLP library is needed.

### Index Building

`build_index()` scans all conversation JSON files and builds an in-memory inverted index:

1. Iterate over every `.json` file in `codilay/chat/conversations/`.
2. For each conversation, extract metadata (title, created_at) and iterate over messages.
3. For each message, tokenize the content and compute term frequencies.
4. Store in the inverted index: `term → [(conv_id, msg_id, normalized_tf)]`.
5. Track document lengths (token counts) for length normalization.
6. Cache conversation metadata for result building.
7. Persist the full index to `search_index.json`.

**Augmented Term Frequency:**

Rather than raw counts, the index stores augmented TF:

```
augmented_tf = 0.5 + 0.5 * (term_count / max_term_count_in_document)
```

This prevents long documents from dominating results simply by having more occurrences of common terms.

### TF-IDF Scoring

`search(query, top_k, role_filter, conv_id_filter)` scores documents using TF-IDF:

1. Tokenize the query.
2. For each query term, look up the inverted index to find matching documents.
3. Compute IDF (Inverse Document Frequency) for each query term:
   ```
   idf = log((1 + N) / (1 + df)) + 1
   ```
   Where `N` is the total number of documents and `df` is the number of documents containing the term. The `+1` smoothing prevents zero IDF for terms appearing in all documents.
4. For each matching document, sum `tf * idf` across all query terms.
5. Normalize by document length: `score / sqrt(doc_length)`. This prevents long messages from always ranking higher.
6. Sort by score descending, take top-k.

### Filtering

Two optional filters narrow results before scoring:

- **`role_filter`** — Only search messages from a specific role (`user` or `assistant`). Useful for finding your own questions vs. the AI's answers.
- **`conv_id_filter`** — Restrict search to a specific conversation. Useful when you know roughly which conversation had the answer.

Filters are applied during the scoring phase — filtered-out documents are skipped before score computation, keeping search fast.

### Snippet Extraction

`_make_snippet(content, query_tokens)` generates a contextual snippet around the best match:

1. Scan the content to find the position where query terms cluster most densely. For each position, count how many query terms appear within a 120-character window.
2. Select the window with the highest density.
3. Extract ~120 characters centered on that window.
4. Add `...` prefix/suffix if the snippet doesn't start/end at the content boundaries.

This ensures the snippet shows the most relevant part of the message, not just the beginning.

### Result Data Structures

**`SearchResult`** (dataclass) — A single search hit:

| Field | Type | Description |
|---|---|---|
| `conversation_id` | `str` | Which conversation |
| `conversation_title` | `str` | Conversation title for display |
| `message_id` | `str` | Specific message within the conversation |
| `role` | `str` | `user` or `assistant` |
| `content` | `str` | Full message content |
| `snippet` | `str` | ~120-char contextual snippet |
| `score` | `float` | TF-IDF relevance score |
| `created_at` | `str` | Message timestamp |
| `escalated` | `bool` | Whether the message was escalated |

**`SearchResults`** (dataclass) — Aggregate result:

| Field | Type | Description |
|---|---|---|
| `query` | `str` | Original search query |
| `results` | `list[SearchResult]` | Ranked results |
| `total_conversations` | `int` | Number of conversations in the index |
| `total_messages` | `int` | Number of messages in the index |
| `search_time_ms` | `float` | Time taken (for diagnostics) |

Serializer: `to_dict()`.

### Index Persistence

The index is saved to `codilay/chat/search_index.json` as:

```json
{
  "inverted_index": {
    "authentication": [["conv_abc", "msg_001", 0.85], ...],
    "database": [["conv_def", "msg_003", 0.72], ...]
  },
  "doc_lengths": {
    "conv_abc:msg_001": 145,
    "conv_def:msg_003": 89
  },
  "conv_metadata": {
    "conv_abc": {"title": "Auth flow discussion", "created_at": "2025-01-10T..."}
  },
  "total_docs": 234,
  "built_at": "2025-01-15T10:30:00+00:00"
}
```

Index persistence is non-critical — errors during save/load are silently caught. If the index file is missing or corrupt, the search auto-rebuilds it from the conversation files.

### Auto-rebuild Behavior

If `search()` is called and the in-memory index is empty, it automatically calls `build_index()` before searching. This means the first search after a fresh install is slightly slower (needs to scan all conversations) but subsequent searches use the cached index. The `--rebuild` CLI flag or `POST /api/search/rebuild` endpoint forces a full re-index.

### Design Decisions

- **Custom TF-IDF over external search libraries** — CodiLay's conversation corpus is small (hundreds to low thousands of messages). A full search engine (Whoosh, Elasticsearch) would be overkill. The custom implementation is ~150 lines, zero-dependency, and fast enough for this scale.
- **Augmented TF** — Standard raw term frequency overweights long documents. Augmented TF normalizes within each document, giving fairer scores to short, focused messages.
- **Length normalization via sqrt** — Dividing by `sqrt(doc_length)` rather than `doc_length` is a standard IR technique that partially compensates for document length without completely eliminating the advantage of longer documents (which genuinely do contain more information).
- **~100 stop words** — A small, fixed stop word list is sufficient for code-oriented conversations. Too aggressive filtering (using a 500+ word list) would remove technical terms that happen to be common English words.
- **120-char snippets** — Long enough to provide context, short enough to scan in a results list. The density-based window selection ensures the snippet is centered on the most relevant part of the message.
- **Non-critical persistence** — The index is a cache, not source-of-truth. Conversation JSON files are the source. If the index is lost, it's rebuilt from scratch with no data loss.

---

*Features 9–10 (Scheduled Re-runs, Multi-user Web UI) will be documented in a subsequent update.*
