"""Doing the same thing to many drawings: recipes, batch jobs, a watched folder, audits.

A **recipe** is a saved list of things to do, written the way you'd type them:

    {"name": "Office standard",
     "commands": ["purge unused layers", "delete duplicates", "standardize layers"],
     "ops": [{"op": "replace_fonts", "font": "arial.ttf"}],
     "output": {"format": "DWG", "target": 2013}}

Each file is opened in a real (temporary) editing session and every step goes
through the same validation and proposal/accept path as the editor, so a batch
can never do something the editor wouldn't. Steps that find nothing to do in a
particular file are recorded as skipped, not failures.

Recipes can also be recorded: ``recipe_from_log`` turns the changes accepted in
an editing session into a recipe to replay on other drawings.
"""

from __future__ import annotations

import io
import ipaddress
import json
import logging
import os
import shutil
import smtplib
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from .. import converter
from ..engines import CancelToken, Engines
from ..versions import BY_YEAR, TARGET_YEARS, DetectError, detect, order
from . import ops

log = logging.getLogger("backdate.editor.batch")

DRAWING_EXT = {".dwg", ".dxf"}
MAX_FILES = int(os.environ.get("BACKDATE_BATCH_MAX_FILES", "200"))
BATCH_AI_LIMIT = int(os.environ.get("BACKDATE_BATCH_AI_LIMIT", "20"))
RETENTION_SECONDS = int(os.environ.get("BACKDATE_RETENTION_SECONDS", "3600"))


class RecipeError(ValueError):
    pass


# ── recipes ─────────────────────────────────────────────────────────────────


def validate_recipe(recipe) -> dict:
    if isinstance(recipe, str):
        try:
            recipe = json.loads(recipe)
        except ValueError as e:
            raise RecipeError("The recipe isn't valid JSON.") from e
    if isinstance(recipe, list):
        recipe = {"commands": [c for c in recipe if isinstance(c, str)], "ops": [o for o in recipe if isinstance(o, dict)]}
    if not isinstance(recipe, dict):
        raise RecipeError("A recipe is an object with commands and/or ops.")
    commands = recipe.get("commands") or []
    op_list = recipe.get("ops") or []
    steps = recipe.get("steps") or []
    ai = str(recipe.get("ai_instruction") or "").strip()
    if not isinstance(commands, list) or not all(isinstance(c, str) and c.strip() for c in commands):
        raise RecipeError("'commands' must be a list of sentences.")
    if not isinstance(op_list, list) or not all(isinstance(o, dict) and "op" in o for o in op_list):
        raise RecipeError("'ops' must be a list of operations.")
    if not isinstance(steps, list) or not all(isinstance(s, dict) and isinstance(s.get("ops"), list) for s in steps):
        raise RecipeError("'steps' must be a list of {title, ops}.")
    for o in op_list + [o for s in steps for o in s["ops"]]:
        if o.get("op") not in ops.OPS:
            raise RecipeError(f"Unknown operation {o.get('op')!r} in the recipe.")
    if len(commands) + len(op_list) + len(steps) > 60:
        raise RecipeError("A recipe can have at most 60 items.")
    if not (commands or op_list or steps or ai):
        raise RecipeError("The recipe doesn't do anything.")
    out = recipe.get("output") or {}
    if not isinstance(out, dict):
        raise RecipeError("'output' must be an object like {\"format\": \"DXF\", \"target\": 2013}.")
    fmt = str(out.get("format") or "DXF").upper()
    if fmt not in ("DXF", "DWG"):
        raise RecipeError("Output format must be DXF or DWG.")
    target = out.get("target")
    if target is not None and target not in TARGET_YEARS:
        raise RecipeError(f"Output target must be one of {TARGET_YEARS}.")
    return {"name": str(recipe.get("name") or "Recipe")[:120], "commands": [c.strip()[:500] for c in commands], "ops": op_list,
            "steps": steps, "ai_instruction": ai[:2000], "output": {"format": fmt, "target": target}}


