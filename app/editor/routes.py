"""HTTP API for the AI editor, mounted under ``/api/editor``."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .. import converter
from ..engines import CancelToken
from ..jobs import Job
from ..versions import BY_YEAR, TARGET_YEARS, DetectError, detect, order
from . import agent, llm, ops, sample
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
    session = store(request).create(doc, name, notes, label)
    return JSONResponse(session.summary(), status_code=201)


@router.post("/sessions/sample", status_code=201)
def open_sample(request: Request):
    session = store(request).create(sample.build(), "Warehouse B (sample).dxf", [], "DXF, sample drawing")
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
    message: str = Field(min_length=1, max_length=2000)
    selection: list[str] = Field(default_factory=list, max_length=5000)


class StageBody(BaseModel):
    ops: list[dict] = Field(min_length=1, max_length=12)
    selection: list[str] = Field(default_factory=list, max_length=5000)


@router.post("/sessions/{sid}/chat")
def chat(sid: str, body: ChatBody, request: Request):
    session = session_of(request, sid)
    result = agent.run(session, body.message, body.selection, _model(request))
    if result.source == "local":  # keep local exchanges in the model's memory too
        session.remember("user", body.message)
        session.remember("assistant", result.reply)
    return {
        "reply": result.reply,
        "source": result.source,
        "error": result.error,
        "queries": result.queries,
        "proposal": result.proposal,
        "aiCallsLeft": max(0, AI_LIMIT - session.ai_calls),
    }


@router.post("/sessions/{sid}/stage")
def stage(sid: str, body: StageBody, request: Request):
    """Propose a list of operations directly (no language model involved)."""
    session = session_of(request, sid)
    try:
        prop = session.stage(body.ops, body.selection, "manual")
    except ops.OpError as e:
        return error(422, "bad_ops", str(e))
    return {"proposal": prop.view()}


@router.post("/sessions/{sid}/proposals/{pid}/accept")
def accept(sid: str, pid: str, request: Request):
    session = session_of(request, sid)
    prop = session.accept(pid)
    return {"proposal": {"id": prop.id, "status": prop.status}, "summary": session.summary()}


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
