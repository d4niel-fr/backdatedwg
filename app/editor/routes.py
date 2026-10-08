"""HTTP API for the AI editor, mounted under ``/api/editor``."""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import converter
from ..engines import CancelToken
from ..jobs import Job
from ..versions import BY_YEAR, TARGET_YEARS, DetectError, detect, order
from . import agent, analysis, batch, compare, exports, importers, llm, ops, sample, share, standards
from .memory import file_fingerprint, valid_workspace, ws_tag
from .quotas import MeteredModel, Quotas
from .session import AI_LIMIT, EditorError, EditorSession, EditorStore

OPEN_PATHS = ("/api/editor/config", "/api/editor/usage", "/api/editor/shares/")


def gate(request: Request) -> None:
    """With BACKDATE_REQUIRE_TOKEN=1, only key holders may use the editor (review links stay open)."""
    q = getattr(request.app.state, "quotas", None)
    if q is None or not q.require or request.url.path.startswith(OPEN_PATHS):
        return
    if not q.allowed(request.headers):
        raise EditorError(401, "access_key", "This editor needs an access key. Enter yours in the editor's settings.")


router = APIRouter(prefix="/api/editor", dependencies=[Depends(gate)])

MAX_UPLOAD = int(os.environ.get("BACKDATE_MAX_UPLOAD_MB", "200")) * 1024 * 1024
ALLOWED_EXT = {".dwg", ".dxf"}
IMPORT_EXT = {".pdf", ".wdp", ".json", ".png", ".jpg", ".jpeg", ".webp"}


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def install(app: FastAPI) -> None:
    app.state.quotas = Quotas()
    app.include_router(router)

    @app.exception_handler(EditorError)
    async def _editor_error(_request: Request, exc: EditorError):  # noqa: ANN202
        return error(exc.status, exc.code, exc.message)

    @app.exception_handler(share.ShareError)
    async def _share_error(_request: Request, exc: share.ShareError):  # noqa: ANN202
        return error(exc.status, exc.code, exc.message)


def _who(request: Request) -> Optional[str]:
    """The browser tab making a request (so it isn't sent its own live events)."""
    return (request.headers.get("x-client-id") or "")[:40] or None


def publish(request: Request, s: EditorSession, kind: str, data: dict) -> None:
    s.bus.publish(kind, {**data, "rev": s.rev, "by": request.headers.get("x-client-name", "")[:40]}, exclude=_who(request))


def store(request: Request) -> EditorStore:
    return request.app.state.editor


def session_of(request: Request, sid: str) -> EditorSession:
    s = store(request).get(sid)
    if s is None:
        raise EditorError(404, "not_found", "This editing session has expired. Open the drawing again.")
    return s


def workspace(request: Request) -> Optional[str]:
    return valid_workspace(request.headers.get("x-workspace"))


def _raw_model(request: Request):
    return getattr(request.app.state, "editor_llm", None) or llm.from_env()


def _caller(request: Request) -> dict:
    return request.app.state.quotas.caller(request.headers, request.client.host if request.client else None)


def _model(request: Request):
    """The chat model, counted against the caller's daily allowance."""
    m = _raw_model(request)
    return MeteredModel(m, request.app.state.quotas, _caller(request)) if m is not None else None


@router.get("/usage")
def usage(request: Request):
    q = request.app.state.quotas
    return {**q.usage(_caller(request)), "keysEnabled": bool(q.tokens), "keyRequired": q.require}


@router.get("/config")
def config(request: Request):
    model = _raw_model(request)
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
async def open_drawing(request: Request, file: UploadFile = File(...), page: int = Form(1), scale: float = Form(1.0), width_m: float = Form(0.0)):
    """Open a DWG/DXF, or import a PDF plan, a sketch image or a rack-designer project as a new drawing."""
    st = store(request)
    jobs = request.app.state.jobs
    name = Path(file.filename or "drawing").name
    ext = Path(name).suffix.lower()
    if ext in IMPORT_EXT:
        return await _import(request, file, name, ext, page, scale, width_m)
    if ext not in ALLOWED_EXT:
        return error(415, "unsupported_type", "Open a .dwg or .dxf drawing, or import a .pdf plan, a sketch image (.png/.jpg) or a .wdp project.")
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
    session = store(request).create(doc, name, notes, label, fingerprint=file_fingerprint(src), workspace=workspace(request))
    keep = session.dir / ("original" + src.suffix.lower())
    shutil.copyfile(src, keep)
    session.original_path, session.original_name = keep, name
    return JSONResponse(session.summary(), status_code=201)


