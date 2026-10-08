"""HTTP API for the AI editor, mounted under ``/api/editor``."""

from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import converter
from ..engines import CancelToken
from ..jobs import Job
from ..versions import BY_YEAR, TARGET_YEARS, DetectError, detect, order
from . import agent, analysis, llm, ops, sample, standards
from .memory import file_fingerprint
from .session import AI_LIMIT, EditorError, EditorSession, EditorStore

router = APIRouter(prefix="/api/editor")

MAX_UPLOAD = int(os.environ.get("BACKDATE_MAX_UPLOAD_MB", "200")) * 1024 * 1024
ALLOWED_EXT = {".dwg", ".dxf"}


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def install(app: FastAPI) -> None:
    app.include_router(router)

    @app.exception_handler(EditorError)
    async def _editor_error(_request: Request, exc: EditorError):  # noqa: ANN202
        return error(exc.status, exc.code, exc.message)


def store(request: Request) -> EditorStore:
    return request.app.state.editor


def session_of(request: Request, sid: str) -> EditorSession:
    s = store(request).get(sid)
    if s is None:
        raise EditorError(404, "not_found", "This editing session has expired. Open the drawing again.")
    return s


def _model(request: Request):
    return getattr(request.app.state, "editor_llm", None) or llm.from_env()


@router.get("/config")
def config(request: Request):
    model = _model(request)
    jobs = request.app.state.jobs
    return {
        "ai": {**llm.describe(model), "limit": AI_LIMIT, "vision": llm.vision_from_env() is not None},
        "canReadDwg": jobs.engines.can_read_dwg,
        "formats": [f for f in ("DWG", "DXF") if jobs.engines.can_write(f)],
        "maxUploadMB": MAX_UPLOAD // (1024 * 1024),
        "targets": [{"year": y, "code": BY_YEAR[y].code, "label": BY_YEAR[y].label} for y in TARGET_YEARS],
    }


# ── opening a drawing ───────────────────────────────────────────────────────


@router.post("/sessions", status_code=201)
async def open_drawing(request: Request, file: UploadFile = File(...)):
    st = store(request)
    jobs = request.app.state.jobs
    name = Path(file.filename or "drawing").name
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        return error(415, "unsupported_type", "Only .dwg and .dxf files can be opened.")
    work = st.root / f"_open-{uuid.uuid4().hex}"
    work.mkdir(parents=True)
    src = work / ("source" + Path(name).suffix.lower())
    try:
        size = 0
        with src.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    return error(413, "too_large", f"That file is over {MAX_UPLOAD // (1024 * 1024)} MB.")
                out.write(chunk)
        if size == 0:
            return error(400, "empty", "That file is empty.")
        with src.open("rb") as fh:
            head = fh.read(64 * 1024)
        try:
            det = detect(head, name)
        except DetectError as e:
            return error(422, "unsupported_type", str(e))
        if det.kind == "DWG" and not jobs.engines.can_read_dwg:
            return error(503, "no_engine", "This server can't read DWG files yet. Upload a DXF instead.")
        return _open(request, src, name, det, work)
    finally:
        await file.close()
        shutil.rmtree(work, ignore_errors=True)


def _open(request: Request, src: Path, name: str, det, work: Path):
    jobs = request.app.state.jobs
    try:
        doc, _auditor, notes = converter._load(src, det, jobs.engines, work / "read", CancelToken())
    except converter.ConversionError as e:
        return error(422, e.code, e.message)
    label = f"{det.kind}, {det.version.label}" if det.version else det.kind
    session = store(request).create(doc, name, notes, label, fingerprint=file_fingerprint(src))
    return JSONResponse(session.summary(), status_code=201)


SAMPLE_FINGERPRINT = "sample-warehouse-v1"


@router.post("/sessions/sample", status_code=201)
def open_sample(request: Request):
    session = store(request).create(sample.build(), "Warehouse B (sample).dxf", [], "DXF, sample drawing", fingerprint=SAMPLE_FINGERPRINT)
    return JSONResponse(session.summary(), status_code=201)


@router.get("/sessions/{sid}")
def get_session(sid: str, request: Request):
    return session_of(request, sid).summary()


@router.delete("/sessions/{sid}", status_code=204)
def close_session(sid: str, request: Request):
    if not store(request).delete(sid):
        return error(404, "not_found", "This editing session has expired.")
    return None


@router.get("/sessions/{sid}/geometry")
def geometry(sid: str, request: Request):
    return Response(session_of(request, sid).geometry_bytes(), media_type="application/json")


# ── talking to it ───────────────────────────────────────────────────────────


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    selection: list[str] = Field(default_factory=list, max_length=5000)
    area: Optional[list[float]] = Field(default=None, min_length=4, max_length=4)


class StageBody(BaseModel):
    ops: list[dict] = Field(min_length=1, max_length=12)
    selection: list[str] = Field(default_factory=list, max_length=5000)


