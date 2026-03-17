# 🦅 CodiLay

> **The Living Reference for Your Codebase** — An AI agent that traces the "wires" of your project to build, update, and chat with your documentation.

[![License: MIT](https://img.shields.io/badge/License-MIT-gold.svg?style=flat-square)](https://opensource.org/licenses/MIT)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg?style=flat-square)](https://www.python.org/downloads/)
[![PRs: Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square)](CONTRIBUTING.md)

---

CodiLay is not just a static documentation generator; it's an **agentic documentary researcher**. It reads your code, understands module connections via **The Wire Model**, and maintains a persistent, searchable knowledge base that you can browse via a Web UI or talk to through an interactive Chat.

---

## 🚀 Experience CodiLay

### 1. Installation

**Install from PyPI (Recommended)**
```bash
# Basic installation
pip install codilay

# Install with all features (Web UI + Watch mode)
pip install "codilay[all]"

# For a global CLI installation (recommended)
pipx install codilay
```

**Install from Source**
```bash
# Clone the repository
git clone https://github.com/HarmanPreet-Singh-XYT/codilay.git
cd codilay

# Install with Web UI support
pip install -e ".[serve]"

# Install with Watch mode support
pip install -e ".[watch]"

# Install everything (Web UI + Watch mode)
pip install -e ".[all]"
```

### 2. First-Time Setup
Forget about exporting API keys every time. Run the setup wizard to securely store your keys.

```bash
codilay setup
```

```bash
codilay
```

Running `codilay` with no arguments opens the **Interactive Control Center**, allowing you to manage projects, configure providers, and launch scans without memorizing flags.

---

## 🛠 Features

### 🧠 The Wire Model
CodiLay treats every import, function call, and variable reference as a **Wire**. 
- **Open Wires**: Unresolved references that the agent is "hunting" for.
- **Closed Wires**: Successfully traced connections that form segments of the dependency graph.

### ⚡️ Smart Triage
Before burning tokens, CodiLay performs a high-speed **Triage Phase**. It classifies files into:
- **Core**: Full architectural analysis and documentation.
- **Skim**: Metadata and signatures only (saves tokens on simple utilities).
- **Skip**: Ignores boilerplate, generated code, and platform-specific noise.

### 🔄 Git-Aware Incremental Updates
CodiLay is repo-aware. If you've only changed 2 files in a 500-file project, `codilay .` will:
1. Detect the delta via Git.
2. Invalidate only the affected documentation sections.
3. Re-open wires related to the changed code.
4. Re-calculate the local impact to keep your `CODEBASE.md` current.

### 💬 Interactive Chat & Memory
Ask questions about your codebase using `codilay chat .`. 
- **RAG + Deep Search**: It uses your documentation for fast answers but can "escalate" to reading source code for implementation details.
- **Memory**: The agent remembers your preferences and facts about the codebase across sessions.
- **Promote to Doc**: Found a great explanation in chat? Use `/promote` to turn the AI's answer into a permanent section of your documentation.

### 🌐 Web Documentation Browser
The Web UI isn't just a reader—it's an interactive intelligence layer.
- **Layer 1: The Reader**: High-fidelity rendering of your sections and graph.
- **Layer 2: The Chatbot**: Quick Q&A from documented context.
- **Layer 3: The Deep Agent**: Reaches into source code to verify facts.

```bash
codilay serve .
```

### 👁 Watch Mode
Run CodiLay in the background and automatically update documentation when files change. Uses filesystem events (via watchdog) with configurable debouncing to avoid redundant re-runs.

```bash
# Watch the current directory, auto-update on save
codilay watch .

# Custom debounce delay (5 seconds)
codilay watch . --debounce 5

# Verbose output for debugging
codilay watch . -v
```

### 🧩 IDE Integration (VSCode Extension)
A VSCode extension that surfaces documentation inline alongside the file you're editing. Features include:
- **Sidebar tree view** of all documented sections
- **Webview panel** showing full documentation for the active file
- **Inline decorations** highlighting documented symbols
- **Quick commands** for asking questions, viewing the graph, and searching conversations

Install from `vscode-extension/` directory — see the extension README for details.

### 🤖 AI Context Export
Export your documentation in a compact, token-efficient format designed for feeding into another LLM's context window. Supports markdown, XML, and JSON formats with optional token budgets.

```bash
# Export as compact markdown (default)
codilay export .

# Export as XML with a 4000-token budget
codilay export . --format xml --max-tokens 4000

# Export as JSON, exclude the dependency graph
codilay export . -f json --no-graph -o context.json
```

### 📊 Documentation Diff
See a section-by-section changelog of what shifted in your documentation between runs. Unlike `codilay diff` (which shows git-level file changes), `diff-doc` compares the actual documentation content.

```bash
# Show what changed in the docs since the last run
codilay diff-doc .

# Output as JSON for programmatic use
codilay diff-doc . --json-output
```

Snapshots are saved automatically after every `codilay run`, so diffs are always available.

### 🎯 Triage Tuning
Flag incorrect triage decisions to improve future runs. Corrections are stored per-project and automatically applied during the triage phase of subsequent runs.

```bash
# Flag a file that was skimmed but should be core
codilay triage-feedback add . src/auth/handler.py skim core -r "Contains critical auth logic"

# Flag a pattern (glob-based)
codilay triage-feedback add . "tests/**" core skip --pattern -r "Tests should be skipped"

# List all stored feedback
codilay triage-feedback list .

# Set a hint for a project type
codilay triage-feedback hint . react "Treat all hooks/ files as core"

# Remove feedback for a specific file
codilay triage-feedback remove . src/auth/handler.py

# Clear all feedback
codilay triage-feedback clear . --yes
```

### 🔍 Graph Filters
Filter the dependency graph by wire type, file layer, module, or connection count. Essential for reducing noise on large repositories.

```bash
# Show only import-type wires
codilay graph . --wire-type import

# Filter to a specific directory layer
codilay graph . --layer src/api

# Show only nodes with 3+ connections, outgoing edges only
codilay graph . --min-connections 3 --direction outgoing

# Combine filters, exclude tests
codilay graph . -w import -l src/core -x "tests/**"

# List available filter values for a project
codilay graph . --list-filters

# Output as JSON
codilay graph . --json-output
```

### 🧠 Team Memory
A shared knowledge base for teams working on the same project. Record facts, architectural decisions, coding conventions, and file annotations — all stored per-project and surfaced to the AI during documentation and chat.

```bash
# Add a team member
codilay team add-user . alice --display-name "Alice Chen"

# Record a fact
codilay team add-fact . "We use Celery for async tasks" -c architecture -a alice -t backend -t infra

# Vote on a fact
codilay team vote . <fact-id> up

# Record an architectural decision
codilay team add-decision . "Use PostgreSQL over MySQL" "Better JSON support, needed for our schema" -a alice -f src/db/

# Add a coding convention
codilay team add-convention . "Error Handling" "All API endpoints must return structured error responses" -e '{"error": "message", "code": 400}' -a alice

# Annotate a specific file
codilay team annotate . src/api/routes.py "This file is getting too large, plan to split by domain" -a alice -l 1-50

# List everything
codilay team facts .                   # All facts
codilay team facts . -c architecture   # Facts by category
codilay team decisions .               # All decisions
codilay team decisions . -s active     # Active decisions only
codilay team conventions .             # All conventions
codilay team annotations .             # All annotations
codilay team annotations . -f src/api/routes.py  # Per-file
codilay team users .                   # All members
```

### 🔎 Conversation Search
Full-text search across all past chat conversations — not just the current session. Uses an inverted index with TF-IDF scoring for fast, relevant results.

```bash
# Search all conversations
codilay search . "authentication flow"

# Top 5 results, assistant messages only
codilay search . "error handling" --top 5 --role assistant

# Search within a specific conversation
codilay search . "database migration" -c <conversation-id>

# Rebuild the index (after manual edits to chat files)
codilay search . "query" --rebuild
```

### 📅 Scheduled Re-runs
Automatically trigger documentation updates on a cron schedule or when new commits land on a branch. Runs as a background daemon with PID file management.

```bash
# Update docs every day at 2am
codilay schedule set . --cron "0 2 * * *"

# Update on every new commit to main
codilay schedule set . --on-commit --branch main

# Combine: cron + commit triggers
codilay schedule set . --cron "0 2 * * *" --on-commit

# Check current schedule
codilay schedule status .

# Start the scheduler (foreground)
codilay schedule start .

# Start with verbose logging
codilay schedule start . -v

# Stop a running scheduler
codilay schedule stop .

# Disable the schedule
codilay schedule disable .
```

---

## ⌨️ CLI Reference

| Command | Action |
|:---|:---|
| `codilay` | Launch the **Interactive Menu** |
| `codilay .` | Document the current directory (incremental) |
| `codilay chat .` | Start a **Chat session** about the project |
| `codilay serve .` | Launch the **Web UI** |
| `codilay status .` | Show documentation coverage and stale sections |
| `codilay diff .` | See what changed in files since the last run |
| `codilay setup` | Configure default provider, model, and API keys |
| `codilay keys` | Manage stored API keys |
| `codilay clean .` | Wipe all generated artifacts |
| `codilay watch .` | Watch for file changes, auto-update docs |
| `codilay export .` | Export docs in AI-friendly format (markdown/xml/json) |
| `codilay diff-doc .` | Show section-level documentation diff between runs |
| `codilay triage-feedback` | Manage triage corrections (add/list/hint/clear/remove) |
| `codilay graph .` | View and filter the dependency graph |
| `codilay team` | Manage shared team knowledge (facts/decisions/conventions) |
| `codilay search . "query"` | Full-text search across all past conversations |
| `codilay schedule` | Configure and run scheduled doc updates (set/start/stop) |

---

## ⚙️ Project Configuration

Place a `codilay.config.json` in your root for project-specific behavior:

```json
{
  "ignore": ["dist/**", "**/tests/**"],
  "notes": "This is a React/Next.js frontend using Tailwind.",
  "instructions": "Focus on data-fetching patterns and state management.",
  "entryHint": "src/main.py",
  "llm": {
    "provider": "anthropic",
    "model": "claude-3-5-sonnet-latest",
    "baseUrl": "https://api.anthropic.com",
    "maxTokensPerCall": 4096
  },
  "triage": {
    "mode": "smart",
    "includeTests": false,
    "forceInclude": ["critical_logic/*.py"],
    "forceSkip": ["legacy_v1/*.js"]
  },
  "chunking": {
    "tokenThreshold": 6000,
    "maxChunkTokens": 4000,
    "overlapRatio": 0.10
  },
  "parallel": {
    "enabled": true,
    "maxWorkers": 4
  }
}
```

### 📋 Configuration Fields

| Category | Key | Type | Description |
|:---|:---|:---|:---|
| **General** | `ignore` | `List[str]` | Glob patterns for files/folders to exclude from scans. |
| | `notes` | `str` | High-level project context provided to the AI. |
| | `instructions` | `str` | Specific documentation style or domain instructions. |
| | `entryHint` | `str` | Point to the main entry file to help trace wires. |
| | `skipGenerated` | `List[str]` | Optional override for default generated/lock file ignores. |
| **LLM** | `provider` | `str` | AI provider (e.g., `anthropic`, `openai`, `google`, `ollama`). |
| | `model` | `str` | Model identifier (e.g., `claude-3-5-sonnet-latest`). |
| | `baseUrl` | `str` | Custom API base URL (useful for local models or proxies). |
| | `maxTokensPerCall`| `int` | Maximum output tokens per individual agent call. |
| **Triage** | `mode` | `str` | Default classification strategy (`smart`, `core`, `skim`, `skip`). |
| | `includeTests` | `bool` | Whether to process test files (defaults to `false`). |
| | `forceInclude` | `List[str]` | Patterns to always treat as **Core** documentation. |
| | `forceSkip` | `List[str]` | Patterns to always ignore. |
| **Chunking** | `tokenThreshold` | `int` | Files larger than this (in tokens) are split into chunks. |
| | `maxChunkTokens` | `int` | Target token count for each detail chunk. |
| | `overlapRatio` | `float` | Contextual overlap between chunks (e.g. `0.10` for 10%). |
| **Parallel** | `enabled` | `bool` | Enable/disable concurrent processing of files within the same tier. |
| | `maxWorkers` | `int` | Max number of concurrent LLM calls. |

### 🌍 Multi-Provider Support
CodiLay is provider-agnostic. Power it with:
- **Cloud**: Anthropic (Sonnet/Haiku), OpenAI (GPT-4o), Google Gemini.
- **Local**: Ollama, Groq, Llama Cloud.
- **Specialty**: DeepSeek, Mistral.
- **Custom**: Any OpenAI-compatible endpoint.

---

## 📂 Project Structure

```text
src/codilay/
├── cli.py              # Command parsing & Interactive Menu
├── scanner.py          # Git-aware file walking
├── triage.py           # AI-powered file categorization
├── processor.py        # The Agent Loop & Large file chunking
├── wire_manager.py     # Linkage & Dependency resolution
├── docstore.py         # Living CODEBASE.md management
├── chatstore.py        # Persistent memory & Chat history
├── server.py           # FastAPI Intelligence Server (Web UI + API)
├── watcher.py          # File system watcher (watch mode)
├── exporter.py         # AI-friendly doc export (markdown/xml/json)
├── doc_differ.py       # Section-level doc diffing & version snapshots
├── triage_feedback.py  # Triage correction store & feedback loop
├── graph_filter.py     # Dependency graph filtering engine
├── team_memory.py      # Shared team knowledge base
├── search.py           # Full-text conversation search (inverted index)
├── scheduler.py        # Cron & commit-based auto re-runs
└── web/                # Premium Glassmorphic Frontend

vscode-extension/       # VSCode extension for inline doc surfacing
├── package.json
├── tsconfig.json
└── src/extension.ts
```

---

## 🤝 Contributing

We love contributors! Trace your own wires into the project by checking out [CONTRIBUTING.md](CONTRIBUTING.md).

1.  **Fork** the repo.
2.  **Install** dev deps: `pip install -e ".[all,dev]"`
3.  **Test**: `pytest`
4.  **Submit** a PR.

---

## 📜 License

Distributed under the **MIT License**. See `LICENSE` for details.

---

*Generated by CodiLay — Documenting the future, one wire at a time.*