def recipe_from_log(name: str, log_entries: list[dict]) -> dict:
    """The accepted changes of an editing session, as a replayable recipe."""
    steps = []
    for e in log_entries:
        if e.get("source") == "history" or not e.get("ops"):
            continue
        selection_bound = any((o.get("selector") or {}).get("selection") or (o.get("selector") or {}).get("handles") for o in e["ops"] if isinstance(o.get("selector"), dict))
        steps.append({"title": (e.get("prompt") or "; ".join(e.get("summaries", [])))[:120],
                      "ops": [{**o, "optional": True} for o in e["ops"]],
                      **({"note": "used a selection; it only applies to the same objects in other files"} if selection_bound else {})})
    if not steps:
        raise RecipeError("Nothing has been changed yet, so there's nothing to record.")
    return {"name": f"Recorded from {name}"[:120], "commands": [], "ops": [], "steps": steps, "output": {"format": "DXF", "target": None}}


# ── one file ────────────────────────────────────────────────────────────────


class _Sink:
    def stage(self, *a):
        pass

    def skipped(self, n):
        pass

    def layers_blocks(self, n):
        pass


def load_drawing(path: Path, engines: Engines, work: Path):
    with path.open("rb") as fh:
        head = fh.read(64 * 1024)
    try:
        det = detect(head, path.name)
    except DetectError as e:
        raise RecipeError(str(e)) from e
    if det.kind == "DWG" and not engines.can_read_dwg:
        raise RecipeError("This server can't read DWG files (no ODA File Converter or LibreDWG).")
    try:
        doc, _aud, notes = converter._load(path, det, engines, work, CancelToken())
    except converter.ConversionError as e:
        raise RecipeError(e.message) from e
    return doc, det, notes


def process_file(path: Path, recipe: dict, engines: Engines, work: Path, model=None, dry_run: bool = False) -> dict:
    """Open one drawing, run the recipe through an editing session, write the result."""
    from .session import EditorSession
    from . import agent

    work.mkdir(parents=True, exist_ok=True)
    started = time.time()
    rec: dict = {"file": path.name, "status": "done", "applied": [], "skipped": [], "errors": []}
    try:
        doc, det, notes = load_drawing(path, engines, work / "read")
    except RecipeError as e:
        return {**rec, "status": "failed", "errors": [str(e)]}
    s = EditorSession(uuid.uuid4().hex, path.name, doc, work / "sessions", notes, det.kind)
    rec["healthBefore"] = s.health()["score"]

    def attempt(title: str, fn) -> None:
        try:
            prop = fn()
        except ops.OpError as e:
            rec["skipped"].append(f"{title}: {e}")
            return
        if prop is None:
            rec["skipped"].append(f"{title}: nothing to do")
            return
        s.accept(prop.id)
        rec["applied"].append({"step": title, "summaries": prop.summaries})

    for cmd in recipe["commands"]:
        plan = agent.local_plan(s, cmd)
        if plan and not plan[0]:
            rec["skipped"].append(f"{cmd}: nothing to do")
            continue
        if plan:
            attempt(cmd, lambda plan=plan: s.stage([], [], "batch", cmd, steps=plan[0]))
            continue
        parsed = agent.local_ops(cmd)
        if parsed is None:
            rec["errors"].append(f"{cmd}: not a built-in command (use ops, or ai_instruction for open-ended requests)")
            continue
        attempt(cmd, lambda parsed=parsed: s.stage([{**o, "optional": True} for o in parsed[0]], [], "batch", cmd))
    if recipe["ops"]:
        attempt("ops", lambda: s.stage([{**o, "optional": True} for o in recipe["ops"]], [], "batch", "ops"))
    for i, st in enumerate(recipe["steps"], 1):
        attempt(st.get("title") or f"step {i}", lambda st=st: s.stage([{**o, "optional": True} for o in st["ops"]], [], "batch", st.get("title")))
    if recipe.get("ai_instruction"):
        if model is None:
            rec["errors"].append("ai_instruction: the AI assistant isn't connected")
        else:
            reply = agent.run(s, recipe["ai_instruction"], [], model)
            if reply.proposal:
                attempt("AI: " + recipe["ai_instruction"][:60], lambda: s.proposals[reply.proposal["id"]])
            else:
                rec["skipped"].append(f"AI: {reply.reply[:200]}")

    rec["healthAfter"] = s.health()["score"]
    rec["changes"] = len(rec["applied"])
    if dry_run:
        rec["seconds"] = round(time.time() - started, 2)
        return rec
    out_dxf = s.write_current()
    out = recipe["output"]
    stem = path.stem
    if out["target"] is None and out["format"] == "DXF":
        final = work / "out" / f"{stem}.dxf"
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out_dxf, final)
    else:
        target = BY_YEAR[out["target"] or 2018]
        with out_dxf.open("rb") as fh:
            det_out = detect(fh.read(64 * 1024), out_dxf.name)
        if order(det_out.code) < order(target.code):
            target = det_out.version or target
        if out["format"] == "DWG" and not engines.can_write("DWG"):
            rec["errors"].append("DWG output needs ODA File Converter; saved as DXF instead.")
            out = {**out, "format": "DXF"}
        try:
            res = converter.run(out_dxf, f"{stem}.dxf", det_out, target, out["format"], engines, work / "convert", _Sink(), CancelToken())
        except converter.ConversionError as e:
            return {**rec, "status": "failed", "errors": rec["errors"] + [e.message]}
        final = work / "out" / f"{stem}.{out['format'].lower()}"
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(res.output), final)
        rec["converted"] = f"{target.label} {out['format']}"
    rec["output"] = final.name
    rec["seconds"] = round(time.time() - started, 2)
    shutil.rmtree(work / "sessions", ignore_errors=True)
    return rec


