"""Editing sessions: the open drawing, its history, and proposals awaiting a decision.

State lives in memory (like the converter's jobs), so run a single server
process. Each session keeps the live drawing, a compressed undo/redo history,
and any proposals that haven't been accepted or rejected.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import threading
import time
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import ezdxf
from ezdxf.document import Drawing

from . import digest as digest_mod
from . import geometry, ops
from .geometry import Scene
from .units import Units, detect_units

log = logging.getLogger("backdate.editor")

RETENTION_SECONDS = int(os.environ.get("BACKDATE_RETENTION_SECONDS", "3600"))
MAX_SESSIONS = int(os.environ.get("BACKDATE_EDITOR_MAX_SESSIONS", "20"))
MAX_UNDO_BYTES = 300 * 1024 * 1024
MAX_PENDING = 3
PREVIEW_LIMIT = 30_000
AI_LIMIT = int(os.environ.get("BACKDATE_EDITOR_AI_LIMIT", "100"))


def dump(doc: Drawing) -> str:
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue()


def parse(text: str) -> Drawing:
    return ezdxf.read(io.StringIO(text))


@dataclass
class Proposal:
    id: str
    base_rev: int
    ops: list
    summaries: list[str]
    warnings: list[str]
    touched: ops.Touched
    doc: Drawing
    before_text: str
    preview_remove: list[str]
    preview_add: list[dict]
    preview_truncated: bool
    source: str
    prompt: Optional[str]
    status: str = "pending"  # pending | accepted | rejected | stale
    created: float = field(default_factory=time.time)

    def view(self) -> dict:
        t = self.touched
        return {
            "id": self.id,
            "baseRev": self.base_rev,
            "status": self.status,
            "source": self.source,
            "summaries": self.summaries,
            "warnings": self.warnings,
            "stats": {"removed": len(t.deleted), "changed": len(t.changed), "added": len(t.created), "tables": t.tables},
            "preview": {"remove": self.preview_remove, "add": self.preview_add, "truncated": self.preview_truncated},
            "ops": self.ops,
        }


class EditorError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class EditorSession:
    def __init__(self, sid: str, name: str, doc: Drawing, root: Path, notes: Optional[list[str]] = None, source_label: str = ""):
        self.id = sid
        self.name = name
        self.doc = doc
        self.dir = root / sid
        self.dir.mkdir(parents=True, exist_ok=True)
        self.notes = notes or []
        self.source_label = source_label
        self.rev = 0
        self.created = time.time()
        self.touched = self.created
        self.lock = threading.RLock()
        self.undo_stack: list[tuple[str, bytes]] = []
        self.redo_stack: list[tuple[str, bytes]] = []
        self.proposals: dict[str, Proposal] = {}
        self.log: list[dict] = []
        self.chat: list[dict] = []
        self.ai_calls = 0
        self._scene: Optional[tuple[int, Scene]] = None
        self._digest: Optional[tuple[int, dict]] = None
        self._units: Optional[tuple[int, Units]] = None
        self._geometry: Optional[tuple[int, bytes]] = None

    # ── derived, cached per revision ──────────────────────────────────────
    def scene(self) -> Scene:
        with self.lock:
            if self._scene is None or self._scene[0] != self.rev:
                self._scene = (self.rev, geometry.extract(self.doc))
            return self._scene[1]

    def geometry_bytes(self) -> bytes:
        """The drawable scene as JSON, encoded once per revision."""
        with self.lock:
            if self._geometry is None or self._geometry[0] != self.rev:
                body = json.dumps(self.scene().to_json(self.rev), separators=(",", ":")).encode("utf-8")
                self._geometry = (self.rev, body)
            return self._geometry[1]

    def units(self) -> Units:
        with self.lock:
            if self._units is None or self._units[0] != self.rev:
                ext = self.scene().extents
                size = max(ext[2] - ext[0], ext[3] - ext[1]) if ext else None
                self._units = (self.rev, detect_units(self.doc.header.get("$INSUNITS", 0), size))
            return self._units[1]

    def digest(self) -> dict:
        with self.lock:
            if self._digest is None or self._digest[0] != self.rev:
                self._digest = (self.rev, digest_mod.build(self.doc, self.scene(), self.units(), self.name))
            return self._digest[1]

    def text_height(self) -> float:
        size = (self.digest().get("size") or [1000, 1000])
        raw = max(size) / 250.0
        return float(f"{raw:.2g}") or 1.0

    # ── summary for the client ────────────────────────────────────────────
    def summary(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "name": self.name,
                "rev": self.rev,
                "sourceLabel": self.source_label,
                "notes": self.notes,
                "digest": self.digest(),
                "canUndo": bool(self.undo_stack),
                "canRedo": bool(self.redo_stack),
                "log": self.log[-100:],
                "aiCallsLeft": max(0, AI_LIMIT - self.ai_calls),
                "docVersion": self.doc.dxfversion,
            }

    # ── proposals ─────────────────────────────────────────────────────────
    def stage(self, op_list: list, selection: list[str], source: str, prompt: Optional[str] = None) -> Proposal:
        """Apply ``op_list`` to a copy and describe the result. Raises ops.OpError."""
        with self.lock:
            before = dump(self.doc)
            clone = parse(before)
            result = ops.apply_ops(clone, op_list, self.units(), selection, self.text_height())
            t = result.touched
            fresh = geometry.extract(clone, handles=t.changed | t.created).items if (t.changed or t.created) else []
            remove = sorted(t.deleted | t.changed)
            truncated = len(fresh) > PREVIEW_LIMIT
            prop = Proposal(
                id=uuid.uuid4().hex[:12],
                base_rev=self.rev,
                ops=op_list,
                summaries=result.summaries,
                warnings=result.warnings,
                touched=t,
                doc=clone,
                before_text=before,
                preview_remove=remove[:PREVIEW_LIMIT],
                preview_add=fresh[:PREVIEW_LIMIT],
                preview_truncated=truncated or len(remove) > PREVIEW_LIMIT,
                source=source,
                prompt=prompt,
            )
            pending = [p for p in self.proposals.values() if p.status == "pending"]
            for old in pending[: max(0, len(pending) - (MAX_PENDING - 1))]:
                old.status = "stale"
                old.doc = None  # type: ignore[assignment]
            self.proposals[prop.id] = prop
            return prop

    def _proposal(self, pid: str) -> Proposal:
        p = self.proposals.get(pid)
        if p is None:
            raise EditorError(404, "not_found", "That proposal doesn't exist any more.")
        return p

    def accept(self, pid: str) -> Proposal:
        with self.lock:
            p = self._proposal(pid)
            if p.status != "pending":
                raise EditorError(409, "not_pending", f"That change was already {p.status}.")
            if p.base_rev != self.rev:
                p.status = "stale"
                raise EditorError(409, "stale", "The drawing changed after this was proposed. Ask again to get a fresh proposal.")
            self._push(self.undo_stack, "; ".join(p.summaries)[:200], p.before_text)
            self.redo_stack.clear()
            self.doc = p.doc
            self.rev += 1
            p.status = "accepted"
            p.doc = None  # type: ignore[assignment]
            p.before_text = ""
            for other in self.proposals.values():
                if other.status == "pending":
                    other.status = "stale"
                    other.doc = None  # type: ignore[assignment]
            self.log.append({"rev": self.rev, "time": int(time.time()), "summaries": p.summaries, "prompt": p.prompt, "source": p.source})
            return p

    def reject(self, pid: str) -> Proposal:
        with self.lock:
            p = self._proposal(pid)
            if p.status == "pending":
                p.status = "rejected"
                p.doc = None  # type: ignore[assignment]
                p.before_text = ""
            return p

    # ── history ───────────────────────────────────────────────────────────
    def _push(self, stack: list, label: str, text: str) -> None:
        stack.append((label, zlib.compress(text.encode("utf-8"), 1)))
        while sum(len(b) for _, b in self.undo_stack + self.redo_stack) > MAX_UNDO_BYTES and len(stack) > 1:
            stack.pop(0)

    def undo(self) -> str:
        with self.lock:
            if not self.undo_stack:
                raise EditorError(409, "nothing_to_undo", "There's nothing to undo.")
            label, blob = self.undo_stack.pop()
            self._push(self.redo_stack, label, dump(self.doc))
            self.doc = parse(zlib.decompress(blob).decode("utf-8"))
            self._swapped("Undid: " + label)
            return label

    def redo(self) -> str:
        with self.lock:
            if not self.redo_stack:
                raise EditorError(409, "nothing_to_redo", "There's nothing to redo.")
            label, blob = self.redo_stack.pop()
            self._push(self.undo_stack, label, dump(self.doc))
            self.doc = parse(zlib.decompress(blob).decode("utf-8"))
            self._swapped("Redid: " + label)
            return label

    def _swapped(self, note: str) -> None:
        self.rev += 1
        for p in self.proposals.values():
            if p.status == "pending":
                p.status = "stale"
                p.doc = None  # type: ignore[assignment]
        self.log.append({"rev": self.rev, "time": int(time.time()), "summaries": [note], "prompt": None, "source": "history"})

    # ── files ─────────────────────────────────────────────────────────────
    def write_current(self) -> Path:
        """The drawing as a DXF file on disk, in the version it was opened in."""
        with self.lock:
            path = self.dir / f"rev{self.rev}.dxf"
            if not path.exists():
                for old in self.dir.glob("rev*.dxf"):
                    old.unlink(missing_ok=True)
                self.doc.saveas(path)
            return path

    def remember(self, role: str, content: str) -> None:
        self.chat.append({"role": role, "content": content[:4000]})
        del self.chat[:-40]


class EditorStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, EditorSession] = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0
        self._clean_orphans()

    def _clean_orphans(self) -> None:
        now = time.time()
        for d in self.root.iterdir():
            try:
                if d.is_dir() and now - d.stat().st_mtime >= RETENTION_SECONDS:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass

    def create(self, doc: Drawing, name: str, notes: Optional[list[str]] = None, source_label: str = "") -> EditorSession:
        self.sweep()
        with self._lock:
            while len(self.sessions) >= MAX_SESSIONS:
                oldest = min(self.sessions.values(), key=lambda s: s.touched)
                self._drop(oldest.id)
            sid = uuid.uuid4().hex
            session = EditorSession(sid, name, doc, self.root, notes, source_label)
            self.sessions[sid] = session
            return session

    def get(self, sid: str) -> Optional[EditorSession]:
        self.sweep()
        with self._lock:
            s = self.sessions.get(sid)
        if s:
            s.touched = time.time()
        return s

    def delete(self, sid: str) -> bool:
        with self._lock:
            return self._drop(sid)

    def _drop(self, sid: str) -> bool:
        s = self.sessions.pop(sid, None)
        if not s:
            return False
        shutil.rmtree(s.dir, ignore_errors=True)
        return True

    def sweep(self, now: Optional[float] = None) -> int:
        now = now or time.time()
        if now - self._last_sweep < 30:
            return 0
        self._last_sweep = now
        with self._lock:
            dead = [sid for sid, s in self.sessions.items() if now - s.touched >= RETENTION_SECONDS]
            for sid in dead:
                self._drop(sid)
        return len(dead)
