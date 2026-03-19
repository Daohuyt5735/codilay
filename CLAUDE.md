# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**CodiLay** is an AI-powered agentic documentary researcher that transforms static documentation into a living, searchable knowledge base. It reads a codebase, traces dependencies via **The Wire Model** (imports, function calls, and references treated as resolvable "wires"), and maintains persistent documentation browsable via Web UI or queryable through interactive Chat.

## Commands

```bash
# Install in development mode
pip install -e ".[dev]"

# Run all tests
pytest

# Run a single test file
pytest tests/test_dependency_graph.py -v

# Lint
ruff check src/

# Format
ruff format src/
```

## Architecture

### Processing Pipeline

```
CLI → Config → Scanner (files, .gitignore) → GitTracker (incremental changes)
  → Triage (Core/Skim/Skip) → Planner (LLM-driven order)
  → DependencyGraph (topological tiers) → ParallelOrchestrator
    → Processor (per file) + WireManager + WireBus + DocStore
  → AgentState (persisted to state.json) → CODEBASE.md
```

### The Wire Model

The core abstraction. Each import/function call is a "wire":
- **Open wires**: unresolved references being hunted during processing
- **Closed wires**: successfully resolved references

Wires drive processing order and track what documentation is still needed. `wire_manager.py` owns the lifecycle; `wire_bus.py` is the thread-safe event bus that notifies workers when dependencies resolve.

### Key Modules

| Module | Role |
|--------|------|
| `cli.py` | Entry point with custom `CodiLayGroup` Click resolver (distinguishes file paths from subcommands) |
| `processor.py` | Agent loop; calls LLM per file with RAG context and wire state |
| `parallel_orchestrator.py` | Tier-based parallel execution; workers within a tier run concurrently, tiers are sequential |
| `wire_bus.py` | Thread-safe wire state with snapshot isolation — each worker gets a frozen snapshot at job start |
| `dependency_graph.py` | Static import analysis → DAG → topological tiers |
| `triage.py` | Fast mode (static patterns) or Smart mode (LLM sees file tree, ~500–1000 tokens, no content) |
| `docstore.py` | Section-based CODEBASE.md management with git awareness |
| `retriever.py` | TF-IDF relevance scoring for RAG context (saves 75–85% tokens in chat) |
| `llm_client.py` | Multi-provider abstraction (Anthropic, OpenAI, Ollama, Gemini, DeepSeek, Mistral, Groq, xAI, etc.) |
| `server.py` | FastAPI web server with three-layer UI: Reader / Chatbot / Deep Agent |
| `scheduler.py` | Cron + on-commit auto-run daemon (no external deps) |
| `audit_manager.py` | 60+ audit types (security, architecture, performance, compliance) |
| `team_memory.py` | Shared knowledge base: facts, decisions, conventions, annotations |

### State Persistence

- `AgentState` (dataclass in `state.py`) is written atomically (write-then-rename) to `state.json`
- Chat history: `codilay/chat/conversations/{conv_id}.json`
- Team memory: `codilay/team/memory.json` + `users.json`
- Audit reports: `codilay/audits/{type}_{timestamp}.json`
- TF-IDF search index kept in-memory per session

### Chunking Strategy

Large files are split into a **skeleton chunk** (high-level structure) and **detail chunks** (implementation). The skeleton informs the planner; detail chunks fill in specifics. Threshold/max tokens controlled via `codilay.config.json`.

### Configuration

Project config: `codilay.config.json` in the target project root (not this repo). Key fields:
- `llm.model` / `llm.maxTokensPerCall` — LLM parameters
- `triage.mode` — `"fast"` (static) or `"smart"` (LLM-assisted)
- `chunking.tokenThreshold` / `maxChunkTokens` / `overlapRatio`
- `ignore` — glob patterns to exclude

### Linting Rules

Ruff is configured in `pyproject.toml` with rules E, F, I, W. Line length is 120. F401/F841/F541 are globally ignored. Long-line exemptions apply to `__init__.py`, `prompts.py`, `server.py`, and `menu.py`.

## Package Entry Point

`codilay = "codilay.cli:cli"` — installed via `pip install -e .`, invoked as `codilay [subcommand | path]`.


Do not try to take shortcuts like skipping tests or not following the architecture. Always follow the architecture and the tests. or maybe writing to bypass tests.