def report_text(recipe: dict, results: list[dict], dry_run: bool) -> str:
    lines = [f"Batch report: {recipe['name']}", "=" * 40, f"{'Dry run (no files changed)' if dry_run else 'Applied'} · {len(results)} file(s)", ""]
    for r in results:
        lines.append(f"{r['file']}: {r['status']}" + (f" → {r.get('output')}" if r.get("output") else "")
                     + (f"  health {r.get('healthBefore')} → {r.get('healthAfter')}" if "healthBefore" in r else ""))
        for a in r.get("applied", []):
            lines.append(f"  ✓ {a['step']}: " + "; ".join(a["summaries"]))
        for sk in r.get("skipped", []):
            lines.append(f"  – skipped {sk}")
        for er in r.get("errors", []):
            lines.append(f"  ! {er}")
        lines.append("")
    return "\n".join(lines)


# ── webhooks ────────────────────────────────────────────────────────────────


class WebhookError(ValueError):
    pass


def check_callback(url: str) -> str:
    """Refuse callbacks that could reach this server's own network (SSRF)."""
    p = urllib.parse.urlparse(url)
    allow_http = os.environ.get("BACKDATE_ALLOW_HTTP_CALLBACKS") == "1"
    if p.scheme not in (("https", "http") if allow_http else ("https",)) or not p.hostname:
        raise WebhookError("Callback URLs must be https://.")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise WebhookError("The callback host doesn't resolve.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified) and not allow_http:
            raise WebhookError("Callback URLs must point to a public address.")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def post_callback(url: str, payload: dict) -> bool:
    try:
        check_callback(url)
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "User-Agent": "Backdate.dwg-batch"}, method="POST")
        urllib.request.build_opener(_NoRedirect()).open(req, timeout=10).close()
        return True
    except Exception:  # noqa: BLE001 - a failed callback never fails the batch
        log.warning("batch callback to %s failed", url, exc_info=True)
        return False


# ── batch jobs ──────────────────────────────────────────────────────────────