SAMPLE_FINGERPRINT = "sample-warehouse-v1"


async def _read_upload(file: UploadFile, limit: int = MAX_UPLOAD) -> bytes:
    chunks, size = [], 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise EditorError(413, "too_large", f"That file is over {limit // (1024 * 1024)} MB.")
        chunks.append(chunk)
    await file.close()
    data = b"".join(chunks)
    if not data:
        raise EditorError(400, "empty", "That file is empty.")
    return data


async def _import(request: Request, file: UploadFile, name: str, ext: str, page: int, scale: float, width_m: float):
    data = await _read_upload(file)
    try:
        if ext == ".pdf":
            doc, notes = importers.pdf_to_dxf(data, page, scale)
            label, new_name = f"PDF page {page}", Path(name).stem + ".dxf"
        elif ext in (".wdp", ".json"):
            doc, project, notes = importers.wdp_to_dxf(data)
            label, new_name = "Warehouse Designer Pro project", project + ".dxf"
        else:
            if width_m <= 0:
                raise importers.ImportError_("Say how wide the sketched area is in real life (metres), so the trace comes out at the right size.")
            vision = getattr(request.app.state, "editor_vision", None) or llm.vision_from_env()
            if vision is not None:
                vision = MeteredModel(vision, request.app.state.quotas, _caller(request))
            doc, notes = importers.sketch_to_dxf(data, width_m, vision)
            label, new_name = "Traced sketch", Path(name).stem + "_traced.dxf"
    except importers.ImportError_ as e:
        return error(422, "import_failed", str(e))
    except llm.LLMError as e:
        return error(502, "ai_failed", str(e))
    from .memory import fingerprint as fp_of

    session = store(request).create(doc, new_name, notes, label, fingerprint=fp_of(data), workspace=workspace(request))
    keep = session.dir / ("original" + ext)
    keep.write_bytes(data)
    session.original_path, session.original_name = keep, name
    return JSONResponse(session.summary(), status_code=201)


@router.post("/parse-table")
async def parse_table(file: UploadFile = File(...)):
    """CSV or Excel → columns and rows (for drawing a table or filling a title block)."""
    data = await _read_upload(file, 20 * 1024 * 1024)
    try:
        return importers.parse_table(data, Path(file.filename or "").name)
    except importers.ImportError_ as e:
        return error(422, "bad_table", str(e))


@router.post("/sessions/sample", status_code=201)
def open_sample(request: Request):
    session = store(request).create(sample.build(), "Warehouse B (sample).dxf", [], "DXF, sample drawing", fingerprint=SAMPLE_FINGERPRINT, workspace=workspace(request))
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
    payload = _chat_payload(session, body, result)
    publish(request, session, "chat", {"message": body.message, "reply": result.reply, "proposal": result.proposal, "data": result.data})
    return payload


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
            payload = _chat_payload(session, body, result)
            events.put(("result", payload))
            publish(request, session, "chat", {"message": body.message, "reply": result.reply, "proposal": result.proposal, "data": result.data})
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
    publish(request, session, "proposal", {"proposal": prop.view()})
    return {"proposal": prop.view()}


class AcceptBody(BaseModel):
    steps: Optional[list[int]] = Field(default=None, max_length=50)


@router.post("/sessions/{sid}/proposals/{pid}/accept")
def accept(sid: str, pid: str, request: Request, body: Optional[AcceptBody] = None):
    session = session_of(request, sid)
    prop = session.accept(pid, steps=body.steps if body else None)
    publish(request, session, "changed", {"what": "accepted", "proposal": prop.id, "summaries": prop.summaries})
    return {"proposal": {"id": prop.id, "status": prop.status, "summaries": prop.summaries},
            "summary": session.summary(), "suggestions": agent.suggestions(session, [])}


@router.post("/sessions/{sid}/proposals/{pid}/reject")
def reject(sid: str, pid: str, request: Request):
    session = session_of(request, sid)
    prop = session.reject(pid)
    publish(request, session, "rejected", {"proposal": prop.id})
    return {"proposal": {"id": prop.id, "status": prop.status}}