def _chat_payload(session: EditorSession, body: ChatBody, result) -> dict:
    if result.source == "local":  # keep local exchanges in the model's memory too
        session.remember("user", body.message)
        session.remember("assistant", result.reply)
    return {
        "reply": result.reply,
        "source": result.source,
        "error": result.error,
        "queries": result.queries,
        "proposal": result.proposal,
        "data": result.data,
        "suggestions": result.suggestions,
        "aiCallsLeft": max(0, AI_LIMIT - session.ai_calls),
    }


@router.post("/sessions/{sid}/chat")
def chat(sid: str, body: ChatBody, request: Request):
    session = session_of(request, sid)
    result = agent.run(session, body.message, body.selection, _model(request), area=body.area)
    return _chat_payload(session, body, result)


@router.post("/sessions/{sid}/chat/stream")
def chat_stream(sid: str, body: ChatBody, request: Request):
    """The same as /chat, as server-sent events: ``status`` lines while it works,
    ``reply`` with the answer as it is written, then ``result`` (the /chat payload)."""
    session = session_of(request, sid)
    model = _model(request)
    events: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            result = agent.run(session, body.message, body.selection, model, area=body.area,
                               emit=lambda kind, data: events.put((kind, data)))
            events.put(("result", _chat_payload(session, body, result)))
        except Exception as e:  # noqa: BLE001 - reported to the page, never a hung stream
            agent.log.exception("chat stream failed")
            events.put(("error", {"message": f"Something went wrong: {e}"}))
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True, name="chat-stream").start()

    def stream():
        yield ": stream open\n\n"
        while True:
            try:
                item = events.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                return
            kind, data = item
            yield f"event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/sessions/{sid}/suggestions")
def suggest(sid: str, request: Request, selected: int = 0):
    session = session_of(request, sid)
    return {"suggestions": agent.suggestions(session, ["x"] * max(0, min(selected, 1)))}


@router.post("/sessions/{sid}/stage")
def stage(sid: str, body: StageBody, request: Request):
    """Propose a list of operations directly (no language model involved)."""
    session = session_of(request, sid)
    try:
        prop = session.stage(body.ops, body.selection, "manual")
    except ops.OpError as e:
        return error(422, "bad_ops", str(e))
    return {"proposal": prop.view()}


class AcceptBody(BaseModel):
    steps: Optional[list[int]] = Field(default=None, max_length=50)


@router.post("/sessions/{sid}/proposals/{pid}/accept")
def accept(sid: str, pid: str, request: Request, body: Optional[AcceptBody] = None):
    session = session_of(request, sid)
    prop = session.accept(pid, steps=body.steps if body else None)
    return {"proposal": {"id": prop.id, "status": prop.status, "summaries": prop.summaries},
            "summary": session.summary(), "suggestions": agent.suggestions(session, [])}


@router.post("/sessions/{sid}/proposals/{pid}/reject")
def reject(sid: str, pid: str, request: Request):
    session = session_of(request, sid)
    prop = session.reject(pid)
    return {"proposal": {"id": prop.id, "status": prop.status}}


@router.post("/sessions/{sid}/undo")
def undo(sid: str, request: Request):
    session = session_of(request, sid)
    label = session.undo()
    return {"label": label, "summary": session.summary()}


@router.post("/sessions/{sid}/redo")
def redo(sid: str, request: Request):
    session = session_of(request, sid)
    label = session.redo()
    return {"label": label, "summary": session.summary()}


# ── getting the result out ──────────────────────────────────────────────────


@router.get("/sessions/{sid}/download.dxf")
def download(sid: str, request: Request):
    session = session_of(request, sid)
    path = session.write_current()
    return FileResponse(path, media_type="image/vnd.dxf", filename=f"{Path(session.name).stem}_edited.dxf")


class ExportBody(BaseModel):
    target: int
    format: str = "DWG"


@router.post("/sessions/{sid}/export", status_code=201)
def export(sid: str, body: ExportBody, request: Request):
    """Save the edited drawing as an older release, through the converter's job queue."""
    session = session_of(request, sid)
    jobs = request.app.state.jobs
    out_format = body.format.upper()
    if body.target not in TARGET_YEARS:
        return error(400, "bad_target", f"AutoCAD {body.target} isn't a supported target version.")
    if out_format not in ("DWG", "DXF"):
        return error(400, "bad_format", "Format must be DWG or DXF.")
    if not jobs.engines.can_write(out_format):
        return error(400, "no_engine", "This server can't write DWG files (ODA File Converter isn't installed). Choose DXF instead.")
    tgt = BY_YEAR[body.target]

    job_id, job_dir = jobs.new_dir()
    try:
        src = job_dir / "source.dxf"
        shutil.copyfile(session.write_current(), src)
        with src.open("rb") as fh:
            head = fh.read(64 * 1024)
        det = detect(head, src.name)
        if order(det.code) < order(tgt.code):
            shutil.rmtree(job_dir, ignore_errors=True)
            label = det.version.label if det.version else det.code
            return error(400, "already_older", f"This drawing is in {label} format, which is older than AutoCAD {body.target}. Pick an older target.")
        job = Job(id=job_id, dir=job_dir, source=src, original_name=f"{Path(session.name).stem}_edited.dxf", size=src.stat().st_size, detected=det, target=tgt, out_format=out_format)
    except BaseException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    jobs.submit(job)
    return job.to_dict()


