"""AI usage limits and access keys.

Built-in commands are always free. Calls to the language model are counted per
day, per caller:

* With ``BACKDATE_ACCESS_TOKENS`` set, each caller presents a key (header
  ``X-Access-Token``) with its own daily allowance, like seats on a plan:
      BACKDATE_ACCESS_TOKENS='{"acme-7d1f...": {"name": "Acme", "daily": 500}, "solo-...": {"name": "Sam", "daily": 50}}'
  (or a path to a JSON file holding that object). ``BACKDATE_REQUIRE_TOKEN=1``
  closes the editor to anyone without a key; review links stay open.
* Without keys, callers are counted by address with ``BACKDATE_AI_DAILY_PER_IP``
  (default 100), which keeps a free model's shared daily allowance from being
  spent by one visitor. ``BACKDATE_TRUST_PROXY=1`` reads the address from
  X-Forwarded-For (Render, Fly, and most hosts set it).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from .llm import LLMError


def _load_tokens() -> dict[str, dict]:
    raw = os.environ.get("BACKDATE_ACCESS_TOKENS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(Path(raw).read_text("utf-8") if not raw.startswith("{") else raw)
    except (OSError, ValueError):
        return {}
    out = {}
    for token, info in (data.items() if isinstance(data, dict) else []):
        if isinstance(token, str) and len(token) >= 12:
            info = info if isinstance(info, dict) else {}
            out[token] = {"name": str(info.get("name") or "key")[:60], "daily": int(info.get("daily", 200))}
    return out


class Quotas:
    def __init__(self):
        self.lock = threading.Lock()
        self.used: dict[tuple[str, str], int] = {}
        self.reload()

    def reload(self) -> None:
        self.tokens = _load_tokens()
        self.require = os.environ.get("BACKDATE_REQUIRE_TOKEN") == "1" and bool(self.tokens)
        self.per_ip = int(os.environ.get("BACKDATE_AI_DAILY_PER_IP", "100"))
        self.trust_proxy = os.environ.get("BACKDATE_TRUST_PROXY") == "1"

    def caller(self, headers, client_host: Optional[str]) -> dict:
        """Who is asking, and how much AI they get per day."""
        token = (headers.get("x-access-token") or "").strip()
        if self.tokens:
            info = self.tokens.get(token)
            if info:
                return {"id": "key:" + hashlib.sha256(token.encode()).hexdigest()[:16], "name": info["name"], "daily": info["daily"], "keyed": True}
        ip = client_host or "unknown"
        if self.trust_proxy:
            fwd = (headers.get("x-forwarded-for") or "").split(",")[0].strip()
            ip = fwd or ip
        return {"id": "ip:" + hashlib.sha256(ip.encode()).hexdigest()[:16], "name": "", "daily": self.per_ip, "keyed": False}

    def allowed(self, headers) -> bool:
        """Whether the editor may be used at all (only matters with BACKDATE_REQUIRE_TOKEN)."""
        return not self.require or (headers.get("x-access-token") or "").strip() in self.tokens

    def _day(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def usage(self, caller: dict) -> dict:
        with self.lock:
            used = self.used.get((caller["id"], self._day()), 0)
        return {"name": caller["name"], "daily": caller["daily"], "used": used, "remaining": max(0, caller["daily"] - used), "keyed": caller["keyed"]}

    def spend(self, caller: dict) -> None:
        day = self._day()
        with self.lock:
            for k in [k for k in self.used if k[1] != day]:
                del self.used[k]
            key = (caller["id"], day)
            if self.used.get(key, 0) >= caller["daily"]:
                who = f"the key “{caller['name']}”" if caller["keyed"] else "your connection"
                raise LLMError(f"Today's AI allowance for {who} is used up ({caller['daily']} requests). Built-in commands still work; it resets at midnight UTC.")
            self.used[key] = self.used.get(key, 0) + 1


class MeteredModel:
    """Wraps a model so every call is counted against the caller's allowance."""

    def __init__(self, model, quotas: Quotas, caller: dict):
        self.model, self.quotas, self.caller = model, quotas, caller
        self.name = getattr(model, "name", "model")
        self.label = getattr(model, "label", None)

    def complete(self, messages, max_tokens=None, on_delta=None):
        self.quotas.spend(self.caller)
        kwargs = {}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if on_delta is not None and _streams(self.model):
            kwargs["on_delta"] = on_delta
        return self.model.complete(messages, **kwargs)


def _streams(model) -> bool:
    import inspect

    try:
        return "on_delta" in inspect.signature(model.complete).parameters
    except (TypeError, ValueError):
        return False
