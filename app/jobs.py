"""In-memory job queue. Each job owns a folder that is deleted after an hour."""

from __future__ import annotations

import logging
import math
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import converter
from .engines import Cancelled, CancelToken, Engines
from .versions import Detected, FormatVersion

log = logging.getLogger("backdate.jobs")

RETENTION_SECONDS = int(os.environ.get("BACKDATE_RETENTION_SECONDS", "3600"))
# Folders whose names start with "_" belong to other features (the AI editor
# keeps its sessions in DATA_DIR/_editor) and are never swept as stale jobs.
EDITOR_SUBDIR = "_editor"
MAX_WORKERS = int(os.environ.get("BACKDATE_WORKERS", "2"))


@dataclass
class Job:
    id: str
    dir: Path
    source: Path
    original_name: str
    size: int
    detected: Detected
    target: FormatVersion
    out_format: str
    created: float = field(default_factory=time.time)
    status: str = "queued"  # queued | running | done | failed | cancelled
    step: int = 1
    layers_blocks: Optional[int] = None
    skipped_so_far: int = 0
    error: Optional[dict] = None
    result: Optional[converter.Result] = None
    finished: Optional[float] = None
    cancel: CancelToken = field(default_factory=CancelToken)
    # progress model: within a stage, creep from lo toward hi on an
    # exponential curve so the bar keeps moving while an engine runs.
    _lo: float = 0.0
    _hi: float = 0.0
    _expected: float = 1.0
    _stage_start: float = field(default_factory=time.time)
    _started: Optional[float] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    # converter.Sink
    def stage(self, index: int, lo: float, hi: float, expected_seconds: float) -> None:
        with self.lock:
            self.step, self._lo, self._hi = index, lo, hi
            self._expected = max(expected_seconds, 0.3)
            self._stage_start = time.time()

    def skipped(self, n: int) -> None:
        self.skipped_so_far = n

    def layers_blocks_count(self, n: int) -> None:
        self.layers_blocks = n

    def progress(self) -> float:
        if self.status == "done":
            return 100.0
        if self.status != "running":
            return 0.0
        t = time.time() - self._stage_start
        frac = 1 - math.exp(-t / self._expected)
        return round(self._lo + (self._hi - self._lo) * 0.95 * frac, 1)

    def eta_seconds(self, progress: float) -> Optional[int]:
        if self.status != "running" or not self._started or progress < 3:
            return None
        elapsed = time.time() - self._started
        return max(1, int(elapsed * (100 - progress) / progress))

    def to_dict(self) -> dict:
        p = self.progress()
        data = {
            "id": self.id,
            "status": self.status,
            "progress": p,
            "step": self.step,
            "etaSeconds": self.eta_seconds(p),
            "layersAndBlocks": self.layers_blocks,
            "skippedSoFar": self.skipped_so_far,
            "file": file_info(self.original_name, self.size, self.detected),
            "target": {"year": self.target.year, "code": self.target.code},
            "format": self.out_format,
            "expiresAt": int((self.finished or self.created) + RETENTION_SECONDS),
        }
        if self.error:
            data["error"] = self.error
        if self.result:
            r = self.result
            data["result"] = {
                "outputName": r.output_name,
                "outputSize": r.output.stat().st_size if r.output.exists() else 0,
                "engine": r.engine,
                "counts": r.report.counts(),
                "items": [i.to_dict() for i in r.report.sorted_items()],
                "downloadUrl": f"/api/jobs/{self.id}/download",
                "reportUrl": f"/api/jobs/{self.id}/report.txt",
            }
        return data


def file_info(name: str, size: int, det: Detected) -> dict:
    v = det.version
    return {
        "name": name,
        "size": size,
        "kind": det.kind,
        "code": det.code,
        "label": v.label if v else "Unknown version",
        "short": v.short if v else "?",
    }


class _SinkAdapter:
    def __init__(self, job: Job):
        self.job = job

    def stage(self, *a):
        self.job.stage(*a)

    def skipped(self, n):
        self.job.skipped(n)

    def layers_blocks(self, n):
        self.job.layers_blocks_count(n)


class JobManager:
    def __init__(self, root: Path, engines: Engines):
        self.root = root
        self.engines = engines
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="convert")
        root.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._sweeper = threading.Thread(target=self._sweep_loop, daemon=True, name="sweeper")
        self._sweeper.start()

    def new_dir(self) -> tuple[str, Path]:
        job_id = uuid.uuid4().hex
        d = self.root / job_id
        d.mkdir(parents=True)
        return job_id, d

    def submit(self, job: Job) -> Job:
        with self._lock:
            self.jobs[job.id] = job
        self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """Stop a job (if still working) and delete it with its files."""
        with self._lock:
            job = self.jobs.pop(job_id, None)
        if not job:
            return False
        job.cancel.cancel()
        if job.status == "running":
            job.status = "cancelled"  # the worker deletes the folder when it stops
        else:
            shutil.rmtree(job.dir, ignore_errors=True)
        return True

    def _run(self, job: Job) -> None:
        if job.cancel.cancelled:
            shutil.rmtree(job.dir, ignore_errors=True)
            return
        job.status = "running"
        job._started = time.time()
        try:
            job.result = converter.run(
                job.source,
                job.original_name,
                job.detected,
                job.target,
                job.out_format,
                self.engines,
                job.dir / "work",
                _SinkAdapter(job),
                job.cancel,
            )
            job.status = "done"
        except Cancelled:
            pass
        except converter.ConversionError as e:
            job.status = "failed"
            job.error = {"code": e.code, "message": e.message}
        except Exception as e:  # noqa: BLE001 - any crash is a failed job, not a dead worker
            log.exception("job %s crashed", job.id)
            job.status = "failed"
            job.error = {"code": "internal", "message": f"Unexpected error while converting: {e}"}
        finally:
            job.finished = time.time()
            job.source.unlink(missing_ok=True)  # the upload is no longer needed
            if job.cancel.cancelled:
                job.status = "cancelled"
                job.result = None
                shutil.rmtree(job.dir, ignore_errors=True)

    def _remove(self, job_id: str) -> None:
        with self._lock:
            job = self.jobs.pop(job_id, None)
        if job:
            shutil.rmtree(job.dir, ignore_errors=True)

    def sweep(self, now: Optional[float] = None) -> int:
        now = now or time.time()
        expired = []
        with self._lock:
            for job_id, job in self.jobs.items():
                if job.status in ("queued", "running"):
                    continue
                if now - (job.finished or job.created) >= RETENTION_SECONDS:
                    expired.append(job_id)
        for job_id in expired:
            self._remove(job_id)
        # Folders left behind by a previous process (e.g. after a restart).
        known = set(self.jobs)
        for d in self.root.iterdir():
            if d.is_dir() and d.name not in known and not d.name.startswith("_"):
                try:
                    if now - d.stat().st_mtime >= RETENTION_SECONDS:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
        return len(expired)

    def _sweep_loop(self) -> None:
        while not self._stop.wait(60):
            try:
                self.sweep()
            except Exception:  # noqa: BLE001
                log.exception("sweep failed")

    def shutdown(self) -> None:
        self._stop.set()
        for job in list(self.jobs.values()):
            job.cancel.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)
