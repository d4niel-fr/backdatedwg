"""In-browser conversion (runs in Pyodide inside a Web Worker).

Static hosts such as Vercel can't run the conversion server, so the page
converts locally instead: a DWG is first turned into DXF by LibreDWG compiled
to WebAssembly, then this module downgrades it with ezdxf, exactly like the
server's fallback engine, and returns a DXF plus the report.
"""

from __future__ import annotations

import io
import json
from typing import Callable

from ezdxf import recover
from ezdxf.lldxf.const import DXFError

from . import report as rpt
from .converter import STEP_CONVERT, STEP_READ, STEP_REPORT, STEP_WRITE, ConversionError, _downgrade, _report_repairs, output_name
from .engines import CancelToken
from .versions import BY_YEAR, DetectError, detect, order


class _Sink:
    def __init__(self, emit: Callable):
        self.emit = emit

    def stage(self, index, lo, hi, expected_seconds):
        self.emit("stage", json.dumps({"index": index, "lo": lo, "hi": hi, "expected": expected_seconds}))

    def skipped(self, n):
        self.emit("skipped", json.dumps(n))

    def layers_blocks(self, n):
        self.emit("layers", json.dumps(n))


def convert(
    head: bytes,
    dxf: bytes,
    name: str,
    target_year: int,
    emit: Callable,
    read_warnings: int = 0,
) -> tuple[bytes, str]:
    """Convert DXF bytes. `head` is the start of the original upload (DWG or
    DXF) and is used to identify its version. Returns (output DXF, result JSON).
    """
    sink = _Sink(emit)
    try:
        det = detect(head, name)
    except DetectError as e:
        raise ConversionError("unsupported_type", str(e)) from e
    target = BY_YEAR[target_year]
    if det.version is None:
        raise ConversionError("unsupported_version", "This file's AutoCAD version isn't recognised.")
    if order(det.code) < order(target.code):
        raise ConversionError("already_older", f"This file is already in {det.version.label} format.")
    size_mb = len(dxf) / 1_000_000
    report = rpt.Report()
    cancel = CancelToken()

    sink.stage(STEP_READ, 0, 30, 1 + size_mb * 0.8)
    try:
        doc, auditor = recover.read(io.BytesIO(dxf))
    except (OSError, DXFError, ValueError, UnicodeError) as e:
        raise ConversionError("corrupt", f"The file is damaged and couldn't be read ({e}).") from e
    _report_repairs(report, auditor)
    if read_warnings:
        report.add("repaired", "Read warnings", "Source drawing", f"{read_warnings} recoverable read errors were ignored")
    sink.layers_blocks(rpt.count_layers_and_blocks(doc))
    report.add_feature_notes(doc, target)

    sink.stage(STEP_CONVERT, 30, 65, 0.5 + size_mb * 0.3)
    _downgrade(doc, target, det, report, sink, cancel)
    before = rpt.inventory(doc)
    sink.skipped(report.skipped)

    sink.stage(STEP_WRITE, 65, 90, 1 + size_mb * 0.8)
    stream = io.StringIO()
    try:
        doc.write(stream)
    except (DXFError, ValueError) as e:
        raise ConversionError("write_failed", f"Couldn't write the converted file ({e}).") from e
    out = stream.getvalue().encode(doc.output_encoding, errors="dxfreplace")
    try:
        after, _ = recover.read(io.BytesIO(out))
    except (OSError, DXFError, ValueError, UnicodeError) as e:
        raise ConversionError("write_failed", f"The converted file couldn't be verified ({e}).") from e

    sink.stage(STEP_REPORT, 90, 100, 0.3)
    report.add_diff(before, rpt.inventory(after), target)
    sink.skipped(report.skipped)

    out_name = output_name(name, target, "DXF")
    engine = "LibreDWG (WebAssembly) + ezdxf" if det.kind == "DWG" else "ezdxf"
    text = report.text(
        source_name=name,
        output_name=out_name,
        source_label=f"{det.kind}, {det.version.label}",
        target=target,
        engine=engine + ", in your browser",
    )
    result = {
        "outputName": out_name,
        "outputSize": len(out),
        "engine": engine,
        "counts": report.counts(),
        "items": [i.to_dict() for i in report.sorted_items()],
        "reportText": text,
    }
    return out, json.dumps(result)


def run_files(in_path: str, head_path: str, out_path: str, name: str, target_year: int, emit, read_warnings: int = 0) -> str:
    """Worker entry point: files in Pyodide's virtual FS in, result JSON out."""
    with open(head_path, "rb") as fh:
        head = fh.read()
    with open(in_path, "rb") as fh:
        dxf = fh.read()
    try:
        out, result = convert(head, dxf, name, int(target_year), emit, int(read_warnings))
    except ConversionError as e:
        return json.dumps({"error": {"code": e.code, "message": e.message}})
    except Exception as e:  # noqa: BLE001 - surfaced to the page as a failed job
        return json.dumps({"error": {"code": "internal", "message": f"Unexpected error while converting: {e}"}})
    with open(out_path, "wb") as fh:
        fh.write(out)
    return result
