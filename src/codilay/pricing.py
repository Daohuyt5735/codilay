"""Model pricing registry for cost estimation and tracking.

Prices are in USD per million tokens (input / output).
The registry is intentionally a plain dict so it can be overridden
by the user in codilay.config.json under "pricing" without a release.

Lookup is fuzzy: "claude-sonnet-4-20250514" matches any model string
that contains "sonnet-4" or starts with "claude-sonnet-4".
"""

from typing import Optional

# ── Pricing table (USD per million tokens) ────────────────────────
# Updated 2026-03 — check provider pricing pages for current rates.

MODEL_PRICING: dict = {
    # Anthropic
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-haiku-4": {"input": 0.8, "output": 4.0},
    "claude-3-5-sonnet": {"input": 3.0, "output": 15.0},
    "claude-3-5-haiku": {"input": 0.8, "output": 4.0},
    "claude-3-opus": {"input": 15.0, "output": 75.0},
    # OpenAI
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    "gpt-4-turbo": {"input": 10.0, "output": 30.0},
    "o1": {"input": 15.0, "output": 60.0},
    "o3-mini": {"input": 1.1, "output": 4.4},
    # Google
    "gemini-2.0-flash": {"input": 0.1, "output": 0.4},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.0},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.3},
    # DeepSeek
    "deepseek-chat": {"input": 0.27, "output": 1.1},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    # Mistral
    "mistral-large": {"input": 3.0, "output": 9.0},
    "mistral-small": {"input": 0.2, "output": 0.6},
    # Groq (fast inference — prices similar to underlying model)
    "llama-3.3-70b": {"input": 0.59, "output": 0.79},
    "llama-3.1-8b": {"input": 0.05, "output": 0.08},
    # xAI
    "grok-2": {"input": 2.0, "output": 10.0},
}


def _find_pricing(model: str) -> Optional[dict]:
    """Fuzzy-match a model string against the pricing table."""
    if not model:
        return None
    model_lower = model.lower()
    # Exact match first
    if model_lower in MODEL_PRICING:
        return MODEL_PRICING[model_lower]
    # Substring match — longest key that appears in the model string wins
    matches = [(k, v) for k, v in MODEL_PRICING.items() if k in model_lower]
    if matches:
        return max(matches, key=lambda x: len(x[0]))[1]
    return None


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated USD cost. Returns 0.0 if model is unknown."""
    pricing = _find_pricing(model)
    if not pricing:
        return 0.0
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


def format_cost(usd: float) -> str:
    """Format a dollar amount for display."""
    if usd < 0.001:
        return "<$0.001"
    if usd < 1.0:
        return f"${usd:.3f}"
    return f"${usd:.2f}"
