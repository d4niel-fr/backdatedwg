"""The conversion pipeline: read -> convert -> write -> report.

With ODA File Converter installed the original file is converted directly
(DWG -> older DWG/DXF), and both sides are read back into ezdxf to work out
what was skipped. Without it, the drawing is downgraded in-process with ezdxf
(DWG input is first read through LibreDWG): entities that don't exist in the
target version are removed and listed, and the result is written as DXF.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ezdxf import recover
from ezdxf.document import Drawing
from ezdxf.lldxf.const import DXFError

from . import report as rpt
from .engines import CancelToken, Engines, EngineError
from .versions import Detected, FormatVersion, order

STEP_READ, STEP_CONVERT, STEP_WRITE, STEP_REPORT = 1, 2, 3, 4


class ConversionError(Exception):
    """Whole-job failure (as opposed to individual skipped entities)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Sink(Protocol):
    def stage(self, index: int, lo: float, hi: float, expected_seconds: float) -> None: ...

    def skipped(self, n: int) -> None: ...

    def layers_blocks(self, n: int) -> None: ...


@dataclass
class Result:
    output: Path
    output_name: str
    report: rpt.Report
    report_text: str
    engine: str


def output_name(original: str, target: FormatVersion, out_format: str) -> str:
    stem = Path(original).stem or "drawing"
    return f"{stem}_{target.year}.{out_format.lower()}"


def run(
    src: Path,
    original_name: str,
    detected: Detected,
    target: FormatVersion,
    out_format: str,
    engines: Engines,
    work: Path,
    sink: Sink,
    cancel: CancelToken,
) -> Result:
    size_mb = src.stat().st_size / 1_000_000
    pass_seconds = 2.0 + size_mb * 0.6  # rough time for one engine pass
    report = rpt.Report()
    use_oda = engines.oda is not None

    # 1 · read the source into ezdxf, for analysis (and, without ODA, editing)
    sink.stage(STEP_READ, 0, 30, pass_seconds if detected.kind == "DWG" else 1 + size_mb * 0.3)
    doc, auditor, read_notes = _load(src, detected, engines, work / "read", cancel)
    if not use_oda:
        # Only the fallback writes this ezdxf document out, so only then are
        # its repairs part of the result. ODA runs its own audit (step 2).
        _report_repairs(report, auditor)
    for note in read_notes:
        report.add("repaired", "Read warnings", "Source drawing", note)
    sink.layers_blocks(rpt.count_layers_and_blocks(doc))
    report.add_feature_notes(doc, target)
    cancel.check()

    # 2 · convert
    sink.stage(STEP_CONVERT, 30, 65, pass_seconds)
    if use_oda:
        before = rpt.inventory(doc)
        produced, log = _engine(lambda: engines.oda.convert(src, work / "convert", target.oda, out_format, cancel))
        report.add_log("repaired", "Audit", log)
        engine_label = engines.oda.name
    else:
        _downgrade(doc, target, detected, report, sink, cancel)
        before = rpt.inventory(doc)
        engine_label = "LibreDWG + ezdxf" if detected.kind == "DWG" else "ezdxf"
    sink.skipped(report.skipped)
    cancel.check()

    # 3 · write (fallback) and read the result back to verify it
    sink.stage(STEP_WRITE, 65, 90, pass_seconds)
    if use_oda:
        after_doc = _read_back(produced, out_format, target, engines, work / "verify", cancel)
    else:
        produced, after_doc = _write_fallback(doc, out_format, work / "write", cancel)
    cancel.check()

    # 4 · report
    sink.stage(STEP_REPORT, 90, 100, 0.5)
    report.add_diff(before, rpt.inventory(after_doc), target)
    sink.skipped(report.skipped)

    name = output_name(original_name, target, out_format)
    final = work / "result" / name
    final.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(produced), final)
    source_label = detected.version.label if detected.version else "unknown version"
    text = report.text(
        source_name=original_name,
        output_name=name,
        source_label=f"{detected.kind}, {source_label}",
        target=target,
        engine=engine_label,
    )
    for d in ("read", "convert", "verify", "write"):
        shutil.rmtree(work / d, ignore_errors=True)
    return Result(final, name, report, text, engine_label)


# ── steps ──────────────────────────────────────────────────────────────────


def _engine(fn):
    try:
        return fn()
    except EngineError as e:
        raise ConversionError("engine_failed", str(e)) from e


