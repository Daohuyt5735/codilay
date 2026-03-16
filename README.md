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

```bash
# Clone and install
git clone https://github.com/HarmanPreet-Singh-XYT/codilay.git
cd codilay

# Install with Web UI support
pip install -e ".[serve]"
```

### 2. First-Time Setup
Forget about exporting API keys every time. Run the setup wizard to securely store your keys.

```bash
codilay setup
```

### 3. Launch the Hub
Simply running `codilay` with no arguments opens the interactive control center.

```bash
codilay
```

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
Prefer a GUI? Launch the interactive documentation viewer:

```bash
codilay serve .
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
| `codilay diff .` | See what changed since the last documentation run |
| `codilay setup` | Configure default provider, model, and API keys |
| `codilay keys` | Manage stored API keys |
| `codilay clean .` | Wipe all generated artifacts |

---

## ⚙️ Project Configuration

Place a `codilay.config.json` in your root for project-specific behavior:

```json
{
  "ignore": ["dist/**", "**/tests/**"],
  "notes": "This is a React/Next.js frontend using Tailwind.",
  "instructions": "Focus on data-fetching patterns and state management.",
  "llm": {
    "provider": "anthropic",
    "model": "claude-3-5-sonnet-latest"
  }
}
```

### 🌍 Multi-Provider Support
CodiLay supports **10+ providers** including:
- **Local**: Ollama (no key needed)
- **Top Tier**: Anthropic, OpenAI, Google Gemini
- **Performance**: Groq, DeepSeek, Mistral
- **Custom**: Any OpenAI-compatible endpoint

---

## 📂 Project Structure

```text
codilay/
├── src/codilay/
│   ├── cli.py           # Multi-command CLI & Path Routing
│   ├── processor.py     # Main Agentic Loop & Chunking logic
│   ├── wire_manager.py  # Dependency & Reference tracking
│   ├── docstore.py      # Markdown assembly & Invalidation
│   ├── chatstore.py     # Chat history & Persistent Memory
│   ├── server.py        # FastAPI Backend for Web UI
│   └── web/             # Web UI Frontend assets
├── codilay/             # Output folder for your project
│   ├── CODEBASE.md      # The final living reference
│   ├── links.json       # Traced wires & Dependency data
│   └── .codilay_state.json # Internal state for incremental runs
└── pyproject.toml       # Build system & Dependencies
```

---

## 🤝 Contributing

We love contributors! Trace your own wires into the project by checking out [CONTRIBUTING.md](CONTRIBUTING.md).

1.  **Fork** the repo.
2.  **Install** dev deps: `pip install -e ".[dev]"`
3.  **Test**: `pytest`
4.  **Submit** a PR.

---

## 📜 License

Distributed under the **MIT License**. See `LICENSE` for details.

---

*Generated by CodiLay — Documenting the future, one wire at a time.*
SE) for more information.

---

*Generated by CodiLay — for the next developer who has to read this codebase.*