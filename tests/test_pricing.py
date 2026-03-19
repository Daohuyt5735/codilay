import pytest

from codilay.pricing import (
    MODEL_PRICING,
    _find_pricing,
    estimate_cost,
    format_cost,
)

# ── _find_pricing ─────────────────────────────────────────────────────────────


def test_exact_match():
    p = _find_pricing("gpt-4o")
    assert p is not None
    assert p["input"] == MODEL_PRICING["gpt-4o"]["input"]


def test_exact_match_case_insensitive():
    p = _find_pricing("GPT-4O")
    assert p is not None
    assert p["input"] == MODEL_PRICING["gpt-4o"]["input"]


def test_substring_match():
    # Full versioned model name — should resolve via substring
    p = _find_pricing("claude-sonnet-4-20250514")
    assert p is not None
    assert p["input"] == MODEL_PRICING["claude-sonnet-4"]["input"]


def test_substring_match_prefers_longest_key():
    # "claude-3-5-haiku" is longer than "claude-3" (not in table, but analogous)
    # "claude-haiku-4" vs "claude-3-5-haiku" — should pick the one that is actually in the string
    p = _find_pricing("claude-3-5-haiku-20251022")
    assert p is not None
    assert p["input"] == MODEL_PRICING["claude-3-5-haiku"]["input"]


def test_unknown_model_returns_none():
    assert _find_pricing("my-local-llama") is None


def test_empty_string_returns_none():
    assert _find_pricing("") is None


def test_none_equivalent_empty():
    # Function signature accepts str; empty string is the sentinel
    assert _find_pricing("   ") is None  # spaces don't match any key


# ── estimate_cost ─────────────────────────────────────────────────────────────


def test_estimate_cost_known_model():
    # gpt-4o: $2.5/M input, $10/M output
    # 1_000_000 input + 1_000_000 output = $12.50
    cost = estimate_cost("gpt-4o", 1_000_000, 1_000_000)
    assert abs(cost - 12.50) < 0.001


def test_estimate_cost_unknown_model_returns_zero():
    assert estimate_cost("ollama/llama3", 100_000, 50_000) == 0.0


def test_estimate_cost_zero_tokens():
    assert estimate_cost("gpt-4o", 0, 0) == 0.0


def test_estimate_cost_only_input():
    # claude-opus-4: $15/M input
    cost = estimate_cost("claude-opus-4", 1_000_000, 0)
    assert abs(cost - 15.0) < 0.001


def test_estimate_cost_only_output():
    # claude-opus-4: $75/M output
    cost = estimate_cost("claude-opus-4", 0, 1_000_000)
    assert abs(cost - 75.0) < 0.001


def test_estimate_cost_small_run():
    # 500 input + 200 output with gpt-4o-mini ($0.15/$0.6 per M)
    cost = estimate_cost("gpt-4o-mini", 500, 200)
    expected = (500 * 0.15 + 200 * 0.6) / 1_000_000
    assert abs(cost - expected) < 1e-9


# ── format_cost ───────────────────────────────────────────────────────────────


def test_format_cost_tiny():
    assert format_cost(0.0) == "<$0.001"
    assert format_cost(0.0005) == "<$0.001"
    assert format_cost(0.00099) == "<$0.001"


def test_format_cost_sub_dollar():
    assert format_cost(0.001) == "$0.001"
    assert format_cost(0.033) == "$0.033"
    assert format_cost(0.9999) == "$1.000"  # rounds up to $1.000 (< 1.0 branch)


def test_format_cost_dollar_plus():
    assert format_cost(1.0) == "$1.00"
    assert format_cost(12.5) == "$12.50"
    assert format_cost(100.0) == "$100.00"


def test_format_cost_boundary_exactly_001():
    # 0.001 is NOT < 0.001, so it hits the sub-dollar branch
    result = format_cost(0.001)
    assert result == "$0.001"
