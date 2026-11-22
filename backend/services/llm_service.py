"""
LLM Service – routes chat completions to Ollama (default, local) or xAI (opt-in).

Configuration is read from environment variables at call time so runtime changes
and test fixtures take effect without re-importing the module:
  - LLM_PROVIDER: "ollama" (default) | "xai"
  - OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_NUM_CTX, OLLAMA_MAX_TOKENS
  - XAI_API_KEY (required when LLM_PROVIDER=xai; never logged or echoed)
  - XAI_MODEL (default "grok-4.3" when LLM_PROVIDER=xai)
  - XAI_MAX_TOKENS (private-testing output cap, default 600, max 1200)
"""
from __future__ import annotations

import json
import os
import re
from typing import AsyncGenerator

import httpx

XAI_CHAT_COMPLETIONS_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_XAI_MODEL = "grok-4.3"
DEFAULT_OLLAMA_MODEL = "qwen2.5:14b"
DEFAULT_XAI_MAX_TOKENS = 600
HARD_CAP_XAI_MAX_TOKENS = 1200

SUPPORTED_LLM_PROVIDERS = ("ollama", "xai")

_SECRET_PATTERNS = (
    re.compile(r"xai-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
)


def _redact_secrets(text: str, extra_secret: str = "") -> str:
    """Scrub API keys and bearer tokens from any diagnostic string."""
    if not text:
        return ""
    cleaned = str(text)
    if extra_secret and len(extra_secret) >= 4:
        cleaned = cleaned.replace(extra_secret, "[REDACTED]")
    env_key = os.environ.get("XAI_API_KEY", "").strip()
    if env_key and len(env_key) >= 4:
        cleaned = cleaned.replace(env_key, "[REDACTED]")
    for pat in _SECRET_PATTERNS:
        cleaned = pat.sub("[REDACTED]", cleaned)
    return cleaned


def get_llm_provider() -> str:
    """Return normalized LLM provider from env ('ollama' by default)."""
    raw = os.environ.get("LLM_PROVIDER", "").strip().lower()
    return raw if raw else "ollama"


def _require_valid_provider() -> str:
    provider = get_llm_provider()
    if provider not in SUPPORTED_LLM_PROVIDERS:
        raise RuntimeError(
            f"Unsupported LLM_PROVIDER '{provider}'. Supported providers: ollama, xai."
        )
    return provider


def _get_validated_xai_key() -> str:
    """Read and validate XAI_API_KEY from environment without exposing its value."""
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("XAI_API_KEY is missing. Set XAI_API_KEY when LLM_PROVIDER=xai.")
    if any(ch.isspace() for ch in key) or len(key) < 8:
        raise RuntimeError("XAI_API_KEY is invalid (malformed format).")
    lowered = key.lower()
    if lowered.startswith(("your_", "<", "changeme", "replace_me", "xxx")):
        raise RuntimeError("XAI_API_KEY is set to a placeholder value.")
    return key


def _base_url() -> str:
    """Resolve the Ollama base URL from env on every call."""
    return os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")


def get_default_model() -> str:
    """Resolve the default model ID for the active provider."""
    provider = _require_valid_provider()
    if provider == "xai":
        return os.environ.get("XAI_MODEL", "").strip() or DEFAULT_XAI_MODEL
    return os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


def _num_ctx() -> int:
    """Ollama context window size."""
    try:
        return int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
    except ValueError:
        return 8192


def _max_tokens() -> int:
    """Ollama default reply length (num_predict)."""
    try:
        return int(os.environ.get("OLLAMA_MAX_TOKENS", "800"))
    except ValueError:
        return 800


def _xai_max_tokens_cap() -> int:
    """Private-beta output token cap for xAI completions."""
    raw = os.environ.get("XAI_MAX_TOKENS", str(DEFAULT_XAI_MAX_TOKENS)).strip()
    try:
        parsed = int(raw)
    except ValueError:
        parsed = DEFAULT_XAI_MAX_TOKENS
    return max(1, min(parsed, HARD_CAP_XAI_MAX_TOKENS))


def _resolve_xai_max_tokens(requested: int | None) -> int:
    cap = _xai_max_tokens_cap()
    if requested is None:
        return cap
    return max(1, min(int(requested), cap))


def _keep_alive() -> str:
    return os.environ.get("OLLAMA_KEEP_ALIVE", "30m")


def _repeat_penalty() -> float:
    try:
        return float(os.environ.get("OLLAMA_REPEAT_PENALTY", "1.15"))
    except ValueError:
        return 1.15


def _xai_timeout() -> httpx.Timeout:
    try:
        secs = float(os.environ.get("XAI_TIMEOUT_SECONDS", "90"))
    except ValueError:
        secs = 90.0
    secs = max(5.0, min(secs, 300.0))
    return httpx.Timeout(secs, connect=min(15.0, secs))


def _normalize_openai_messages(messages: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for msg in messages:
        role = str(msg.get("role") or "user").strip()
        if role not in ("system", "user", "assistant"):
            role = "user"
        content = msg.get("content", "")
        normalized.append({"role": role, "content": "" if content is None else str(content)})
    return normalized


def _build_xai_payload(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int | None,
    stream: bool,
) -> dict:
    """Map supported chat completion options intentionally for xAI."""
    clamped_temp = max(0.0, min(2.0, float(temperature)))
    payload = {
        "model": model,
        "messages": _normalize_openai_messages(messages),
        "stream": stream,
        "temperature": clamped_temp,
        "top_p": 0.95,
        "max_tokens": _resolve_xai_max_tokens(max_tokens),
    }
    if model in {"grok-4.3", "grok-4.3-latest"}:
        payload["reasoning_effort"] = "none"
    return payload


def _raise_xai_http_error(status_code: int, model: str) -> None:
    """Raise a useful non-secret error for xAI HTTP failures without leaking bodies."""
    if status_code in (401, 403):
        raise RuntimeError(
            f"xAI authentication failed (HTTP {status_code}). Check XAI_API_KEY configuration."
        )
    if status_code == 404:
        raise RuntimeError(
            f"xAI model '{model}' or endpoint not found (HTTP 404). Check XAI_MODEL."
        )
    if status_code == 429:
        raise RuntimeError(
            "xAI rate limit or credit quota exceeded (HTTP 429). Please wait and retry."
        )
    if status_code in (400, 422):
        raise RuntimeError(
            f"xAI rejected the completion request (HTTP {status_code}). Check model and prompt parameters."
        )
    if status_code >= 500:
        raise RuntimeError(f"xAI service is temporarily unavailable (HTTP {status_code}).")
    raise RuntimeError(f"xAI request failed (HTTP {status_code}).")


def _extract_choice_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


async def _xai_chat_completion_stream(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int | None,
) -> AsyncGenerator[str, None]:
    api_key = _get_validated_xai_key()
    payload = _build_xai_payload(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    try:
        async with httpx.AsyncClient(timeout=_xai_timeout()) as client:
            async with client.stream(
                "POST",
                XAI_CHAT_COMPLETIONS_URL,
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    _raise_xai_http_error(response.status_code, model)

                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue

                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    if data_str == "[DONE]":
                        break

                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(event, dict):
                        continue
                    if event.get("error"):
                        raise RuntimeError("xAI stream returned an upstream error.")

                    choices = event.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    first = choices[0] if isinstance(choices[0], dict) else {}
                    delta = first.get("delta")
                    if isinstance(delta, dict):
                        chunk = _extract_choice_text(delta.get("content"))
                        if chunk:
                            yield chunk
    except RuntimeError:
        raise
    except httpx.TimeoutException:
        raise RuntimeError("xAI request timed out. Please try again.")
    except httpx.RequestError:
        raise RuntimeError("Cannot connect to xAI API. Check network connectivity.")
    except Exception as exc:
        safe = _redact_secrets(type(exc).__name__, api_key)
        raise RuntimeError(f"xAI streaming failed ({safe}).")


async def _xai_chat_completion(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int | None,
) -> str:
    api_key = _get_validated_xai_key()
    payload = _build_xai_payload(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_xai_timeout()) as client:
            resp = await client.post(
                XAI_CHAT_COMPLETIONS_URL,
                headers=headers,
                json=payload,
            )
            if resp.status_code >= 400:
                _raise_xai_http_error(resp.status_code, model)

            try:
                data = resp.json()
            except ValueError:
                raise RuntimeError("xAI returned a malformed JSON response.")

            if not isinstance(data, dict):
                raise RuntimeError("xAI returned an unexpected response structure.")
            if data.get("error"):
                raise RuntimeError("xAI returned an upstream error.")

            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise RuntimeError("xAI response contained no completion choices.")

            message = choices[0].get("message") or {}
            if not isinstance(message, dict):
                return ""
            return _extract_choice_text(message.get("content"))
    except RuntimeError:
        raise
    except httpx.TimeoutException:
        raise RuntimeError("xAI request timed out. Please try again.")
    except httpx.RequestError:
        raise RuntimeError("Cannot connect to xAI API. Check network connectivity.")
    except Exception as exc:
        safe = _redact_secrets(type(exc).__name__, api_key)
        raise RuntimeError(f"xAI completion failed ({safe}).")


async def chat_completion_stream(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.95,
    max_tokens: int | None = None,
) -> AsyncGenerator[str, None]:
    """Stream chat completion from the configured provider. Yields text chunks."""
    provider = _require_valid_provider()
    resolved_model = model or get_default_model()

    if provider == "xai":
        async for chunk in _xai_chat_completion_stream(
            messages=messages,
            model=resolved_model,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            yield chunk
        return

    payload = {
        "model": resolved_model,
        "messages": messages,
        "stream": True,
        "keep_alive": _keep_alive(),
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens if max_tokens is not None else _max_tokens(),
            "num_ctx": _num_ctx(),
            "repeat_penalty": _repeat_penalty(),
        },
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            async with client.stream(
                "POST",
                f"{_base_url()}/api/chat",
                json=payload,
            ) as response:
                if response.status_code == 404:
                    raise RuntimeError(
                        f"Model '{resolved_model}' not found. It may still be downloading. "
                        f"Check progress with: ollama pull {resolved_model}"
                    )
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict) and data.get("error"):
                        raise RuntimeError(f"Ollama error: {data['error']}")
                    if "message" in data and "content" in data["message"]:
                        chunk = data["message"]["content"]
                        if chunk:
                            yield chunk
                    if data.get("done", False):
                        break
    except httpx.ConnectError:
        raise RuntimeError(
            "Cannot connect to Ollama. Make sure Ollama is running (ollama serve)."
        )


async def chat_completion(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.85,
    max_tokens: int | None = None,
) -> str:
    """Non-streaming chat completion. Returns full response text."""
    provider = _require_valid_provider()
    resolved_model = model or get_default_model()

    if provider == "xai":
        return await _xai_chat_completion(
            messages=messages,
            model=resolved_model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    payload = {
        "model": resolved_model,
        "messages": messages,
        "stream": False,
        "keep_alive": _keep_alive(),
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens if max_tokens is not None else _max_tokens(),
            "num_ctx": _num_ctx(),
            "repeat_penalty": _repeat_penalty(),
        },
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            resp = await client.post(
                f"{_base_url()}/api/chat",
                json=payload,
            )
            if resp.status_code == 404:
                raise RuntimeError(
                    f"Model '{resolved_model}' not found. It may still be downloading. "
                    f"Check progress with: ollama pull {resolved_model}"
                )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")
    except httpx.ConnectError:
        raise RuntimeError(
            "Cannot connect to Ollama. Make sure Ollama is running (ollama serve)."
        )


async def check_ollama_health() -> bool:
    """Check if local Ollama server is running (free local check)."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{_base_url()}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False


async def list_models() -> list[str]:
    """List available models for the active provider without paid probes."""
    provider = get_llm_provider()
    if provider == "xai":
        try:
            _get_validated_xai_key()
            return [os.environ.get("XAI_MODEL", "").strip() or DEFAULT_XAI_MODEL]
        except RuntimeError:
            return []
    if provider != "ollama":
        return []

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{_base_url()}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


async def get_llm_health() -> dict:
    """
    Return honest LLM provider health without making any paid upstream probe.
    """
    provider = get_llm_provider()
    if provider not in SUPPORTED_LLM_PROVIDERS:
        return {
            "provider": provider,
            "healthy": False,
            "status": "invalid_provider",
            "ollama": "disconnected",
            "models": [],
            "detail": f"Unsupported LLM_PROVIDER '{provider}'. Expected 'ollama' or 'xai'.",
        }

    if provider == "xai":
        model = os.environ.get("XAI_MODEL", "").strip() or DEFAULT_XAI_MODEL
        try:
            _get_validated_xai_key()
            return {
                "provider": "xai",
                "healthy": True,
                "status": "configured",
                "ollama": "not_selected",
                "models": [model],
                "detail": f"xAI configured ({model}, unprobed)",
            }
        except RuntimeError as err:
            return {
                "provider": "xai",
                "healthy": False,
                "status": "misconfigured",
                "ollama": "not_selected",
                "models": [],
                "detail": _redact_secrets(str(err)),
            }

    ollama_ok = await check_ollama_health()
    models = await list_models() if ollama_ok else []
    return {
        "provider": "ollama",
        "healthy": ollama_ok,
        "status": "connected" if ollama_ok else "disconnected",
        "ollama": "connected" if ollama_ok else "disconnected",
        "models": models,
        "detail": None if ollama_ok else "Ollama is unreachable",
    }