# ── reading the drawing ─────────────────────────────────────────────────────


def _csv(text: str, name: str) -> Response:
    return Response(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/sessions/{sid}/health")
def health(sid: str, request: Request):
    return session_of(request, sid).health()


class HandlesBody(BaseModel):
    handles: list[str] = Field(min_length=1, max_length=5000)


@router.post("/sessions/{sid}/inspect")
def inspect(sid: str, body: HandlesBody, request: Request):
    s = session_of(request, sid)
    with s.lock:
        return {"items": analysis.inspect(s.doc, body.handles, s.units())}


class AreaBody(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)


@router.post("/sessions/{sid}/area")
def area(sid: str, body: AreaBody, request: Request):
    s = session_of(request, sid)
    with s.lock:
        summary = analysis.area_summary(s.doc, s.scene(), body.bbox, s.units())
    return {**summary, "text": analysis.describe_area(summary)}


@router.get("/sessions/{sid}/takeoff")
def takeoff(sid: str, request: Request, format: str = "json"):
    s = session_of(request, sid)
    with s.lock:
        t = analysis.takeoff(s.doc, s.units())
    if format == "csv":
        return _csv(analysis.takeoff_csv(t), f"{Path(s.name).stem}_takeoff.csv")
    return t


@router.get("/sessions/{sid}/rooms")
def rooms(sid: str, request: Request, min_area: float = 1.0):
    s = session_of(request, sid)
    with s.lock:
        rows = analysis.rooms(s.doc, s.units(), min_area_m2=max(0.0, min_area))
    return {"rooms": rows, "totalSquareMetres": round(sum(r["squareMetres"] for r in rows if r["name"] != "(unnamed)"), 2)}


@router.get("/sessions/{sid}/schedule")
def schedule(sid: str, request: Request, block: Optional[str] = None, mode: str = "instances", format: str = "json"):
    if mode not in ("instances", "bom"):
        return error(400, "bad_mode", "mode must be instances or bom.")
    s = session_of(request, sid)
    with s.lock:
        table = analysis.schedule(s.doc, block, mode)
    if format == "csv":
        return _csv(analysis.table_csv(table), f"{Path(s.name).stem}_{'bom' if mode == 'bom' else 'schedule'}.csv")
    return table


@router.get("/sessions/{sid}/explain")
def explain(sid: str, request: Request):
    s = session_of(request, sid)
    h = s.health()
    with s.lock:
        return analysis.explain(s.doc, s.digest(), s.units(), {"score": h["score"], "issues": h["issues"]})


@router.get("/sessions/{sid}/warehouse")
def warehouse(sid: str, request: Request, min_aisle: float = 2.8):
    s = session_of(request, sid)
    with s.lock:
        return analysis.warehouse(s.doc, s.units(), min_aisle_m=max(0.1, min_aisle))


@router.get("/sessions/{sid}/standards")
def standards_proposal(sid: str, request: Request):
    s = session_of(request, sid)
    with s.lock:
        return standards.propose(s.doc)


class MappingBody(BaseModel):
    mapping: str = Field(min_length=1, max_length=200_000)


@router.post("/sessions/{sid}/standards/custom")
def standards_custom(sid: str, body: MappingBody, request: Request):
    s = session_of(request, sid)
    try:
        mapping = standards.parse_mapping(body.mapping)
    except ValueError as e:
        return error(422, "bad_mapping", str(e))
    try:
        prop = s.stage([{"op": "map_layers", "mapping": mapping}], [], "standards", "Apply a custom layer mapping")
    except ops.OpError as e:
        return error(422, "bad_ops", str(e))
    return {"proposal": prop.view()}


# ── memory ──────────────────────────────────────────────────────────────────


@router.get("/sessions/{sid}/memory")
def get_memory(sid: str, request: Request):
    s = session_of(request, sid)
    rec = store(request).memory.get(s.fingerprint)
    if not rec:
        return {"remembered": False}
    return {"remembered": True, "visits": rec.get("visits", 0), "first": rec.get("first"), "last": rec.get("last"),
            "changes": rec.get("changes", [])[-30:], "chat": rec.get("chat", [])[-12:]}


@router.delete("/sessions/{sid}/memory", status_code=204)
def forget_memory(sid: str, request: Request):
    s = session_of(request, sid)
    if s.fingerprint:
        store(request).memory.forget(s.fingerprint)
    s.previous = None
    return None