@dataclass
class BatchJob:
    id: str
    dir: Path
    files: list[Path]
    recipe: dict
    dry_run: bool = False
    callback: Optional[str] = None
    status: str = "queued"  # queued | running | done | failed
    results: list[dict] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    zip_path: Optional[Path] = None
    report: str = ""
    model: object = None

    def view(self) -> dict:
        done = len(self.results)
        return {"id": self.id, "status": self.status, "recipe": self.recipe["name"], "dryRun": self.dry_run,
                "files": len(self.files), "done": done, "progress": round(100 * done / max(1, len(self.files))),
                "results": self.results,
                "downloadUrl": f"/api/editor/batch/{self.id}/download" if self.zip_path else None,
                "reportUrl": f"/api/editor/batch/{self.id}/report.txt" if self.report else None}


class BatchManager:
    def __init__(self, root: Path, engines: Engines, model_factory=None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.engines = engines
        self.model_factory = model_factory or (lambda: None)
        self.jobs: dict[str, BatchJob] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="batch")

    def new_dir(self) -> tuple[str, Path]:
        jid = uuid.uuid4().hex
        d = self.root / jid
        (d / "in").mkdir(parents=True)
        return jid, d

    def submit(self, job: BatchJob) -> BatchJob:
        self.sweep()
        with self.lock:
            self.jobs[job.id] = job
        self.pool.submit(self._run, job)
        return job

    def get(self, jid: str) -> Optional[BatchJob]:
        with self.lock:
            return self.jobs.get(jid)

    def _run(self, job: BatchJob) -> None:
        job.status = "running"
        model = (job.model or self.model_factory()) if job.recipe.get("ai_instruction") else None
        try:
            for i, f in enumerate(job.files):
                use_model = model if i < BATCH_AI_LIMIT else None
                try:
                    r = process_file(f, job.recipe, self.engines, job.dir / "work" / str(i), use_model, job.dry_run)
                except Exception as e:  # noqa: BLE001 - one bad file never stops the batch
                    log.exception("batch file failed")
                    r = {"file": f.name, "status": "failed", "errors": [f"Unexpected error: {e}"], "applied": [], "skipped": []}
                job.results.append(r)
            job.report = report_text(job.recipe, job.results, job.dry_run)
            if not job.dry_run:
                zp = job.dir / f"{job.recipe['name'][:40].replace('/', '_') or 'batch'}.zip"
                with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
                    for i, r in enumerate(job.results):
                        if r.get("output"):
                            z.write(job.dir / "work" / str(i) / "out" / r["output"], f"output/{r['output']}")
                    z.writestr("report.txt", job.report)
                    z.writestr("recipe.json", json.dumps(job.recipe, indent=2))
                job.zip_path = zp
            job.status = "done"
        except Exception:  # noqa: BLE001
            log.exception("batch failed")
            job.status = "failed"
        finally:
            job.finished = time.time()
            for f in job.files:
                f.unlink(missing_ok=True)
            if job.callback:
                post_callback(job.callback, {"event": "batch.finished", **{k: v for k, v in job.view().items() if k != "results"},
                                             "summary": [{k: r.get(k) for k in ("file", "status", "changes", "output", "healthBefore", "healthAfter")} for r in job.results]})

    def sweep(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        with self.lock:
            dead = [j for j in self.jobs.values() if j.finished and now - j.finished > RETENTION_SECONDS]
            for j in dead:
                self.jobs.pop(j.id, None)
                shutil.rmtree(j.dir, ignore_errors=True)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


# ── watched folder and audits (for self-hosted servers and the CLI) ─────────


def watch_once(folder: Path, recipe: dict, engines: Engines) -> list[dict]:
    """Process every drawing waiting in folder/in; results go to out/, originals to done/."""
    inbox, outbox, done = folder / "in", folder / "out", folder / "done"
    for d in (inbox, outbox, done):
        d.mkdir(parents=True, exist_ok=True)
    results = []
    for f in sorted(inbox.iterdir()):
        if not f.is_file() or f.suffix.lower() not in DRAWING_EXT:
            continue
        if time.time() - f.stat().st_mtime < 5:  # still being copied in
            continue
        work = folder / ".work" / uuid.uuid4().hex
        try:
            r = process_file(f, recipe, engines, work)
            if r.get("output"):
                shutil.move(str(work / "out" / r["output"]), outbox / r["output"])
            (outbox / f"{f.stem}_report.txt").write_text(report_text(recipe, [r], False), "utf-8")
            shutil.move(str(f), done / f.name)
            results.append(r)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return results


class FolderWatcher(threading.Thread):
    def __init__(self, folder: Path, recipe: dict, engines: Engines, interval: float = 30.0):
        super().__init__(daemon=True, name="hot-folder")
        self.folder, self.recipe, self.engines, self.interval = folder, recipe, engines, interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        log.info("watching %s", self.folder)
        while not self.stop_event.is_set():
            try:
                for r in watch_once(self.folder, self.recipe, self.engines):
                    log.info("hot folder: %s %s", r["file"], r["status"])
            except Exception:  # noqa: BLE001
                log.exception("hot folder pass failed")
            self.stop_event.wait(self.interval)


def start_watcher_from_env(engines: Engines) -> Optional[FolderWatcher]:
    folder = os.environ.get("BACKDATE_WATCH_DIR")
    recipe_path = os.environ.get("BACKDATE_WATCH_RECIPE")
    if not folder or not recipe_path:
        return None
    try:
        recipe = validate_recipe(Path(recipe_path).read_text("utf-8"))
    except (OSError, RecipeError) as e:
        log.error("hot folder disabled: %s", e)
        return None
    w = FolderWatcher(Path(folder), recipe, engines, float(os.environ.get("BACKDATE_WATCH_INTERVAL", "30")))
    w.start()
    return w


def audit_folder(folder: Path, engines: Engines) -> tuple[list[dict], str, str]:
    """Health-check every drawing in a folder. Returns (rows, text report, csv)."""
    from .geometry import extract
    from .digest import build
    from .health import check
    from .units import detect_units

    rows = []
    for f in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in DRAWING_EXT):
        work = folder / ".audit" / uuid.uuid4().hex
        try:
            doc, _det, _n = load_drawing(f, engines, work)
            scene = extract(doc)
            ext = scene.extents
            units = detect_units(doc.header.get("$INSUNITS", 0), max(ext[2] - ext[0], ext[3] - ext[1]) if ext else None)
            h = check(doc, scene, units, build(doc, scene, units, f.name))
            rows.append({"file": str(f.relative_to(folder)), "score": h["score"], "issues": h["issues"], "top": [x["title"] for x in h["findings"][:5]]})
        except RecipeError as e:
            rows.append({"file": str(f.relative_to(folder)), "score": None, "issues": None, "top": [f"Couldn't read: {e}"]})
        finally:
            shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(folder / ".audit", ignore_errors=True)
    rows.sort(key=lambda r: (r["score"] is None, r["score"] if r["score"] is not None else 0))
    scored = [r["score"] for r in rows if r["score"] is not None]
    text = [f"Drawing audit of {folder}", f"{len(rows)} drawing(s), average health {round(sum(scored) / len(scored)) if scored else '–'}/100", ""]
    for r in rows:
        text.append(f"{r['score'] if r['score'] is not None else '  ?'}/100  {r['file']}" + (f"  ({r['issues']} issues)" if r["issues"] else ""))
        text += [f"        - {t}" for t in r["top"]]
    buf = io.StringIO()
    import csv

    w = csv.writer(buf)
    w.writerow(["File", "Score", "Issues", "Top findings"])
    for r in rows:
        w.writerow([r["file"], r["score"], r["issues"], " | ".join(r["top"])])
    return rows, "\n".join(text) + "\n", buf.getvalue()


def send_email(to: str, subject: str, body: str, attachments: Optional[dict[str, bytes]] = None) -> None:
    """Send through SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/SMTP_FROM (STARTTLS)."""
    host = os.environ.get("SMTP_HOST")
    if not host:
        raise RuntimeError("Set SMTP_HOST (and SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM) to send email.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "backdate@localhost"
    msg["To"] = to
    msg.set_content(body)
    for name, data in (attachments or {}).items():
        msg.add_attachment(data, maintype="text", subtype="csv" if name.endswith(".csv") else "plain", filename=name)
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        if port != 25:
            smtp.starttls()
        if os.environ.get("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(msg)