@router.post("/sessions/{sid}/undo")
def undo(sid: str, request: Request):
    session = session_of(request, sid)
    label = session.undo()
    publish(request, session, "changed", {"what": "undo", "label": label})
    return {"label": label, "summary": session.summary()}


@router.post("/sessions/{sid}/redo")
def redo(sid: str, request: Request):
    session = session_of(request, sid)
    label = session.redo()
    publish(request, session, "changed", {"what": "redo", "label": label})
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
    rec = store(request).memory.get(s.memory_key)
    if not rec:
        return {"remembered": False}
    return {"remembered": True, "visits": rec.get("visits", 0), "first": rec.get("first"), "last": rec.get("last"),
            "changes": rec.get("changes", [])[-30:], "chat": rec.get("chat", [])[-12:]}


@router.delete("/sessions/{sid}/memory", status_code=204)
def forget_memory(sid: str, request: Request):
    s = session_of(request, sid)
    if s.memory_key:
        store(request).memory.forget(s.memory_key)
    s.previous = None
    return None


# ── compare revisions ───────────────────────────────────────────────────────


def _compare_payload(s: EditorSession, result: dict, other_name: str) -> dict:
    s.last_compare = {"result": result, "other": other_name}  # type: ignore[attr-defined]
    out = {k: v for k, v in result.items() if k not in ("added", "removed", "changed")}
    out["added"], out["removed"], out["changed"] = result["added"][:5000], result["removed"][:5000], result["changed"][:5000]
    out["other"] = other_name
    out["reportUrl"] = f"/api/editor/sessions/{s.id}/compare/report.txt"
    return out


@router.post("/sessions/{sid}/compare/original")
def compare_original(sid: str, request: Request):
    """What changed since the drawing was opened."""
    s = session_of(request, sid)
    with s.lock:
        result = compare.compare(s.original_doc(), s.doc, s.units())
    return _compare_payload(s, result, f"{s.name} (as opened)")


@router.post("/sessions/{sid}/compare")
async def compare_file(sid: str, request: Request, file: UploadFile = File(...)):
    """Compare another revision (older) with the drawing that is open (newer)."""
    s = session_of(request, sid)
    jobs = request.app.state.jobs
    name = Path(file.filename or "other").name
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        return error(415, "unsupported_type", "Only .dwg and .dxf files can be compared.")
    work = store(request).root / f"_cmp-{uuid.uuid4().hex}"
    work.mkdir(parents=True)
    try:
        src = work / ("other" + Path(name).suffix.lower())
        size = 0
        with src.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    return error(413, "too_large", f"That file is over {MAX_UPLOAD // (1024 * 1024)} MB.")
                out.write(chunk)
        with src.open("rb") as fh:
            head = fh.read(64 * 1024)
        try:
            det = detect(head, name)
        except DetectError as e:
            return error(422, "unsupported_type", str(e))
        if det.kind == "DWG" and not jobs.engines.can_read_dwg:
            return error(503, "no_engine", "This server can't read DWG files yet. Upload a DXF instead.")
        try:
            other, _a, _n = converter._load(src, det, jobs.engines, work / "read", CancelToken())
        except converter.ConversionError as e:
            return error(422, e.code, e.message)
        with s.lock:
            result = compare.compare(other, s.doc, s.units())
        return _compare_payload(s, result, name)
    finally:
        await file.close()
        shutil.rmtree(work, ignore_errors=True)


