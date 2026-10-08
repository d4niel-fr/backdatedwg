"""What the editor remembers about a drawing between sessions, and an index across drawings.

A drawing is recognised by a fingerprint of its file. Each fingerprint keeps a
small JSON record: how often it was opened, the changes accepted, the last few
chat exchanges, and an index of its layers, blocks and labels. That powers:

* "memory": reopening a drawing brings back what was done to it last time,
  and the assistant is told about it;
* search across every drawing this server has seen;
* "similar drawings", by comparing those indexes.

Records live in ``<editor dir>/_memory`` and expire after
``BACKDATE_EDITOR_MEMORY_DAYS`` (default 30). Set
``BACKDATE_EDITOR_MEMORY=off`` to keep nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

MEMORY_DAYS = float(os.environ.get("BACKDATE_EDITOR_MEMORY_DAYS", "30"))
MAX_CHANGES = 60
MAX_CHAT = 12


def enabled() -> bool:
    return os.environ.get("BACKDATE_EDITOR_MEMORY", "on").lower() not in ("off", "0", "false", "no")


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def file_fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:32]


class MemoryStore:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        if enabled():
            root.mkdir(parents=True, exist_ok=True)

    def _path(self, fp: str) -> Path:
        if not re.fullmatch(r"[0-9a-z\-]{6,64}", fp):
            raise ValueError("bad fingerprint")
        return self.root / f"{fp}.json"

    def _load(self, fp: str) -> Optional[dict]:
        try:
            data = json.loads(self._path(fp).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if "alias" in data:
            return self._load(data["alias"]) if data["alias"] != fp else None
        if time.time() - data.get("last", 0) > MEMORY_DAYS * 86400:
            return None
        return data

    def _key(self, fp: str) -> str:
        try:
            data = json.loads(self._path(fp).read_text("utf-8"))
            if "alias" in data and data["alias"] != fp:
                return data["alias"]
        except (OSError, ValueError):
            pass
        return fp

    def _save(self, fp: str, data: dict) -> None:
        tmp = self._path(fp).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        tmp.replace(self._path(fp))

    # ── per drawing ────────────────────────────────────────────────────────
    def get(self, fp: Optional[str]) -> Optional[dict]:
        if not fp or not enabled():
            return None
        with self.lock:
            return self._load(fp)

    def visit(self, fp: Optional[str], name: str, index: dict) -> Optional[dict]:
        """Record an opening; returns what was remembered from before (or None)."""
        if not fp or not enabled():
            return None
        with self.lock:
            key = self._key(fp)
            before = self._load(key)
            now = time.time()
            data = before or {"fp": key, "first": now, "visits": 0, "changes": [], "chat": []}
            previous = {"visits": data["visits"], "last": data.get("last"), "changes": data["changes"][-10:], "chat": data["chat"][-6:]} if before else None
            data.update({"name": name, "last": now, "visits": data["visits"] + 1, "index": index})
            self._save(key, data)
            return previous

    def add_change(self, fp: Optional[str], summaries: list[str], prompt: Optional[str]) -> None:
        if not fp or not enabled():
            return
        with self.lock:
            key = self._key(fp)
            data = self._load(key)
            if data is None:
                return
            data["changes"].append({"time": int(time.time()), "summaries": summaries[:12], "prompt": (prompt or "")[:300]})
            del data["changes"][:-MAX_CHANGES]
            data["last"] = time.time()
            self._save(key, data)

    def add_chat(self, fp: Optional[str], user: str, assistant: str) -> None:
        if not fp or not enabled():
            return
        with self.lock:
            key = self._key(fp)
            data = self._load(key)
            if data is None:
                return
            data["chat"].append({"user": user[:500], "assistant": assistant[:800], "time": int(time.time())})
            del data["chat"][:-MAX_CHAT]
            self._save(key, data)

    def alias(self, new_fp: str, original_fp: Optional[str]) -> None:
        """An edited copy of a drawing shares the original's memory."""
        if not original_fp or not enabled() or new_fp == original_fp:
            return
        with self.lock:
            key = self._key(original_fp)
            if self._load(key) is None or self._path(new_fp).exists():
                return
            self._save(new_fp, {"alias": key, "last": time.time()})

    def forget(self, fp: str) -> bool:
        with self.lock:
            key = self._key(fp)
            removed = False
            for p in self.root.glob("*.json"):
                try:
                    data = json.loads(p.read_text("utf-8"))
                except (OSError, ValueError):
                    continue
                if p.stem == key or data.get("alias") == key:
                    p.unlink(missing_ok=True)
                    removed = True
            return removed

    # ── across drawings ────────────────────────────────────────────────────
    def all(self) -> list[dict]:
        if not enabled():
            return []
        out = []
        with self.lock:
            for p in self.root.glob("*.json"):
                try:
                    data = json.loads(p.read_text("utf-8"))
                except (OSError, ValueError):
                    continue
                if "alias" in data or time.time() - data.get("last", 0) > MEMORY_DAYS * 86400:
                    continue
                out.append(data)
        return out

    def search(self, query: str, limit: int = 50) -> list[dict]:
        """Drawings whose name, layers, blocks or labels contain every word of the query."""
        words = [w for w in re.split(r"\s+", query.lower().strip()) if w]
        if not words:
            return []
        hits = []
        for rec in self.all():
            idx = rec.get("index") or {}
            fields = {
                "name": [rec.get("name", "")],
                "layer": idx.get("layers", []),
                "block": idx.get("blocks", []),
                "text": idx.get("texts", []),
            }
            matches = []
            for kind, values in fields.items():
                for v in values:
                    if all(w in v.lower() for w in words):
                        matches.append({"kind": kind, "value": v})
            if matches or all(any(w in v.lower() for kind_vals in fields.values() for v in kind_vals) for w in words):
                hits.append({"fp": rec["fp"], "name": rec.get("name"), "last": rec.get("last"), "matches": matches[:12], "score": len(matches)})
        hits.sort(key=lambda h: (-h["score"], -(h["last"] or 0)))
        return hits[:limit]

    def similar(self, fp: str, limit: int = 10) -> list[dict]:
        """Other drawings ranked by how much their layers, blocks and labels overlap."""
        me = self.get(fp)
        if not me:
            return []
        mine = _features(me.get("index") or {})
        out = []
        for rec in self.all():
            if rec["fp"] == me["fp"]:
                continue
            theirs = _features(rec.get("index") or {})
            score = _jaccard(mine, theirs)
            if score > 0:
                shared = sorted(mine & theirs)[:10]
                out.append({"fp": rec["fp"], "name": rec.get("name"), "similarity": round(score, 3), "shared": shared, "last": rec.get("last")})
        out.sort(key=lambda r: -r["similarity"])
        return out[:limit]


def _features(index: dict) -> set[str]:
    f = {"layer:" + l.lower() for l in index.get("layers", [])}
    f |= {"block:" + b.lower() for b in index.get("blocks", [])}
    f |= {"text:" + t.lower() for t in index.get("texts", [])[:100]}
    return f


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def index_of(digest: dict) -> dict:
    """The searchable summary of a drawing, from its digest."""
    return {
        "layers": [l["name"] for l in digest.get("layers", []) if l.get("count")][:300],
        "blocks": [b["name"] for b in digest.get("blocks", [])][:100],
        "texts": [t["text"] for t in digest.get("texts", [])][:200],
        "size": digest.get("sizeMetres"),
        "entityCount": digest.get("entityCount"),
        "units": (digest.get("units") or {}).get("short"),
    }
