"""The language model behind the chat: NVIDIA's OpenAI-compatible API (Nemotron).

Configuration (environment):

* ``NVIDIA_API_KEY``   the key from build.nvidia.com. Without it the editor still
                       works with its built-in commands.
* ``NEMOTRON_MODEL``   model id; default ``nvidia/nemotron-3-ultra-550b-a55b``.
                       Copy the exact id from the model card if it differs.
* ``NVIDIA_BASE_URL``  default ``https://integrate.api.nvidia.com/v1``. Any
                       OpenAI-compatible server works (a self-hosted vLLM, for example).
* ``NVIDIA_EXTRA_BODY`` optional JSON merged into every request, for model-specific
                       switches such as reasoning settings.

Only the standard library is used, so the converter's requirements don't grow.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional, Protocol

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
TIMEOUT_SECONDS = float(os.environ.get("NVIDIA_TIMEOUT", "120"))


class LLMError(Exception):
    """A failure with a sentence worth showing to the person."""


class ChatModel(Protocol):
    name: str

    def complete(self, messages: list[dict], max_tokens: int = 2048) -> str: ...


@dataclass
class NvidiaModel:
    api_key: str
    base_url: str
    name: str
    extra: dict

    def complete(self, messages: list[dict], max_tokens: int = 2048) -> str:
        body = {"model": self.name, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens, "stream": False, **self.extra}
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        last: Optional[LLMError] = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return _content(data)
            except urllib.error.HTTPError as e:
                status = e.code
                if status in (401, 403):
                    raise LLMError("The AI service rejected the API key.") from None
                if status == 404:
                    raise LLMError(f"The AI service doesn't know the model {self.name!r}. Check NEMOTRON_MODEL.") from None
                if status in (429, 500, 502, 503, 504):
                    last = LLMError("The AI service is busy right now. Try again in a moment." if status == 429 else "The AI service had a problem. Try again in a moment.")
                else:
                    raise LLMError(f"The AI service refused the request (HTTP {status}).") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                last = LLMError("Couldn't reach the AI service.")
            except (ValueError, KeyError):
                raise LLMError("The AI service sent a reply that couldn't be read.") from None
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
        raise last or LLMError("The AI service didn't answer.")


def _content(data: dict) -> str:
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError("no content") from e
    if not isinstance(text, str):
        raise ValueError("content isn't text")
    # Reasoning models may print their thinking inline; it isn't part of the answer.
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def from_env() -> Optional[NvidiaModel]:
    key = os.environ.get("NVIDIA_API_KEY") or os.environ.get("NEMOTRON_API_KEY")
    if not key:
        return None
    extra: dict = {}
    raw = os.environ.get("NVIDIA_EXTRA_BODY")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                extra = parsed
        except ValueError:
            pass
    return NvidiaModel(key.strip(), os.environ.get("NVIDIA_BASE_URL", DEFAULT_BASE_URL), os.environ.get("NEMOTRON_MODEL", DEFAULT_MODEL), extra)
