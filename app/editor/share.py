"""Sharing: review links with pinned comments and approval, and live collaboration.

Roles
* ``viewer``   — sees a frozen revision, adds pinned comments, downloads it.
* ``approver`` — a viewer who can also approve the revision or request changes.
* ``editor``   — joins the live editing session (chat, accept, undo) and sees
                 everyone's cursors.

Viewer and approver links freeze the revision they were made from, so what a
client approves is exactly what they saw. They are stored on disk and last
``BACKDATE_SHARE_DAYS`` (default 14), well beyond the editing session.

Live updates use server-sent events. Each listener has an asyncio queue, so an
open connection costs no thread.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

ROLES = ("viewer", "approver", "editor")
SHARE_DAYS = float(os.environ.get("BACKDATE_SHARE_DAYS", "14"))
MAX_COMMENTS = 500
MAX_LISTENERS = 25
COLORS = ["#c67139", "#7a8a5e", "#3b82f6", "#a855f7", "#ec4899", "#14b8a6", "#f59e0b", "#ef4444"]


class ShareError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def clean_name(name, default: str = "Guest") -> str:
    n = re.sub(r"[\x00-\x1f<>]", "", str(name or "")).strip()[:40]
    return n or default


# ── live events ─────────────────────────────────────────────────────────────


class EventBus:
    """Fan-out of events to the people watching one session."""

    def __init__(self):
        self.lock = threading.Lock()
        self.listeners: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Queue, dict]] = {}

    def join(self, client: str, who: dict) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self.lock:
            if len(self.listeners) >= MAX_LISTENERS and client not in self.listeners:
                raise ShareError(429, "too_many", "Too many people are watching this drawing.")
            self.listeners[client] = (loop, q, who)
        self.publish("presence", {"client": client, **who, "joined": True}, exclude=client)
        return q

    def leave(self, client: str) -> None:
        with self.lock:
            gone = self.listeners.pop(client, None)
        if gone:
            self.publish("presence", {"client": client, "left": True})

    def people(self) -> list[dict]:
        with self.lock:
            return [{"client": c, **who} for c, (_l, _q, who) in self.listeners.items()]

    def publish(self, kind: str, data: dict, exclude: Optional[str] = None) -> None:
        with self.lock:
            targets = [(c, loop, q) for c, (loop, q, _w) in self.listeners.items() if c != exclude]
        for _c, loop, q in targets:
            try:
                loop.call_soon_threadsafe(_offer, q, (kind, data))
            except RuntimeError:  # the listener's loop has closed
                continue


def _offer(q: asyncio.Queue, item) -> None:
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:  # a stalled listener drops events rather than holding everyone up
        pass


async def stream(bus: EventBus, client: str, who: dict, first: Optional[dict] = None):
    """An SSE body: a welcome event, then whatever is published, with keep-alives."""
    q = bus.join(client, who)
    try:
        yield _sse("hello", {"client": client, "people": bus.people(), **(first or {})})
        while True:
            try:
                kind, data = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield _sse(kind, data)
    finally:
        bus.leave(client)


def _sse(kind: str, data) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


# ── review links ────────────────────────────────────────────────────────────


class ShareStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.live: dict[str, dict] = {}  # editor tokens → {"session": id, ...} (they live as long as the session)

    def _dir(self, token: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{16,64}", token or ""):
            raise ShareError(404, "not_found", "That link isn't valid.")
        return self.root / token

    def create_snapshot(self, role: str, session, label: str = "", days: Optional[float] = None, allow_download: bool = True) -> dict:
        if role not in ("viewer", "approver"):
            raise ShareError(400, "bad_role", "Review links are for viewers or approvers.")
        token = secrets.token_urlsafe(18)
        d = self.root / token
        d.mkdir(parents=True)
        with session.lock:
            (d / "geometry.json").write_bytes(session.geometry_bytes())
            session.doc.saveas(d / "drawing.dxf")
            digest = session.digest()
            units = session.units()
        now = time.time()
        meta = {
            "token": token, "role": role, "label": clean_name(label, ""), "name": session.name, "rev": session.rev,
            "created": now, "expires": now + 86400 * min(max(days or SHARE_DAYS, 0.01), 90), "allowDownload": bool(allow_download),
            "session": session.id, "decision": None, "decisions": [],
            "digest": {"entityCount": digest["entityCount"], "layers": digest["layers"], "units": digest["units"], "sizeMetres": digest.get("sizeMetres")},
            "unitsToMetres": units.to_m, "unitsName": units.name, "unitsGuessed": units.guessed,
        }
        self._write(d / "meta.json", meta)
        self._write(d / "comments.json", [])
        return meta

    def create_live(self, session) -> dict:
        token = secrets.token_urlsafe(18)
        rec = {"token": token, "role": "editor", "session": session.id, "name": session.name, "created": time.time()}
        with self.lock:
            self.live[token] = rec
        return rec

    def join(self, token: str) -> dict:
        with self.lock:
            rec = self.live.get(token)
        if not rec:
            raise ShareError(404, "not_found", "That editing link has expired or was revoked.")
        return rec

    @staticmethod
    def _write(path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        tmp.replace(path)

    def meta(self, token: str) -> dict:
        d = self._dir(token)
        try:
            meta = json.loads((d / "meta.json").read_text("utf-8"))
        except (OSError, ValueError):
            raise ShareError(404, "not_found", "That link has expired or was revoked.") from None
        if time.time() > meta["expires"]:
            shutil.rmtree(d, ignore_errors=True)
            raise ShareError(410, "expired", "That link has expired.")
        return meta

    def file(self, token: str, name: str) -> Path:
        self.meta(token)
        return self._dir(token) / name

    def comments(self, token: str) -> list[dict]:
        self.meta(token)
        try:
            return json.loads((self._dir(token) / "comments.json").read_text("utf-8"))
        except (OSError, ValueError):
            return []

    def add_comment(self, token: str, author: str, text: str, x: Optional[float], y: Optional[float], reply_to: Optional[str], owner: bool = False) -> dict:
        meta = self.meta(token)
        text = str(text or "").strip()
        if not text:
            raise ShareError(400, "empty", "Write something first.")
        with self.lock:
            items = self.comments(token)
            if len(items) >= MAX_COMMENTS:
                raise ShareError(429, "too_many", "This link has reached its comment limit.")
            if reply_to and not any(c["id"] == reply_to for c in items):
                raise ShareError(404, "not_found", "That comment doesn't exist.")
            c = {"id": secrets.token_hex(6), "author": clean_name(author, "Owner" if owner else "Guest"), "owner": owner,
                 "text": text[:2000], "x": x, "y": y, "replyTo": reply_to, "time": int(time.time()), "resolved": False, "rev": meta["rev"]}
            items.append(c)
            self._write(self._dir(token) / "comments.json", items)
        return c

    def resolve(self, token: str, cid: str, resolved: bool = True) -> dict:
        with self.lock:
            items = self.comments(token)
            for c in items:
                if c["id"] == cid:
                    c["resolved"] = resolved
                    self._write(self._dir(token) / "comments.json", items)
                    return c
        raise ShareError(404, "not_found", "That comment doesn't exist.")

    def decide(self, token: str, author: str, decision: str, note: str) -> dict:
        meta = self.meta(token)
        if meta["role"] != "approver":
            raise ShareError(403, "not_allowed", "This link can comment but not approve.")
        if decision not in ("approved", "changes_requested"):
            raise ShareError(400, "bad_decision", "Choose approve or request changes.")
        rec = {"decision": decision, "author": clean_name(author), "note": str(note or "").strip()[:1000], "time": int(time.time()), "rev": meta["rev"]}
        with self.lock:
            meta["decision"] = rec
            meta["decisions"] = (meta.get("decisions") or []) + [rec]
            self._write(self._dir(token) / "meta.json", meta)
        return rec

    def revoke(self, token: str) -> bool:
        with self.lock:
            if self.live.pop(token, None):
                return True
        try:
            d = self._dir(token)
        except ShareError:
            return False
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            return True
        return False

    def for_session(self, session_id: str) -> list[dict]:
        out = []
        for d in self.root.iterdir():
            if not d.is_dir():
                continue
            try:
                meta = self.meta(d.name)
            except ShareError:
                continue
            if meta.get("session") == session_id:
                comments = self.comments(d.name)
                out.append({**{k: meta[k] for k in ("token", "role", "label", "rev", "created", "expires", "decision")},
                            "comments": len(comments), "open": sum(1 for c in comments if not c["resolved"])})
        with self.lock:
            out += [{"token": r["token"], "role": "editor", "label": "", "rev": None, "created": r["created"], "expires": None,
                     "decision": None, "comments": 0, "open": 0} for r in self.live.values() if r["session"] == session_id]
        out.sort(key=lambda r: r["created"])
        return out

    def sweep(self) -> None:
        now = time.time()
        for d in self.root.iterdir():
            try:
                meta = json.loads((d / "meta.json").read_text("utf-8"))
                if now > meta["expires"]:
                    shutil.rmtree(d, ignore_errors=True)
            except (OSError, ValueError):
                continue

    def drop_session(self, session_id: str) -> None:
        with self.lock:
            for t in [t for t, r in self.live.items() if r["session"] == session_id]:
                self.live.pop(t, None)