@router.get("/sessions/{sid}/compare/report.txt")
def compare_report(sid: str, request: Request):
    s = session_of(request, sid)
    last = getattr(s, "last_compare", None)
    if not last:
        return error(404, "not_found", "Compare the drawing with another revision first.")
    text = compare.report_text(last["result"], last["other"], s.name)
    return Response(text, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{Path(s.name).stem}_comparison.txt"'})


class CloudsBody(BaseModel):
    rev: str = Field(default="A", min_length=1, max_length=8)
    description: str = Field(default="Revised as marked", max_length=200)
    table: bool = True


@router.post("/sessions/{sid}/compare/clouds")
def compare_clouds(sid: str, body: CloudsBody, request: Request):
    """Revision clouds around everything the last comparison found, plus a revision-table row."""
    s = session_of(request, sid)
    last = getattr(s, "last_compare", None)
    if not last or not last["result"].get("regions"):
        return error(409, "nothing_compared", "Compare with another revision first; there are no changed areas to mark.")
    ext = s.digest().get("extents")
    at = (ext[2] + s.text_height() * 4, ext[1] + s.text_height() * 12) if (ext and body.table) else None
    from datetime import date

    steps = compare.cloud_steps(last["result"], body.rev.upper(), body.description, date.today().isoformat(), at, s.text_height())
    try:
        prop = s.stage([], [], "compare", f"Mark revision {body.rev.upper()}", steps=steps,
                       why="Each cloud surrounds a group of objects that differ between the two revisions.")
    except ops.OpError as e:
        return error(422, "bad_ops", str(e))
    return {"proposal": prop.view()}


# ── exports ─────────────────────────────────────────────────────────────────


def _download(data: bytes, media: str, filename: str) -> Response:
    return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/sessions/{sid}/export.pdf")
def export_pdf(sid: str, request: Request, paper: str = "A3", orientation: str = "landscape", hidden: list[str] = Query(default=[]),
               title: str = "", project: str = "", drawn_by: str = "", rev: str = "", date: str = ""):
    s = session_of(request, sid)
    u = s.units()
    data, _info = exports.drawing_pdf(s.scene(), units_to_m=u.to_m, units_name=u.name, units_guessed=u.guessed, name=s.name,
                                      hidden=hidden, paper=paper, orientation=orientation,
                                      fields={"title": title[:80], "project": project[:60], "drawn_by": drawn_by[:40], "rev": rev[:8], "date": date[:20]})
    return _download(data, "application/pdf", f"{Path(s.name).stem}.pdf")


@router.get("/sessions/{sid}/export.svg")
def export_svg(sid: str, request: Request, hidden: list[str] = Query(default=[])):
    s = session_of(request, sid)
    return _download(exports.drawing_svg(s.scene(), hidden, s.name).encode("utf-8"), "image/svg+xml", f"{Path(s.name).stem}.svg")


def _changelog_text(s: EditorSession) -> str:
    lines = [f"Change log: {s.name}", f"Opened as: {s.source_label}", f"Changes accepted: {sum(1 for e in s.log if e.get('source') != 'history')}", ""]
    from datetime import datetime as _dt

    for e in s.log:
        lines.append(f"Change {e['rev']} · {_dt.fromtimestamp(e['time']).strftime('%Y-%m-%d %H:%M')} · {e['source']}")
        if e.get("prompt"):
            lines.append(f'  Asked: "{e["prompt"]}"')
        lines += [f"  - {x}" for x in e["summaries"]]
        lines.append("")
    if not s.log:
        lines.append("No changes were accepted.")
    return "\n".join(lines) + "\n"


@router.get("/sessions/{sid}/changelog.txt")
def changelog_txt(sid: str, request: Request):
    s = session_of(request, sid)
    return Response(_changelog_text(s), media_type="text/plain; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{Path(s.name).stem}_changes.txt"'})


@router.get("/sessions/{sid}/changelog.pdf")
def changelog_pdf(sid: str, request: Request):
    s = session_of(request, sid)
    data = exports.changelog_pdf(s.name, s.log, f"Opened as {s.source_label}. {len(s.log)} entr{'y' if len(s.log) == 1 else 'ies'}.")
    return _download(data, "application/pdf", f"{Path(s.name).stem}_changes.pdf")


@router.get("/sessions/{sid}/proof-pack.zip")
def proof_pack(sid: str, request: Request):
    """Everything needed to show what was done: before and after, the exact operations, comparison, health, PDFs."""
    from . import health as health_mod
    from . import geometry as geometry_mod
    from . import digest as digest_mod

    s = session_of(request, sid)
    stem = Path(s.name).stem
    with s.lock:
        original = s.original_doc()
        cmp = compare.compare(original, s.doc, s.units())
        o_scene = geometry_mod.extract(original)
        o_digest = digest_mod.build(original, o_scene, s.units(), s.name)
        before = health_mod.check(original, o_scene, s.units(), o_digest)
        after = s.health()
        edited = s.write_current().read_bytes()
        u = s.units()
        pdf, info = exports.drawing_pdf(s.scene(), units_to_m=u.to_m, units_name=u.name, units_guessed=u.guessed, name=s.name)
    files: dict[str, bytes] = {}
    if s.original_path and s.original_path.exists():
        files[f"original/{s.original_name or s.name}"] = s.original_path.read_bytes()
    else:
        buf = io.StringIO()
        original.write(buf)
        files[f"original/{stem}.dxf"] = buf.getvalue().encode("utf-8")
    files[f"edited/{stem}_edited.dxf"] = edited
    files["changes/changelog.txt"] = _changelog_text(s).encode("utf-8")
    files["changes/changelog.pdf"] = exports.changelog_pdf(s.name, s.log, f"Opened as {s.source_label}.")
    files["changes/operations.json"] = json.dumps(s.log, indent=2, ensure_ascii=False).encode("utf-8")
    slim = {k: v for k, v in cmp.items() if k not in ("overlay",)}
    files["comparison/comparison.txt"] = compare.report_text(cmp, f"{s.name} (as opened)", f"{stem}_edited.dxf").encode("utf-8")
    files["comparison/comparison.json"] = json.dumps(slim, indent=2, ensure_ascii=False).encode("utf-8")
    files["health/before.json"] = json.dumps(before, indent=2, ensure_ascii=False).encode("utf-8")
    files["health/after.json"] = json.dumps(after, indent=2, ensure_ascii=False).encode("utf-8")
    files[f"{stem}_edited.pdf"] = pdf
    meta = {"drawing": s.name, "openedAs": s.source_label, "changes": len(s.log), "healthBefore": before["score"],
            "healthAfter": after["score"], "comparison": cmp["summary"], "pdfScale": f"1:{info['scale']} @ {info['paper']}"}
    return _download(exports.proof_pack(files, meta), "application/zip", f"{stem}_proof_pack.zip")


# ── recipes and batch jobs ──────────────────────────────────────────────────


@router.get("/sessions/{sid}/recipe")
def recipe_from_session(sid: str, request: Request):
    """The changes accepted so far, as a recipe to replay on other drawings."""
    s = session_of(request, sid)
    try:
        return batch.recipe_from_log(s.name, s.log)
    except batch.RecipeError as e:
        return error(409, "nothing_recorded", str(e))


class RecipeBody(BaseModel):
    recipe: dict


@router.post("/recipes/validate")
def validate_recipe(body: RecipeBody):
    try:
        return {"recipe": batch.validate_recipe(body.recipe)}
    except batch.RecipeError as e:
        return error(422, "bad_recipe", str(e))


@router.post("/sessions/{sid}/recipe/apply")
def apply_recipe_here(sid: str, body: RecipeBody, request: Request):
    """Run a recipe on the open drawing, as one step-by-step proposal."""
    s = session_of(request, sid)
    try:
        recipe = batch.validate_recipe(body.recipe)
    except batch.RecipeError as e:
        return error(422, "bad_recipe", str(e))
    steps = []
    for cmd in recipe["commands"]:
        plan = agent.local_plan(s, cmd)
        if plan:
            steps += plan[0]
            continue
        parsed = agent.local_ops(cmd)
        if parsed is None:
            return error(422, "bad_recipe", f"“{cmd}” isn't a built-in command.")
        steps.append({"title": cmd, "ops": [{**o, "optional": True} for o in parsed[0]]})
    if recipe["ops"]:
        steps.append({"title": "Operations", "ops": [{**o, "optional": True} for o in recipe["ops"]]})
    steps += [{"title": st.get("title") or "Step", "ops": [{**o, "optional": True} for o in st["ops"]]} for st in recipe["steps"]]
    try:
        prop = s.stage([], [], "recipe", f"Recipe: {recipe['name']}", steps=steps[:8],
                       why="These are the recipe's steps; any that find nothing to do in this drawing are skipped.")
    except ops.OpError as e:
        return error(422, "bad_ops", str(e))
    return {"proposal": prop.view(), "truncated": len(steps) > 8}


def batches(request: Request) -> batch.BatchManager:
    return request.app.state.batches


@router.post("/batch", status_code=201)
async def create_batch(request: Request, files: list[UploadFile] = File(...), recipe: str = Form(...),
                       dry_run: bool = Form(False), callback_url: Optional[str] = Form(None)):
    try:
        rec = batch.validate_recipe(recipe)
        if callback_url:
            batch.check_callback(callback_url)
    except (batch.RecipeError, batch.WebhookError) as e:
        return error(422, "bad_request", str(e))
    if not files or len(files) > batch.MAX_FILES:
        return error(400, "bad_files", f"Send 1 to {batch.MAX_FILES} drawings.")
    if rec["output"]["format"] == "DWG" and not request.app.state.jobs.engines.can_write("DWG"):
        return error(400, "no_engine", "This server can't write DWG files (ODA File Converter isn't installed). Choose DXF.")
    mgr = batches(request)
    jid, d = mgr.new_dir()
    saved = []
    for i, f in enumerate(files):
        name = Path(f.filename or f"drawing{i}.dxf").name
        if Path(name).suffix.lower() not in batch.DRAWING_EXT:
            shutil.rmtree(d, ignore_errors=True)
            return error(415, "unsupported_type", f"{name} isn't a .dwg or .dxf file.")
        dest = d / "in" / name
        if dest.exists():
            dest = d / "in" / f"{Path(name).stem}_{i}{Path(name).suffix}"
        dest.write_bytes(await _read_upload(f))
        saved.append(dest)
    job = batch.BatchJob(jid, d, saved, rec, dry_run=dry_run, callback=callback_url)
    job.model = _model(request) if rec.get("ai_instruction") else None  # counted against whoever started the batch
    job = mgr.submit(job)
    return job.view()


def _batch(request: Request, jid: str) -> batch.BatchJob:
    job = batches(request).get(jid)
    if not job:
        raise EditorError(404, "not_found", "That batch has expired.")
    return job


@router.get("/batch/{jid}")
def get_batch(jid: str, request: Request):
    return _batch(request, jid).view()


@router.get("/batch/{jid}/download")
def download_batch(jid: str, request: Request):
    job = _batch(request, jid)
    if not job.zip_path or not job.zip_path.exists():
        return error(404, "not_ready", "The results aren't ready (or this was a dry run).")
    return FileResponse(job.zip_path, media_type="application/zip", filename=job.zip_path.name)


@router.get("/batch/{jid}/report.txt")
def batch_report(jid: str, request: Request):
    job = _batch(request, jid)
    if not job.report:
        return error(404, "not_ready", "The report isn't ready yet.")
    return Response(job.report, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="batch_report.txt"'})


# ── sharing ─────────────────────────────────────────────────────────────────


def shares(request: Request) -> share.ShareStore:
    return store(request).shares


class ShareBody(BaseModel):
    role: str = "viewer"
    label: str = Field(default="", max_length=80)
    days: Optional[float] = Field(default=None, gt=0, le=90)
    allow_download: bool = True


@router.post("/sessions/{sid}/shares", status_code=201)
def create_share(sid: str, body: ShareBody, request: Request):
    s = session_of(request, sid)
    if body.role not in share.ROLES:
        return error(400, "bad_role", "Role must be viewer, approver or editor.")
    if body.role == "editor":
        rec = shares(request).create_live(s)
        return {**rec, "link": f"editor.html?join={rec['token']}"}
    meta = shares(request).create_snapshot(body.role, s, body.label, body.days, body.allow_download)
    return {**{k: meta[k] for k in ("token", "role", "label", "rev", "created", "expires", "allowDownload")}, "link": f"editor.html?share={meta['token']}"}


@router.get("/sessions/{sid}/shares")
def list_shares(sid: str, request: Request):
    s = session_of(request, sid)
    return {"shares": shares(request).for_session(s.id)}


@router.delete("/sessions/{sid}/shares/{token}", status_code=204)
def revoke_share(sid: str, token: str, request: Request):
    s = session_of(request, sid)
    owned = {r["token"] for r in shares(request).for_session(s.id)}
    if token not in owned or not shares(request).revoke(token):
        return error(404, "not_found", "That link doesn't belong to this drawing.")
    return None


@router.get("/sessions/{sid}/comments")
def session_comments(sid: str, request: Request):
    """Every comment left on this drawing's review links (for pins on the owner's view)."""
    s = session_of(request, sid)
    out = []
    for r in shares(request).for_session(s.id):
        if r["role"] == "editor":
            continue
        out += [{**c, "token": r["token"], "role": r["role"], "label": r["label"]} for c in shares(request).comments(r["token"])]
    out.sort(key=lambda c: c["time"])
    return {"comments": out}


class CommentBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    author: str = Field(default="", max_length=60)
    x: Optional[float] = None
    y: Optional[float] = None
    reply_to: Optional[str] = Field(default=None, max_length=32)


def _owned(request: Request, s: EditorSession, token: str) -> None:
    if token not in {r["token"] for r in shares(request).for_session(s.id)}:
        raise share.ShareError(404, "not_found", "That link doesn't belong to this drawing.")


@router.post("/sessions/{sid}/shares/{token}/comments", status_code=201)
def owner_comment(sid: str, token: str, body: CommentBody, request: Request):
    s = session_of(request, sid)
    _owned(request, s, token)
    return shares(request).add_comment(token, body.author or "Owner", body.text, body.x, body.y, body.reply_to, owner=True)


class ResolveBody(BaseModel):
    resolved: bool = True


@router.post("/sessions/{sid}/shares/{token}/comments/{cid}/resolve")
def resolve_comment(sid: str, token: str, cid: str, body: ResolveBody, request: Request):
    s = session_of(request, sid)
    _owned(request, s, token)
    return shares(request).resolve(token, cid, body.resolved)


# public side of a review link


@router.get("/shares/{token}")
def share_meta(token: str, request: Request):
    meta = shares(request).meta(token)
    return {k: v for k, v in meta.items() if k not in ("session",)}


@router.get("/shares/{token}/geometry")
def share_geometry(token: str, request: Request):
    return FileResponse(shares(request).file(token, "geometry.json"), media_type="application/json")


@router.get("/shares/{token}/drawing.dxf")
def share_download(token: str, request: Request):
    meta = shares(request).meta(token)
    if not meta.get("allowDownload"):
        return error(403, "not_allowed", "Downloading isn't allowed on this link.")
    return FileResponse(shares(request).file(token, "drawing.dxf"), media_type="image/vnd.dxf", filename=f"{Path(meta['name']).stem}_rev{meta['rev']}.dxf")


@router.get("/shares/{token}/export.pdf")
def share_pdf(token: str, request: Request, paper: str = "A3"):
    from .geometry import Scene

    meta = shares(request).meta(token)
    geo = json.loads(shares(request).file(token, "geometry.json").read_text("utf-8"))
    scene = Scene(items=geo["items"], extents=geo["extents"])
    data, _ = exports.drawing_pdf(scene, units_to_m=meta["unitsToMetres"], units_name=meta["unitsName"], units_guessed=meta["unitsGuessed"],
                                  name=meta["name"], paper=paper, fields={"rev": str(meta["rev"])})
    return _download(data, "application/pdf", f"{Path(meta['name']).stem}_rev{meta['rev']}.pdf")


@router.get("/shares/{token}/comments")
def share_comments(token: str, request: Request):
    return {"comments": shares(request).comments(token)}


@router.post("/shares/{token}/comments", status_code=201)
def add_share_comment(token: str, body: CommentBody, request: Request):
    meta = shares(request).meta(token)
    c = shares(request).add_comment(token, body.author, body.text, body.x, body.y, body.reply_to)
    owner = store(request).get(meta["session"])
    if owner:
        owner.bus.publish("comment", {"token": token, "comment": c, "label": meta["label"]})
    return c


class DecisionBody(BaseModel):
    decision: str
    author: str = Field(default="", max_length=60)
    note: str = Field(default="", max_length=1000)


@router.post("/shares/{token}/decision")
def decide(token: str, body: DecisionBody, request: Request):
    meta = shares(request).meta(token)
    rec = shares(request).decide(token, body.author, body.decision, body.note)
    owner = store(request).get(meta["session"])
    if owner:
        owner.bus.publish("decision", {"token": token, "decision": rec, "label": meta["label"]})
    return rec


@router.get("/join/{token}")
def join(token: str, request: Request):
    rec = shares(request).join(token)
    s = store(request).get(rec["session"])
    if not s:
        return error(410, "expired", "The editing session behind that link has ended.")
    return {"sessionId": s.id, "name": s.name}


# ── live presence ───────────────────────────────────────────────────────────


@router.get("/sessions/{sid}/events")
async def events(sid: str, request: Request, client: str = Query(..., min_length=4, max_length=40), name: str = ""):
    s = session_of(request, sid)
    color = share.COLORS[sum(map(ord, client)) % len(share.COLORS)]
    who = {"name": share.clean_name(name), "color": color}
    return StreamingResponse(share.stream(s.bus, client, who, {"rev": s.rev}), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class PresenceBody(BaseModel):
    client: str = Field(min_length=4, max_length=40)
    name: str = Field(default="", max_length=60)
    x: Optional[float] = None
    y: Optional[float] = None
    selection: list[str] = Field(default_factory=list, max_length=200)


@router.post("/sessions/{sid}/presence", status_code=204)
def presence(sid: str, body: PresenceBody, request: Request):
    s = session_of(request, sid)
    color = share.COLORS[sum(map(ord, body.client)) % len(share.COLORS)]
    s.bus.publish("cursor", {"client": body.client, "name": share.clean_name(body.name), "color": color, "x": body.x, "y": body.y,
                             "selection": body.selection[:200]}, exclude=body.client)
    return None


@router.get("/sessions/{sid}/people")
def people(sid: str, request: Request):
    return {"people": session_of(request, sid).bus.people()}


# ── search and similar drawings ─────────────────────────────────────────────


@router.get("/search")
def search(request: Request, q: str = Query(..., min_length=1, max_length=200)):
    """Search every drawing opened in this workspace (by name, layer, block or label)."""
    ws = workspace(request)
    if not ws:
        return error(400, "no_workspace", "Search needs a workspace key; the editor page sends one automatically.")
    return {"results": store(request).memory.search(q, ws_tag(ws))}


@router.get("/sessions/{sid}/similar")
def similar(sid: str, request: Request):
    s = session_of(request, sid)
    if not s.memory_key:
        return {"results": [], "note": "Similar drawings need a workspace key."}
    return {"results": store(request).memory.similar(s.memory_key, s.ws_tag)}


@router.get("/sessions/{sid}/find")
def find(sid: str, request: Request, q: str = Query(..., min_length=1, max_length=200)):
    """Find text, blocks, layers or a handle in the open drawing."""
    s = session_of(request, sid)
    needle = q.lower().strip()
    hits: dict[str, dict] = {}
    with s.lock:
        boxes: dict[str, list[float]] = {}
        for it in s.scene().items:
            if it["k"] == "p":
                p = it["p"]
                b = [min(p[0::2]), min(p[1::2]), max(p[0::2]), max(p[1::2])]
            else:
                b = [it["x"], it["y"], it["x"] + it["s"] * max(1, len(it["v"])) * 0.6, it["y"] + it["s"]]
            cur = boxes.setdefault(it["h"], b)
            if cur is not b:
                cur[0], cur[1], cur[2], cur[3] = min(cur[0], b[0]), min(cur[1], b[1]), max(cur[2], b[2]), max(cur[3], b[3])
            if it["k"] == "t" and needle in it["v"].lower() and it["h"] not in hits:
                hits[it["h"]] = {"handle": it["h"], "kind": "text", "label": " ".join(it["v"].split())[:80], "layer": it["l"]}
            if len(hits) >= 500:
                break
        for e in s.doc.modelspace():
            h = e.dxf.handle
            if h in hits:
                continue
            if h.lower() == needle:
                hits[h] = {"handle": h, "kind": "handle", "label": e.dxftype(), "layer": e.dxf.get("layer", "0")}
            elif e.dxftype() == "INSERT" and needle in e.dxf.get("name", "").lower():
                hits[h] = {"handle": h, "kind": "block", "label": e.dxf.get("name"), "layer": e.dxf.get("layer", "0")}
            elif needle in e.dxf.get("layer", "0").lower() and len(hits) < 500:
                hits[h] = {"handle": h, "kind": "layer", "label": e.dxftype(), "layer": e.dxf.get("layer", "0")}
            if len(hits) >= 500:
                break
    rows = [{**r, "bbox": boxes.get(r["handle"])} for r in hits.values()]
    order = {"handle": 0, "text": 1, "block": 2, "layer": 3}
    rows.sort(key=lambda r: order[r["kind"]])
    return {"results": rows[:500], "count": len(rows)}
