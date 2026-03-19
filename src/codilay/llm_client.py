"""LLM client — supports Anthropic, OpenAI, and 8+ OpenAI-compatible providers."""

import json
import os
import re
import sys
import time
from typing import Any, Dict

import tiktoken

from codilay.pricing import estimate_cost

# ── Provider registry ─────────────────────────────────────────────────────────
# "sdk" is "anthropic" (native SDK) or "openai" (OpenAI SDK / compatible).

PROVIDER_CONFIGS: Dict[str, Dict[str, Any]] = {
    "anthropic": {
        "sdk": "anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-20250514",
        "label": "Anthropic",
    },
    "openai": {
        "sdk": "openai",
        "base_url": None,  # uses SDK default
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
        "label": "OpenAI",
    },
    "ollama": {
        "sdk": "openai",
        "base_url": "http://localhost:11434/v1",
        "env_key": None,  # no key for local
        "default_model": "llama3.2",
        "label": "Ollama (local)",
    },
    "gemini": {
        "sdk": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_key": "GEMINI_API_KEY",
        "default_model": "gemini-2.0-flash",
        "label": "Google Gemini",
    },
    "deepseek": {
        "sdk": "openai",
        "base_url": "https://api.deepseek.com",
        "env_key": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "label": "DeepSeek",
    },
    "mistral": {
        "sdk": "openai",
        "base_url": "https://api.mistral.ai/v1",
        "env_key": "MISTRAL_API_KEY",
        "default_model": "mistral-large-latest",
        "label": "Mistral AI",
    },
    "groq": {
        "sdk": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
        "label": "Groq",
    },
    "xai": {
        "sdk": "openai",
        "base_url": "https://api.x.ai/v1",
        "env_key": "XAI_API_KEY",
        "default_model": "grok-2-latest",
        "label": "xAI (Grok)",
    },
    "llama": {
        "sdk": "openai",
        "base_url": "https://api.llama.com/compat/v1/",
        "env_key": "LLAMA_API_KEY",
        "default_model": "Llama-4-Maverick-17B-128E",
        "label": "Llama Cloud (Meta)",
    },
    "custom": {
        "sdk": "openai",
        "base_url": None,  # MUST be supplied
        "env_key": "CUSTOM_LLM_API_KEY",
        "default_model": None,  # MUST be supplied
        "label": "Custom endpoint",
    },
}

ALL_PROVIDERS = list(PROVIDER_CONFIGS.keys())


# ── Rate-limit helpers ────────────────────────────────────────────────────────

_anthropic_rate_limit_error = None
_openai_rate_limit_error = None
_anthropic_auth_error = None
_openai_auth_error = None


def _get_rate_limit_errors():
    """Import provider-specific rate-limit error classes once."""
    global _anthropic_rate_limit_error, _openai_rate_limit_error
    if _anthropic_rate_limit_error is None:
        try:
            import anthropic

            _anthropic_rate_limit_error = anthropic.RateLimitError
        except ImportError:
            _anthropic_rate_limit_error = type(None)
    if _openai_rate_limit_error is None:
        try:
            import openai

            _openai_rate_limit_error = openai.RateLimitError
        except ImportError:
            _openai_rate_limit_error = type(None)
    return _anthropic_rate_limit_error, _openai_rate_limit_error


def _get_auth_errors():
    """Import provider-specific authentication error classes once."""
    global _anthropic_auth_error, _openai_auth_error
    if _anthropic_auth_error is None:
        try:
            import anthropic

            _anthropic_auth_error = anthropic.AuthenticationError
        except (ImportError, AttributeError):
            _anthropic_auth_error = type(None)
    if _openai_auth_error is None:
        try:
            import openai

            _openai_auth_error = openai.AuthenticationError
        except (ImportError, AttributeError):
            _openai_auth_error = type(None)
    return _anthropic_auth_error, _openai_auth_error


class AuthenticationError(Exception):
    """Raised when the API key is invalid or expired — user must fix it."""


