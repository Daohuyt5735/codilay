"""
CodiLay Settings — persistent, cross-session configuration.

Settings are stored in ~/.codilay/settings.json and survive terminal restarts,
new sessions, etc.  They hold:
    • API keys for each provider
    • The user's preferred (default) provider & model
    • Global preferences (verbose, triage mode, …)
    • Doc output location preference (where CODEBASE.md is written / gitignored)
    • Documentation style (response style, detail level, examples)
    • Watch mode defaults (debounce delay, auto-open web UI, extensions)
    • Export defaults (format, token budget)
    • Web UI defaults (port, auto-open browser)
    • Large file threshold override
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# ── Location ──────────────────────────────────────────────────────────────────

SETTINGS_DIR = Path.home() / ".codilay"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

# ── Provider metadata (label + env var name, for display only) ────────────────

PROVIDER_META = {
    "anthropic": {"label": "Anthropic (Claude)", "env_key": "ANTHROPIC_API_KEY"},
    "openai": {"label": "OpenAI", "env_key": "OPENAI_API_KEY"},
    "gemini": {"label": "Google Gemini", "env_key": "GEMINI_API_KEY"},
    "deepseek": {"label": "DeepSeek", "env_key": "DEEPSEEK_API_KEY"},
    "mistral": {"label": "Mistral AI", "env_key": "MISTRAL_API_KEY"},
    "groq": {"label": "Groq", "env_key": "GROQ_API_KEY"},
    "xai": {"label": "xAI (Grok)", "env_key": "XAI_API_KEY"},
    "llama": {"label": "Llama Cloud (Meta)", "env_key": "LLAMA_API_KEY"},
    "ollama": {"label": "Ollama (local)", "env_key": None},
    "custom": {"label": "Custom endpoint", "env_key": "CUSTOM_LLM_API_KEY"},
}

# Default models per provider
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "gemini": "gemini-2.0-flash",
    "deepseek": "deepseek-chat",
    "mistral": "mistral-large-latest",
    "groq": "llama-3.3-70b-versatile",
    "xai": "grok-2-latest",
    "llama": "Llama-4-Maverick-17B-128E",
    "ollama": "llama3.2",
    "custom": None,
}

# Preset model list per provider — {"id": model_id, "desc": description, "reasoning": bool}
# reasoning=True marks models that support extended thinking / reasoning mode
PROVIDER_MODELS = {
    "anthropic": [
        {"id": "claude-opus-4-6", "desc": "Most capable, highest cost", "reasoning": True},
        {"id": "claude-sonnet-4-6", "desc": "Balanced (recommended)", "reasoning": True},
        {"id": "claude-haiku-4-5-20251001", "desc": "Fastest, lowest cost", "reasoning": False},
        {"id": "claude-sonnet-4-20250514", "desc": "Previous generation Sonnet", "reasoning": False},
    ],
    "openai": [
        {"id": "gpt-4o", "desc": "Flagship multimodal", "reasoning": False},
        {"id": "gpt-4o-mini", "desc": "Fast, low cost", "reasoning": False},
        {"id": "o3", "desc": "Reasoning model", "reasoning": True},
        {"id": "o4-mini", "desc": "Reasoning, low cost", "reasoning": True},
    ],
    "gemini": [
        {"id": "gemini-2.0-flash", "desc": "Fast", "reasoning": False},
        {"id": "gemini-2.5-pro-preview-03-25", "desc": "Most capable", "reasoning": True},
        {"id": "gemini-2.0-flash-thinking-exp", "desc": "Reasoning", "reasoning": True},
    ],
    "deepseek": [
        {"id": "deepseek-chat", "desc": "Standard chat", "reasoning": False},
        {"id": "deepseek-reasoner", "desc": "Reasoning model", "reasoning": True},
    ],
    "mistral": [
        {"id": "mistral-large-latest", "desc": "Most capable", "reasoning": False},
        {"id": "mistral-small-latest", "desc": "Fast, low cost", "reasoning": False},
        {"id": "codestral-latest", "desc": "Code-optimized", "reasoning": False},
    ],
    "groq": [
        {"id": "llama-3.3-70b-versatile", "desc": "Large, versatile", "reasoning": False},
        {"id": "llama-3.1-8b-instant", "desc": "Fast, low cost", "reasoning": False},
        {"id": "gemma2-9b-it", "desc": "Google Gemma 2", "reasoning": False},
    ],
    "xai": [
        {"id": "grok-2-latest", "desc": "Most capable", "reasoning": False},
        {"id": "grok-3-mini-beta", "desc": "Reasoning, efficient", "reasoning": True},
    ],
    "llama": [
        {"id": "Llama-4-Maverick-17B-128E", "desc": "Most capable", "reasoning": False},
        {"id": "Llama-4-Scout-17B-16E", "desc": "Faster, efficient", "reasoning": False},
    ],
    "ollama": [],  # Dynamic — user enters model name
    "custom": [],  # Always custom
}


# ── Data class ────────────────────────────────────────────────────────────────


@dataclass
class Settings:
    """Global, persistent settings for CodiLay."""

    # API keys — keyed by provider name
    api_keys: Dict[str, str] = field(default_factory=dict)

    # Preferred defaults
    default_provider: str = "anthropic"
    default_model: Optional[str] = None  # None → provider default
    custom_base_url: Optional[str] = None  # for 'custom' provider

    # Behaviour
    verbose: bool = False
    triage_mode: str = "smart"  # smart | fast | none
    include_tests: bool = False
    max_tokens_per_call: int = 4096
    parallel: bool = True  # tier-based parallel processing
    max_workers: int = 4  # max concurrent workers per tier

    # Doc output location
    # "codilay"  → write to <project>/codilay/, gitignore chat/state (Scenario A)
    # "docs"     → write CODEBASE.md to <project>/docs/, codilay/ dir gitignored (Scenario B)
    # "local"    → gitignore everything, purely local tool (Scenario C)
    doc_output_location: str = "codilay"  # codilay | docs | local

    # Documentation style
    # response_style: how the LLM structures answers
    #   "code-first"       → lead with code references, then explanation
    #   "explanation-first"→ lead with prose summary, then code
    #   "balanced"         → interleaved (default)
    response_style: str = "balanced"  # balanced | code-first | explanation-first

    # detail_level: verbosity of generated section prose
    #   "terse"    → one-liners, minimum prose
    #   "standard" → concise paragraphs (default)
    #   "detailed" → thorough writeups with context
    detail_level: str = "standard"  # standard | terse | detailed

    # Whether to include code examples in doc sections
    include_examples: bool = True

    # Watch mode defaults
    watch_debounce_seconds: float = 2.0
    watch_auto_open_ui: bool = False
    # Extra file extensions to watch (in addition to code files)
    watch_extensions: List[str] = field(default_factory=list)

    # Export defaults
    export_default_format: str = "compact"  # compact | structured | narrative
    export_max_tokens: int = 100000

    # Custom export presets (user-defined)
    export_presets: Dict[str, Dict] = field(default_factory=dict)

    # Web UI defaults
    web_ui_port: int = 8765
    web_ui_auto_open_browser: bool = True

    # Large file handling — token threshold above which chunking kicks in.
    # None means use the per-project config / built-in default (6000).
    large_file_threshold: Optional[int] = None

    # Reasoning / extended thinking
    # reasoning_enabled: activates thinking mode for supported models
    # reasoning_budget_tokens: token budget for Anthropic extended thinking
    # reasoning_effort: effort level for OpenAI o-series (low | medium | high)
    # reasoning_apply_to: which operations get thinking (processing, planning, deep_agent)
    reasoning_enabled: bool = False
    reasoning_budget_tokens: int = 10000
    reasoning_effort: str = "medium"
    reasoning_apply_to: List[str] = field(default_factory=lambda: ["processing", "planning"])

    # Code annotation settings
    annotate_model: Optional[str] = None  # dedicated model for annotate (overrides config file model)
    annotate_use_config_model: bool = False  # if True, project codilay.config.json model applies to annotate
    annotate_require_git_clean: bool = True
    annotate_require_dry_run_first: bool = True
    annotate_auto_commit: bool = False
    annotate_commit_message: str = "docs: add CodiLay annotations"
    annotate_level: str = "docstrings"  # docstrings | inline | full
    annotate_skip_existing: bool = True
    annotate_skip_tests: bool = True
    annotate_skip_short_functions: bool = True
    annotate_short_function_threshold: int = 5
    annotate_confidence_threshold: float = 0.7
    annotate_review_mode: bool = False
    annotate_syntax_validation: bool = True

    # ── persistence ───────────────────────────────────────────────

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from disk, or return fresh defaults."""
        if not SETTINGS_FILE.exists():
            return cls()
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError):
            return cls()

    def save(self) -> None:
        """Persist settings to disk."""
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    # ── helpers ───────────────────────────────────────────────────

    def get_api_key(self, provider: str) -> Optional[str]:
        """Return stored key, falling back to the environment variable."""
        key = self.api_keys.get(provider)
        if key:
            return key
        env_key = PROVIDER_META.get(provider, {}).get("env_key")
        if env_key:
            return os.environ.get(env_key)
        return None

    def set_api_key(self, provider: str, key: str) -> None:
        """Store an API key persistently."""
        self.api_keys[provider] = key
        self.save()

    def remove_api_key(self, provider: str) -> None:
        """Remove a stored API key."""
        self.api_keys.pop(provider, None)
        self.save()

    def get_effective_model(self, provider: Optional[str] = None) -> Optional[str]:
        """Return the model in use for the given provider."""
        prov = provider or self.default_provider
        return self.default_model or DEFAULT_MODELS.get(prov)

    def has_provider_configured(self, provider: str) -> bool:
        """Check if a provider has the credentials it needs."""
        meta = PROVIDER_META.get(provider, {})
        if meta.get("env_key") is None:
            # No key required (e.g. Ollama local)
            return True
        return bool(self.get_api_key(provider))

    def inject_env_vars(self) -> None:
        """
        Push stored API keys into environment variables so that
        LLMClient (and the underlying SDKs) can find them without
        the user having to `export` anything.
        """
        for provider, key in self.api_keys.items():
            env_key = PROVIDER_META.get(provider, {}).get("env_key")
            if env_key and key:
                os.environ[env_key] = key
        # Custom base URL
        if self.custom_base_url:
            os.environ["CUSTOM_LLM_BASE_URL"] = self.custom_base_url

    @staticmethod
    def mask_key(key: str) -> str:
        """Return a masked version of an API key for display."""
        if not key or len(key) < 10:
            return "****"
        return key[:4] + "•" * (len(key) - 8) + key[-4:]

    def is_first_run(self) -> bool:
        """True when no settings file exists yet."""
        return not SETTINGS_FILE.exists()
