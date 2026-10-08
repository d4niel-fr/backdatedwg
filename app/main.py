"""HTTP API and static front end for Backdate.dwg."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .editor.routes import install as install_editor
from .editor.session import EditorStore
from .engines import Engines
from .jobs import RETENTION_SECONDS, Job, JobManager, file_info
from .versions import BY_YEAR, DEFAULT_TARGET_YEAR, TARGET_YEARS, DetectError, detect, order

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("ezdxf").setLevel(logging.ERROR)  # it narrates every repair

MAX_UPLOAD_MB = int(os.environ.get("BACKDATE_MAX_UPLOAD_MB", "200"))
MAX_UPLOAD = MAX_UPLOAD_MB * 1024 * 1024
DATA_DIR = Path(os.environ.get("BACKDATE_DATA_DIR", Path(tempfile.gettempdir()) / "backdate"))
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
ALLOWED_EXT = {".dwg", ".dxf"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    engines = Engines.discover()
    logging.getLogger("backdate").info("engines: %s", engines.describe())
    app.state.jobs = JobManager(DATA_DIR, engines)
    # The AI editor keeps its sessions beside the jobs, not inside the folder
    # the job sweeper cleans.
    app.state.editor = EditorStore(DATA_DIR.parent / (DATA_DIR.name + "-editor"))
    yield
    app.state.jobs.shutdown()


app = FastAPI(title="Backdate.dwg", lifespan=lifespan)
install_editor(app)  # /api/editor/* (the AI editor); registered before the static mount below
# The static front end (e.g. on Vercel) may call this API from another origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("BACKDATE_CORS_ORIGINS", "*").split(",") if o.strip()],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def manager(request: Request) -> JobManager:
    return request.app.state.jobs


@app.get("/api/config")
def config(request: Request):
    engines = manager(request).engines
    return {
        "maxUploadMB": MAX_UPLOAD_MB,
        "retentionMinutes": RETENTION_SECONDS // 60,
        "defaultTarget": DEFAULT_TARGET_YEAR,
        "canReadDwg": engines.can_read_dwg,
        "formats": [f for f in ("DWG", "DXF") if engines.can_write(f)],
        "engines": engines.describe(),
        "targets": [
            {
                "year": y,
                "code": BY_YEAR[y].code,
                "label": BY_YEAR[y].label,
                
            }
            for y in TARGET_YEARS
        ],
    }


@app.post("/api/jobs", status_code=201)
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    target: int = Form(DEFAULT_TARGET_YEAR),
    format: str = Form("DWG"),
):
    jobs = manager(request)
    name = Path(file.filename or "drawing").name
    out_format = format.upper()
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        return error(415, "unsupported_type", "Only .dwg and .dxf files can be converted.")
    if target not in TARGET_YEARS:
        return error(400, "bad_target", f"AutoCAD {target} isn't a supported target version.")
    if out_format not in ("DWG", "DXF"):
        return error(400, "bad_format", "Format must be DWG or DXF.")
    tgt = BY_YEAR[target]
    if not jobs.engines.can_write(out_format):
        return error(
            400,
            "no_engine",
            "This server can't write DWG files (ODA File Converter isn't installed). Choose DXF instead.",
        )

    job_id, job_dir = jobs.new_dir()
    src = job_dir / ("source" + Path(name).suffix.lower())
    size = 0
    try:
        with src.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise _TooLarge()
                out.write(chunk)
        if size == 0:
            raise _Reject(400, "empty", "That file is empty.")
        with src.open("rb") as fh:
            head = fh.read(64 * 1024)
        try:
            det = detect(head, name)
        except DetectError as e:
            raise _Reject(422, "unsupported_type", str(e)) from e
        if det.kind == "DWG" and not jobs.engines.can_read_dwg:
            raise _Reject(503, "no_engine", "This server can't read DWG files yet. Upload a DXF instead.")
        if det.version is None:
            raise _Reject(422, "unsupported_version", "This file's AutoCAD version isn't recognised.")
        if order(det.code) < order(tgt.code):
            raise _Reject(
                400,
                "already_older",
                f"This file is already in {det.version.label} format, older than AutoCAD {target}.",
            )
    except _TooLarge:
        shutil.rmtree(job_dir, ignore_errors=True)
        return error(413, "too_large", f"That file is over {MAX_UPLOAD_MB} MB.")
    except _Reject as r:
        shutil.rmtree(job_dir, ignore_errors=True)
        return error(r.status, r.code, r.message)
    except BaseException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    job = Job(
        id=job_id,
        dir=job_dir,
        source=src,
        original_name=name,
        size=size,
        detected=det,
        target=tgt,
        out_format=out_format,
    )
    jobs.submit(job)
    return job.to_dict()


class _TooLarge(Exception):
    pass


class _Reject(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


@app.post("/api/detect")
async def detect_file(file: UploadFile = File(...)):
    """Identify a file's format from its first bytes (the UI sends 64 KB)."""
    head = await file.read(64 * 1024)
    try:
        det = detect(head, file.filename or "")
    except DetectError as e:
        return error(422, "unsupported_type", str(e))
    return file_info(file.filename or "", 0, det)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    job = manager(request).get(job_id)
    if not job:
        return error(404, "not_found", "This conversion has expired or was cancelled.")
    return job.to_dict()


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, request: Request):
    if not manager(request).cancel(job_id):
        return error(404, "not_found", "This conversion has expired or was cancelled.")
    return None


def _finished(job_id: str, request: Request):
    job = manager(request).get(job_id)
    if not job or job.status != "done" or not job.result or not job.result.output.exists():
        return None
    return job


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, request: Request):
    job = _finished(job_id, request)
    if not job:
        return error(404, "not_found", "This file has expired. Convert it again.")
    media = "image/vnd.dwg" if job.out_format == "DWG" else "image/vnd.dxf"
    return FileResponse(job.result.output, media_type=media, filename=job.result.output_name)


@app.get("/api/jobs/{job_id}/report.txt")
def report(job_id: str, request: Request):
    job = _finished(job_id, request)
    if not job:
        return error(404, "not_found", "This report has expired.")
    filename = Path(job.result.output_name).stem + "_report.txt"
    return PlainTextResponse(
        job.result.report_text,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/healthz")
def health(request: Request):
    return {"ok": True, "engines": manager(request).engines.describe()}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
