"""The language model behind the chat, reached through any OpenAI-compatible API.

The default is **OpenRouter** with NVIDIA's Nemotron 3 Ultra (the free route),
because that is what this project is configured for. NVIDIA's own API and any
self-hosted OpenAI-compatible server (vLLM, Ollama, LM Studio...) work too.

Configuration (environment). The key is looked for under these names, in order:
``OPENROUTER_API_KEY``, ``AI_API_KEY``, ``NVIDIA_API_KEY``, ``NEMOTRON_API_KEY``.
An OpenRouter key (``sk-or-...``) is recognised wherever it is stored.

* ``AI_PROVIDER``     ``openrouter`` | ``nvidia`` | ``custom``. Normally inferred.
* ``AI_MODEL``        model id. Default for OpenRouter:
                      ``nvidia/nemotron-3-ultra-550b-a55b:free``.
                      (``NEMOTRON_MODEL`` is still read, for older setups.)
* ``AI_BASE_URL``     override the API address (``NVIDIA_BASE_URL`` still read).
* ``AI_VISION_MODEL`` a model that accepts images, for "sketch to drawing".
* ``AI_REASONING``    ``off`` | ``low`` | ``medium`` | ``high`` (OpenRouter's
                      reasoning effort; default ``low`` keeps replies quick).
* ``AI_MAX_TOKENS``   per reply (default 4096; reasoning models need room).
* ``AI_EXTRA_BODY``   JSON merged into each request (``NVIDIA_EXTRA_BODY`` too).
* ``AI_TIMEOUT``      seconds per call (default 120).

Only the standard library is used, so the converter's requirements don't grow.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

PROVIDERS = {
    "openrouter": {
        "label": "OpenRouter",
        "base": "https://openrouter.ai/api/v1",
        "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "vision": "nvidia/nemotron-nano-12b-v2-vl:free",
    },
    "nvidia": {
        "label": "NVIDIA",
        "base": "https://integrate.api.nvidia.com/v1",
        "model": "nvidia/nemotron-3-ultra-550b-a55b",
        "vision": "nvidia/nemotron-nano-12b-v2-vl",
    },
    "custom": {"label": "Custom", "base": "http://localhost:8001/v1", "model": "local-model", "vision": ""},
    # Self-hosted, OpenAI-compatible servers: drawings never leave your network. No key needed.
    "ollama": {"label": "Ollama (local)", "base": "http://localhost:11434/v1", "model": "llama3.1", "vision": "llava"},
    "lmstudio": {"label": "LM Studio (local)", "base": "http://localhost:1234/v1", "model": "local-model", "vision": ""},
    "vllm": {"label": "vLLM (self-hosted)", "base": "http://localhost:8001/v1", "model": "nvidia/nemotron-3-ultra-550b-a55b", "vision": ""},
}
LOCAL_PROVIDERS = ("ollama", "lmstudio", "vllm")
KEY_NAMES = ("OPENROUTER_API_KEY", "AI_API_KEY", "NVIDIA_API_KEY", "NEMOTRON_API_KEY")

# Kept for imports elsewhere and older configuration notes.
DEFAULT_BASE_URL = PROVIDERS["nvidia"]["base"]
DEFAULT_MODEL = PROVIDERS["openrouter"]["model"]


class LLMError(Exception):
    """A failure with a sentence worth showing to the person."""


class ChatModel(Protocol):
    name: str

    def complete(self, messages: list[dict], max_tokens: int = 4096, on_delta: Optional[Callable[[str, str], None]] = None) -> str: ...


def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return ""


@dataclass
class OpenAICompatModel:
    """Any OpenAI-style ``/chat/completions`` endpoint."""

    api_key: str
    base_url: str
    name: str
    provider: str = "openrouter"
    extra: dict = field(default_factory=dict)
    reasoning: str = "low"
    max_tokens: int = 4096
    timeout: float = 120.0

    @property
    def label(self) -> str:
        return PROVIDERS.get(self.provider, PROVIDERS["custom"])["label"]

    def _body(self, messages: list[dict], max_tokens: int, stream: bool) -> dict:
        body: dict = {
            "model": self.name,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if self.provider == "openrouter":
            if self.reasoning == "off":
                body["reasoning"] = {"enabled": False}
            elif self.reasoning in ("low", "medium", "high"):
                # The thinking still happens; "exclude" keeps it out of the reply.
                body["reasoning"] = {"effort": self.reasoning, "exclude": True}
        body.update(self.extra)
        return body

    def _request(self, body: dict) -> urllib.request.Request:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if body.get("stream") else "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if self.provider == "openrouter":
            # Optional attribution headers OpenRouter uses for its app rankings.
            headers["HTTP-Referer"] = os.environ.get("AI_APP_URL", "https://backdatedwg.vercel.app")
            headers["X-Title"] = "Backdate.dwg AI editor"
        return urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def complete(self, messages: list[dict], max_tokens: Optional[int] = None, on_delta: Optional[Callable[[str, str], None]] = None) -> str:
        """The reply text. With ``on_delta`` the reply is streamed: ``on_delta(kind, text)``
        is called with ``("content", piece)`` and ``("reasoning", piece)`` as they arrive."""
        stream = on_delta is not None
        req = self._request(self._body(messages, max_tokens or self.max_tokens, stream))
        last: Optional[LLMError] = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if stream:
                        return _read_stream(resp, on_delta)
                    data = json.loads(resp.read().decode("utf-8"))
                return _content(data)
            except urllib.error.HTTPError as e:
                err = _http_error(e, self)
                if err is None:  # retryable
                    last = LLMError(_retry_message(e))
                else:
                    raise err from None
            except _Retry as e:
                last = LLMError(str(e))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                last = LLMError(f"Couldn't reach the AI service ({self.label}).")
            except (ValueError, KeyError):
                raise LLMError("The AI service sent a reply that couldn't be read.") from None
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
        raise last or LLMError("The AI service didn't answer.")


# Older code and tests refer to the NVIDIA-specific name.
NvidiaModel = OpenAICompatModel


class _Retry(Exception):
    pass


def _error_text(raw: bytes) -> str:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or "")
        if isinstance(err, str):
            return err
    except ValueError:
        pass
    return ""


def _http_error(e: urllib.error.HTTPError, model: OpenAICompatModel) -> Optional[LLMError]:
    """A final error to raise, or None when the request is worth retrying."""
    status = e.code
    try:
        detail = _error_text(e.read())
    except Exception:  # noqa: BLE001
        detail = ""
    if status in (401, 403):
        return LLMError(f"The AI service ({model.label}) rejected the API key.")
    if status == 402:
        return LLMError("The AI account is out of credits. Add credits, or switch to a free model.")
    if status == 404:
        return LLMError(f"The AI service doesn't offer the model {model.name!r}. Check AI_MODEL.")
    if status == 429 and "per-day" in detail.lower():
        return LLMError("Today's free AI allowance is used up (OpenRouter free models have a daily limit). Built-in commands still work.")
    if status in (408, 429, 500, 502, 503, 504):
        return None
    return LLMError(f"The AI service refused the request (HTTP {status}).")


def _retry_message(e: urllib.error.HTTPError) -> str:
    if e.code == 429:
        return "The AI service is busy right now. Try again in a moment."
    return "The AI service had a problem. Try again in a moment."


_THINK = re.compile(r"<think>.*?(</think>|$)", re.S)


def _clean(text: str) -> str:
    # Reasoning models may print their thinking inline; it isn't part of the answer.
    return _THINK.sub("", text).strip()


def _content(data: dict) -> str:
    if isinstance(data, dict) and data.get("error"):
        msg = _error_text(json.dumps(data).encode())
        raise _Retry(f"The AI service reported a problem: {msg[:160]}" if msg else "The AI service reported a problem.")
    try:
        choice = data["choices"][0]
        text = choice["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise ValueError("no content") from e
    if not text:
        if choice.get("finish_reason") == "length":
            raise LLMError("The model ran out of room before it answered. Try a shorter or more specific request.")
        raise ValueError("empty content")
    if not isinstance(text, str):
        raise ValueError("content isn't text")
    return _clean(text)


def _read_stream(resp, on_delta: Callable[[str, str], None]) -> str:
    """Read an SSE stream of chat-completion chunks."""
    parts: list[str] = []
    finish = None
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        if not line or line.startswith(":"):  # comments are keep-alives ("OPENROUTER PROCESSING")
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        if chunk.get("error"):
            raise _Retry("The AI service reported a problem mid-answer.")
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("reasoning"):
                on_delta("reasoning", str(delta["reasoning"]))
            if delta.get("content"):
                piece = str(delta["content"])
                parts.append(piece)
                on_delta("content", piece)
            finish = choice.get("finish_reason") or finish
    text = "".join(parts)
    if not text.strip():
        if finish == "length":
            raise LLMError("The model ran out of room before it answered. Try a shorter or more specific request.")
        raise ValueError("empty stream")
    return _clean(text)


def _provider_for(key_name: str, key: str) -> str:
    explicit = os.environ.get("AI_PROVIDER", "").strip().lower()
    if explicit in PROVIDERS:
        return explicit
    if key.startswith("sk-or-") or key_name == "OPENROUTER_API_KEY":
        return "openrouter"
    if key.startswith("nvapi-"):
        return "nvidia"
    if _env("AI_BASE_URL"):
        return "custom"
    if key_name in ("NVIDIA_API_KEY", "NEMOTRON_API_KEY") and _env("NVIDIA_BASE_URL"):
        return "custom" if "nvidia.com" not in _env("NVIDIA_BASE_URL") else "nvidia"
    if key_name in ("NVIDIA_API_KEY", "NEMOTRON_API_KEY"):
        return "nvidia"
    return "openrouter"


def _settings(vision: bool = False) -> Optional[OpenAICompatModel]:
    key_name, key = next(((n, os.environ[n].strip()) for n in KEY_NAMES if os.environ.get(n, "").strip()), ("", ""))
    if not key and os.environ.get("AI_PROVIDER", "").strip().lower() in LOCAL_PROVIDERS:
        key = "local"  # local servers usually ignore the key
    if not key:
        return None
    provider = _provider_for(key_name, key)
    preset = PROVIDERS[provider]
    base = _env("AI_BASE_URL") or (_env("NVIDIA_BASE_URL") if provider in ("nvidia", "custom") else "") or preset["base"]
    if vision:
        name = _env("AI_VISION_MODEL") or preset["vision"]
        if not name:
            return None
    else:
        name = _env("AI_MODEL", "NEMOTRON_MODEL") or preset["model"]
    extra: dict = {}
    raw = _env("AI_EXTRA_BODY", "NVIDIA_EXTRA_BODY")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                extra = parsed
        except ValueError:
            pass
    reasoning = (_env("AI_REASONING") or ("off" if provider in LOCAL_PROVIDERS else "low")).lower()
    try:
        max_tokens = max(256, min(int(_env("AI_MAX_TOKENS") or 4096), 32768))
    except ValueError:
        max_tokens = 4096
    try:
        timeout = float(_env("AI_TIMEOUT", "NVIDIA_TIMEOUT") or 120)
    except ValueError:
        timeout = 120.0
    return OpenAICompatModel(key, base, name, provider, extra, reasoning, max_tokens, timeout)


def from_env() -> Optional[OpenAICompatModel]:
    """The chat model, or None when no key is configured."""
    return _settings(vision=False)


def vision_from_env() -> Optional[OpenAICompatModel]:
    """A model that accepts images, or None."""
    return _settings(vision=True)


def describe(model) -> dict:
    """What the page may show about the model (never the key)."""
    if model is None:
        return {"enabled": False, "model": None, "provider": None}
    return {"enabled": True, "model": getattr(model, "name", None), "provider": getattr(model, "label", None) or "AI"}
