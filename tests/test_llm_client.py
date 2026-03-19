import os
from unittest.mock import MagicMock, patch

import pytest

from codilay.config import CodiLayConfig
from codilay.llm_client import LLMClient


@pytest.fixture
def mock_config():
    config = CodiLayConfig()
    config.llm_provider = "openai"
    config.llm_model = "gpt-4o"
    return config


@patch("openai.OpenAI")
def test_llm_client_openai_call(mock_openai, mock_config):
    os.environ["OPENAI_API_KEY"] = "test-key"
    client = LLMClient(mock_config)

    # Mock response
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"answer": "hello"}'
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 5
    mock_openai.return_value.chat.completions.create.return_value = mock_response

    result = client.call("sys", "user")
    assert result == {"answer": "hello"}
    assert client.total_input_tokens == 10
    assert client.total_output_tokens == 5


@patch("anthropic.Anthropic")
def test_llm_client_anthropic_call(mock_anthropic, mock_config):
    mock_config.llm_provider = "anthropic"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    client = LLMClient(mock_config)

    # Mock response — content blocks must have type="text" for the extractor
    mock_block = MagicMock()
    mock_block.type = "text"
    mock_block.text = '{"answer": "hi"}'
    mock_response = MagicMock()
    mock_response.content = [mock_block]
    mock_response.usage.input_tokens = 20
    mock_response.usage.output_tokens = 10
    mock_anthropic.return_value.messages.create.return_value = mock_response

    result = client.call("sys", "user")
    assert result == {"answer": "hi"}
    assert client.total_input_tokens == 20
    assert client.total_output_tokens == 10


@patch("anthropic.Anthropic")
def test_llm_client_anthropic_skips_thinking_blocks(mock_anthropic, mock_config):
    """Content blocks of type 'thinking' must be ignored — only 'text' blocks returned."""
    mock_config.llm_provider = "anthropic"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    client = LLMClient(mock_config)

    thinking_block = MagicMock()
    thinking_block.type = "thinking"
    thinking_block.thinking = "let me reason..."

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = '{"result": "done"}'

    mock_response = MagicMock()
    mock_response.content = [thinking_block, text_block]
    mock_response.usage.input_tokens = 30
    mock_response.usage.output_tokens = 15
    mock_anthropic.return_value.messages.create.return_value = mock_response

    result = client.call("sys", "user")
    assert result == {"result": "done"}


@patch("anthropic.Anthropic")
def test_llm_client_anthropic_thinking_params_sent(mock_anthropic, mock_config):
    """When use_thinking=True and a budget is set, thinking params go to the API."""
    mock_config.llm_provider = "anthropic"
    mock_config.thinking_budget_tokens = 5000
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    client = LLMClient(mock_config)
    assert client.thinking_budget == 5000

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = '{"ok": true}'
    mock_response = MagicMock()
    mock_response.content = [text_block]
    mock_response.usage.input_tokens = 10
    mock_response.usage.output_tokens = 5
    mock_anthropic.return_value.messages.create.return_value = mock_response

    client.call("sys", "user", use_thinking=True)

    call_kwargs = mock_anthropic.return_value.messages.create.call_args.kwargs
    assert call_kwargs["thinking"] == {"type": "enabled", "budget_tokens": 5000}
    assert "interleaved-thinking-2025-05-14" in call_kwargs["betas"]


@patch("anthropic.Anthropic")
def test_llm_client_anthropic_no_thinking_params_when_disabled(mock_anthropic, mock_config):
    """Without use_thinking, thinking params must NOT appear in the API call."""
    mock_config.llm_provider = "anthropic"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    client = LLMClient(mock_config)

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = '{"ok": true}'
    mock_response = MagicMock()
    mock_response.content = [text_block]
    mock_response.usage.input_tokens = 10
    mock_response.usage.output_tokens = 5
    mock_anthropic.return_value.messages.create.return_value = mock_response

    client.call("sys", "user", use_thinking=False)

    call_kwargs = mock_anthropic.return_value.messages.create.call_args.kwargs
    assert "thinking" not in call_kwargs
    assert "betas" not in call_kwargs


@patch("openai.OpenAI")
def test_llm_client_openai_reasoning_effort_sent(mock_openai, mock_config):
    """When use_thinking=True and reasoning_effort is set, it goes to the OpenAI call."""
    mock_config.reasoning_effort = "high"
    os.environ["OPENAI_API_KEY"] = "test-key"
    client = LLMClient(mock_config)
    assert client.reasoning_effort == "high"

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"ok": true}'
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 5
    mock_openai.return_value.chat.completions.create.return_value = mock_response

    client.call("sys", "user", use_thinking=True)

    call_kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert call_kwargs.get("reasoning_effort") == "high"
    assert "max_completion_tokens" in call_kwargs
    assert "max_tokens" not in call_kwargs