def _extract_retry_after(exc) -> float:
    """Try to pull a retry-after value (seconds) from the API error response."""
    try:
        response = getattr(exc, "response", None)
        if response is not None:
            header = response.headers.get("retry-after")
            if header is not None:
                return max(float(header), 1.0)
    except Exception:
        pass
    return 0.0


# ── LLM Client ───────────────────────────────────────────────────────────────


class LLMClient:
    """Unified LLM client supporting Anthropic, OpenAI, and OpenAI-compatible providers."""

    RATE_LIMIT_MAX_RETRIES = 5
    RATE_LIMIT_DEFAULT_WAIT = 60.0

    def __init__(self, config):
        self.config = config
        self.provider = config.llm_provider
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.call_count = 0

        pcfg = PROVIDER_CONFIGS.get(self.provider)
        if pcfg is None:
            raise ValueError(f"Unknown provider '{self.provider}'. Supported: {', '.join(ALL_PROVIDERS)}")

        # Resolve model: explicit config → provider default
        self.model = config.llm_model or pcfg["default_model"]
        if not self.model:
            raise ValueError(
                f"No model specified for provider '{self.provider}'. Set it via --model or in codilay.config.json"
            )

        self.max_tokens = config.max_tokens_per_call
        self._sdk_type = pcfg["sdk"]  # "anthropic" or "openai"

        # Reasoning / extended thinking settings (applied when use_thinking=True)
        self.thinking_budget = getattr(config, "thinking_budget_tokens", None)
        self.reasoning_effort = getattr(config, "reasoning_effort", None)

        # ── Build SDK client ───────────────────────────────────────
        if self._sdk_type == "anthropic":
            self._init_anthropic(pcfg)
        else:
            self._init_openai_compat(pcfg, config)

        # Token counter (approximate — used for chunking, not billing)
        try:
            self.tokenizer = tiktoken.encoding_for_model("gpt-4")
        except Exception:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")

    # ── Anthropic init ─────────────────────────────────────────────

    def _init_anthropic(self, pcfg):
        try:
            import anthropic
        except ImportError:
            raise ImportError("The 'anthropic' package is required. Install: pip install anthropic")
        api_key = os.environ.get(pcfg["env_key"])
        if not api_key:
            raise ValueError(f"{pcfg['env_key']} not set. export {pcfg['env_key']}=your-key")
        self.client = anthropic.Anthropic(api_key=api_key)

    # ── OpenAI / compatible init ───────────────────────────────────

    def _init_openai_compat(self, pcfg, config):
        try:
            import openai
        except ImportError:
            raise ImportError("The 'openai' package is required for this provider. Install: pip install openai")

        # Resolve base URL: CLI/config override → provider default → env
        base_url = getattr(config, "llm_base_url", None) or pcfg.get("base_url")

        if not base_url and self.provider == "custom":
            base_url = os.environ.get("CUSTOM_LLM_BASE_URL")

        if not base_url and self.provider == "custom":
            raise ValueError(
                "Custom provider requires a base URL. Set via:\n"
                "  --base-url https://your-endpoint.com/v1\n"
                "  OR codilay.config.json → llm.baseUrl\n"
                "  OR env CUSTOM_LLM_BASE_URL"
            )

        # Resolve API key
        env_key = pcfg.get("env_key")
        if env_key:
            api_key = os.environ.get(env_key)
            if not api_key:
                raise ValueError(f"{env_key} not set. export {env_key}=your-key")
        else:
            # No key required (e.g. Ollama local)
            api_key = "not-needed"

        # Build client
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url

        self.client = openai.OpenAI(**client_kwargs)

    # ── Token counting ─────────────────────────────────────────────

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text))

    # ── Public entry point ─────────────────────────────────────────

    def call(
        self,
        system_prompt: str,
        user_prompt: str,
        retries: int = 3,
        json_mode: bool = True,
        use_thinking: bool = False,
    ) -> Dict[str, Any]:
        raw = ""
        for attempt in range(retries):
            try:
                raw = self._raw_call_with_rate_limit(
                    system_prompt, user_prompt, json_mode=json_mode, use_thinking=use_thinking
                )
                self.call_count += 1
                if not json_mode:
                    return {"answer": raw}
                return self._parse_json(raw)
            except json.JSONDecodeError:
                if attempt < retries - 1:
                    time.sleep(2**attempt)
                    continue
                return self._salvage_json(raw)
            except Exception:
                if attempt < retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise

    # ── Rate-limit wrapper ─────────────────────────────────────────

    def _raw_call_with_rate_limit(
        self, system_prompt: str, user_prompt: str, json_mode: bool = False, use_thinking: bool = False
    ) -> str:
        """Call _raw_call with automatic retry on 429 rate-limit errors.

        Auth errors (401) are converted to AuthenticationError immediately —
        they are not transient and must be fixed by the user.
        """
        anthropic_rl, openai_rl = _get_rate_limit_errors()
        anthropic_auth, openai_auth = _get_auth_errors()
        rate_limit_errors = (anthropic_rl, openai_rl)
        auth_errors = (anthropic_auth, openai_auth)

        for rate_attempt in range(self.RATE_LIMIT_MAX_RETRIES):
            try:
                return self._raw_call(system_prompt, user_prompt, json_mode=json_mode, use_thinking=use_thinking)
            except auth_errors as exc:
                # Auth errors are not retryable — surface them immediately
                raise AuthenticationError(
                    f"API authentication failed: {exc}\nCheck your API key (codilay keys set) and try again."
                ) from exc
            except rate_limit_errors as exc:
                if rate_attempt >= self.RATE_LIMIT_MAX_RETRIES - 1:
                    raise

                wait = _extract_retry_after(exc)
                if wait <= 0:
                    wait = self.RATE_LIMIT_DEFAULT_WAIT

                wait += rate_attempt * 5

                print(
                    f"\n⏳  Rate limit hit — waiting {wait:.0f}s before "
                    f"retry {rate_attempt + 2}/{self.RATE_LIMIT_MAX_RETRIES}  "
                    f"(Ctrl+C to abort)",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(wait)

        return self._raw_call(system_prompt, user_prompt, json_mode=json_mode, use_thinking=use_thinking)

    # ── Raw API call (routes to correct SDK) ───────────────────────

    def _raw_call(
        self, system_prompt: str, user_prompt: str, json_mode: bool = False, use_thinking: bool = False
    ) -> str:
        if self._sdk_type == "anthropic":
            return self._call_anthropic(system_prompt, user_prompt, use_thinking=use_thinking)
        else:
            return self._call_openai(system_prompt, user_prompt, json_mode=json_mode, use_thinking=use_thinking)

    def _call_anthropic(self, system_prompt: str, user_prompt: str, use_thinking: bool = False) -> str:
        # Anthropic doesn't have a specific 'json_mode' toggle like OpenAI,
        # it relies on the prompt.
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "timeout": 60.0,
        }

        if use_thinking and self.thinking_budget:
            # Extended thinking requires betas header and budget_tokens
            # Temperature must not be set (defaults to 1 when thinking is active)
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": self.thinking_budget}
            kwargs["betas"] = ["interleaved-thinking-2025-05-14"]

        response = self.client.messages.create(**kwargs)
        self.total_input_tokens += response.usage.input_tokens
        self.total_output_tokens += response.usage.output_tokens

        # Extract text-type content blocks only (skip thinking blocks)
        text_parts = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
        return "\n".join(text_parts) if text_parts else ""

    def _call_openai(
        self, system_prompt: str, user_prompt: str, json_mode: bool = False, use_thinking: bool = False
    ) -> str:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "timeout": 60.0,
        }

        # o-series reasoning models use reasoning_effort + max_completion_tokens
        effort = self.reasoning_effort if use_thinking else None
        if effort:
            kwargs["reasoning_effort"] = effort
            kwargs["max_completion_tokens"] = self.max_tokens
        else:
            kwargs["max_tokens"] = self.max_tokens

        if json_mode:
            try:
                response = self.client.chat.completions.create(
                    **kwargs,
                    response_format={"type": "json_object"},
                )
            except Exception as e:
                err_msg = str(e).lower()
                if "response_format" in err_msg or "json_object" in err_msg:
                    response = self.client.chat.completions.create(**kwargs)
                else:
                    raise
        else:
            response = self.client.chat.completions.create(**kwargs)

        usage = response.usage
        if usage:
            self.total_input_tokens += getattr(usage, "prompt_tokens", 0) or 0
            self.total_output_tokens += getattr(usage, "completion_tokens", 0) or 0
        return response.choices[0].message.content

    # ── JSON parsing ───────────────────────────────────────────────

    def _parse_json(self, text: str) -> Dict[str, Any]:
        # Strip thinking blocks which often contain invalid JSON or brackets
        pattern = r"(?is)<(?:think|thinking|thought|reasoning)>.*?</(?:think|thinking|thought|reasoning)>"
        text = re.sub(pattern, "", text).strip()

        # Handle markdown fences that might not be at the very start/end
        text = text.strip()

        # More robust fence stripping
        if "```json" in text:
            start = text.find("```json") + 7
            end = text.rfind("```")
            if end > start:
                text = text[start:end]
        elif "```" in text:
            start = text.find("```") + 3
            end = text.rfind("```")
            if end > start:
                text = text[start:end]

        text = text.strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            if "Extra data" in str(e):
                try:
                    parsed = json.loads(text[: e.pos].strip())
                except Exception:
                    raise e
            else:
                raise e

        if isinstance(parsed, dict):
            return parsed
        elif isinstance(parsed, list):
            return {"error": "LLM returned a list instead of an object", "raw_list": parsed}
        else:
            return {"error": "LLM returned non-object JSON", "raw_value": parsed}

    def _salvage_json(self, text: str) -> Dict[str, Any]:
        pattern = r"(?is)<(?:think|thinking|thought|reasoning)>.*?</(?:think|thinking|thought|reasoning)>"
        text = re.sub(pattern, "", text).strip()
        text = text.strip()

        brace_starts = [m.start() for m in re.finditer(r"\{", text)]
        if not brace_starts:
            return {"error": "Failed to parse LLM response (no start brace)", "raw_response": text[:1000]}

        candidates_parsed = []

        # Parse starting from every brace. Save valid outputs along with string block length.
        for start_idx in brace_starts:
            candidate = text[start_idx:]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    candidates_parsed.append((len(candidate), parsed))
                    continue
            except json.JSONDecodeError as e:
                # Strategy 2: Handle extra data after valid object
                if "Extra data" in str(e):
                    try:
                        valid_str = candidate[: e.pos].strip()
                        parsed = json.loads(valid_str)
                        if isinstance(parsed, dict):
                            candidates_parsed.append((len(valid_str), parsed))
                            continue
                    except Exception:
                        pass

                # Strategy 3: Truncated JSON repair
                for suffix in ["}", '"', '"}', '"}]}', '"}}', "}}", "]}", "]}"]:
                    try:
                        valid_str = candidate + suffix
                        parsed = json.loads(valid_str)
                        if isinstance(parsed, dict):
                            parsed["_repaired"] = True
                            candidates_parsed.append((len(valid_str), parsed))
                            break
                    except Exception:
                        continue

        if candidates_parsed:
            # Sort by the length of the matching JSON string to prefer the largest top-level object
            candidates_parsed.sort(key=lambda x: x[0], reverse=True)
            return candidates_parsed[0][1]

        return {"error": "Failed to parse LLM response", "raw_response": text[:1000]}

    def get_usage_stats(self) -> Dict[str, Any]:
        cost = estimate_cost(self.model, self.total_input_tokens, self.total_output_tokens)
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_calls": self.call_count,
            "estimated_cost_usd": cost,
        }