def _load(src: Path, det: Detected, engines: Engines, work: Path, cancel: CancelToken):
    work.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    if det.kind == "DXF":
        path = src
    elif engines.oda:
        ver = det.version.oda if det.version and order(det.code) >= order("AC1015") else "ACAD2018"
        try:
            path, _ = engines.oda.convert(src, work, ver, "DXF", cancel, audit=True)
        except EngineError as e:
            if e.broken:
                raise ConversionError("engine_failed", str(e)) from e
            raise ConversionError("corrupt", f"The DWG file couldn't be read. It may be damaged. ({e})") from e
    elif engines.libredwg:
        path = work / (src.stem + ".dxf")
        try:
            warnings = engines.libredwg.dwg_to_dxf(src, path, det.code, cancel)
        except EngineError as e:
            raise ConversionError("corrupt", f"The DWG file couldn't be read. It may be damaged. ({e})") from e
        if warnings:
            notes.append(f"{len(warnings)} recoverable read errors were ignored")
    else:
        raise ConversionError("no_engine", "Reading DWG files needs ODA File Converter or LibreDWG on the server.")
    cancel.check()
    try:
        doc, auditor = recover.readfile(str(path))
    except (OSError, DXFError, ValueError, UnicodeError) as e:
        raise ConversionError("corrupt", f"The file is damaged and couldn't be read ({e}).") from e
    return doc, auditor, notes


def _report_repairs(report: rpt.Report, auditor) -> None:
    # recover() fixes what it can; leftover errors are reported, not fatal.
    for entries, verdict in ((auditor.fixes, "Fixed automatically"), (auditor.errors, "Left as-is")):
        grouped: dict[str, list] = {}
        for f in entries:
            grouped.setdefault(_humanize(f.code), []).append(f)
        for label, items in grouped.items():
            example = items[0].message.strip()
            report.add("repaired", label, "Source drawing", f"{verdict}. E.g. {example}"[:220], len(items))


_AUDIT_NAMES = {
    "INVALID_OWNER_HANDLE": "Broken object reference",
    "UNDEFINED_LINETYPE": "Missing linetype",
    "UNDEFINED_TEXT_STYLE": "Missing text style",
    "UNDEFINED_DIMENSION_STYLE": "Missing dimension style",
    "UNDEFINED_BLOCK": "Missing block definition",
    "INVALID_LAYER_NAME": "Invalid layer name",
    "INVALID_COLOR_INDEX": "Invalid color",
    "INVALID_VERTEX_COUNT": "Damaged geometry",
    "INVALID_DICTIONARY_ENTRY": "Damaged dictionary entry",
}


def _humanize(code) -> str:
    name = getattr(code, "name", str(code))
    return _AUDIT_NAMES.get(name) or name.replace("_", " ").capitalize()


def _downgrade(doc: Drawing, target: FormatVersion, det: Detected, report: rpt.Report, sink: Sink, cancel: CancelToken):
    """Remove what the target version can't hold (fallback engine only)."""
    same_version = det.code == target.code
    for label, container in rpt.containers(doc, include_anonymous=True):
        doomed = []
        for e in container:
            t = e.dxftype()
            if rpt.newer_than(t, target):
                doomed.append((e, rpt.reason_for(t, target)))
            elif not same_version and (t == "ACAD_PROXY_ENTITY" or _is_unknown(e)):
                doomed.append((e, "Custom object from an add-on; can't be rewritten for an older version"))
        for e, reason in doomed:
            report.add("skipped", rpt.entity_name(e.dxftype()), rpt.where(label, e.dxf.get("layer", "0")), reason)
            container.delete_entity(e)
        if doomed:
            sink.skipped(report.skipped)
            cancel.check()

    if order(target.code) < order("AC1024"):
        for e in doc.entitydb.values():
            if e.dxf.hasattr("transparency"):
                e.dxf.discard("transparency")
    if order(target.code) < order("AC1018"):
        for e in doc.entitydb.values():
            if e.dxf.hasattr("true_color"):
                e.dxf.discard("true_color")
    doc.dxfversion = target.code


def _is_unknown(entity) -> bool:
    # ezdxf keeps entities it doesn't understand as raw tag storage.
    return type(entity).__name__ == "DXFTagStorage"


def _write_fallback(doc: Drawing, out_format: str, work: Path, cancel: CancelToken):
    if out_format != "DXF":
        raise ConversionError("no_engine", "Saving as DWG needs ODA File Converter on the server. Choose DXF instead.")
    work.mkdir(parents=True, exist_ok=True)
    dxf_path = work / "out.dxf"
    try:
        doc.saveas(dxf_path)
    except (DXFError, ValueError) as e:
        raise ConversionError("write_failed", f"Couldn't write the converted file ({e}).") from e
    cancel.check()
    return dxf_path, _read_dxf(dxf_path, "write_failed")


def _read_back(produced: Path, out_format: str, target: FormatVersion, engines: Engines, work: Path, cancel: CancelToken) -> Drawing:
    if out_format == "DXF":
        return _read_dxf(produced, "write_failed")
    work.mkdir(parents=True, exist_ok=True)
    dxf, _ = _engine(lambda: engines.oda.convert(produced, work, target.oda, "DXF", cancel, audit=False))
    return _read_dxf(dxf, "write_failed")


def _read_dxf(path: Path, code: str) -> Drawing:
    try:
        doc, _ = recover.readfile(str(path))
        return doc
    except (OSError, DXFError, ValueError, UnicodeError) as e:
        raise ConversionError(code, f"The converted file couldn't be verified ({e}).") from e