@patch("openai.OpenAI")
def test_llm_client_openai_no_reasoning_effort_without_flag(mock_openai, mock_config):
    """Without use_thinking, reasoning_effort must NOT appear in the OpenAI call."""
    mock_config.reasoning_effort = "high"
    os.environ["OPENAI_API_KEY"] = "test-key"
    client = LLMClient(mock_config)

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"ok": true}'
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 5
    mock_openai.return_value.chat.completions.create.return_value = mock_response

    client.call("sys", "user", use_thinking=False)

    call_kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert "reasoning_effort" not in call_kwargs
    assert "max_tokens" in call_kwargs


def test_llm_client_parse_json(mock_config):
    with patch("openai.OpenAI"):
        client = LLMClient(mock_config)

        # Test markdown stripping
        res = client._parse_json('```json\n{"id": 1}\n```')
        assert res == {"id": 1}

        # Test simple parsing
        res = client._parse_json('{"id": 2}')
        assert res == {"id": 2}


def test_llm_client_salvage_json(mock_config):
    with patch("openai.OpenAI"):
        client = LLMClient(mock_config)

        # Test salvage
        res = client._salvage_json('Here is the json: {"foo": "bar"} hope you like it')
        assert res == {"foo": "bar"}

        # Test salvage failure
        res = client._salvage_json("no json here")
        assert "error" in res


# ── AuthenticationError ───────────────────────────────────────────────────────


@patch("anthropic.Anthropic")
def test_llm_client_anthropic_auth_error_raises(mock_anthropic, mock_config):
    """An Anthropic AuthenticationError should be re-raised as codilay.llm_client.AuthenticationError."""
    import anthropic as anthropic_lib

    from codilay.llm_client import AuthenticationError

    mock_config.llm_provider = "anthropic"
    os.environ["ANTHROPIC_API_KEY"] = "bad-key"
    client = LLMClient(mock_config)

    mock_response = MagicMock()
    mock_response.request = MagicMock()
    mock_anthropic.return_value.messages.create.side_effect = anthropic_lib.AuthenticationError(
        message="invalid api key",
        response=mock_response,
        body=None,
    )

    with pytest.raises(AuthenticationError):
        client.call("sys", "user")


@patch("openai.OpenAI")
def test_llm_client_openai_auth_error_raises(mock_openai, mock_config):
    """An OpenAI AuthenticationError should be re-raised as codilay.llm_client.AuthenticationError."""
    import openai as openai_lib

    from codilay.llm_client import AuthenticationError

    os.environ["OPENAI_API_KEY"] = "bad-key"
    client = LLMClient(mock_config)

    mock_openai.return_value.chat.completions.create.side_effect = openai_lib.AuthenticationError(
        message="invalid api key",
        response=MagicMock(status_code=401, headers={}),
        body=None,
    )

    with pytest.raises(AuthenticationError):
        client.call("sys", "user")


# ── get_usage_stats includes cost ─────────────────────────────────────────────


@patch("openai.OpenAI")
def test_get_usage_stats_includes_estimated_cost(mock_openai, mock_config):
    """get_usage_stats() must include estimated_cost_usd when model is known."""
    os.environ["OPENAI_API_KEY"] = "test-key"
    client = LLMClient(mock_config)

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"ok": true}'
    mock_response.usage.prompt_tokens = 1_000_000
    mock_response.usage.completion_tokens = 1_000_000
    mock_openai.return_value.chat.completions.create.return_value = mock_response

    client.call("sys", "user")
    stats = client.get_usage_stats()

    assert "estimated_cost_usd" in stats
    # gpt-4o: $2.5/M input + $10/M output = $12.50
    assert abs(stats["estimated_cost_usd"] - 12.50) < 0.01


@patch("anthropic.Anthropic")
def test_get_usage_stats_cost_zero_for_unknown_model(mock_anthropic, mock_config):
    """estimated_cost_usd should be 0.0 for models not in the pricing table."""
    mock_config.llm_provider = "anthropic"
    mock_config.llm_model = "my-custom-local-model"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    client = LLMClient(mock_config)

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = '{"ok": true}'
    mock_response = MagicMock()
    mock_response.content = [text_block]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_anthropic.return_value.messages.create.return_value = mock_response

    client.call("sys", "user")
    stats = client.get_usage_stats()

    assert stats["estimated_cost_usd"] == 0.0
