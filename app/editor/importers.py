"""Bringing other things in: PDF floor plans, sketch photos, rack-designer projects, tables.

* ``pdf_to_dxf``     vector PDF (from CAD or a plotter) → lines, curves and text.
* ``sketch_to_dxf``  a photo of a hand sketch or a raster plan, traced by a vision model.
* ``wdp_to_dxf``     a Warehouse Designer Pro project (.wdp, Custom Builder) → DXF.
* ``parse_table``    CSV or Excel (.xlsx) → columns and rows, for a drawn table or a title block.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import math
import re
import struct
import zipfile
from typing import Optional
from xml.etree import ElementTree as ET

import ezdxf
from ezdxf.document import Drawing
from ezdxf.enums import TextEntityAlignment
from ezdxf.math import ConstructionArc, Vec2


class ImportError_(ValueError):
    """An import that can't be done; the message is for people."""


MAX_PDF_SEGMENTS = 300_000
MAX_TABLE_ROWS = 2000
MAX_TABLE_COLS = 60


# ── tables ──────────────────────────────────────────────────────────────────


def parse_table(data: bytes, filename: str) -> dict:
    name = filename.lower()
    if name.endswith(".xlsx") or data[:2] == b"PK":
        rows = _xlsx_rows(data)
    elif name.endswith((".csv", ".txt", ".tsv")) or not name:
        rows = _csv_rows(data)
    else:
        raise ImportError_("Tables can be read from .csv or .xlsx files.")
    rows = [[str(c).strip() for c in r[:MAX_TABLE_COLS]] for r in rows if any(str(c).strip() for c in r)][:MAX_TABLE_ROWS + 1]
    if not rows:
        raise ImportError_("That table is empty.")
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header, body = rows[0], rows[1:]
    columns = [h or f"Column {i + 1}" for i, h in enumerate(header)]
    return {"columns": columns, "rows": body, "count": len(body)}


def _csv_rows(data: bytes) -> list[list[str]]:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text), dialect))


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref.upper())
    n = 0
    for ch in letters.group(0) if letters else "A":
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element:
    info = z.getinfo(name)
    if info.file_size > 30 * 1024 * 1024:
        raise ImportError_("That spreadsheet is too large.")
    raw = z.read(name)
    if b"<!DOCTYPE" in raw[:2000] or b"<!ENTITY" in raw[:2000]:
        raise ImportError_("That spreadsheet contains unsupported XML.")
    return ET.fromstring(raw)


def _xlsx_rows(data: bytes) -> list[list[str]]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ImportError_("That isn't a valid .xlsx file.") from e
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
          "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}
    names = set(z.namelist())
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in _xml(z, "xl/sharedStrings.xml").findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    sheet_path = "xl/worksheets/sheet1.xml"
    if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
        wb = _xml(z, "xl/workbook.xml")
        first = wb.find("m:sheets/m:sheet", ns)
        rels = _xml(z, "xl/_rels/workbook.xml.rels")
        if first is not None:
            rid = first.get("{%s}id" % ns["r"])
            for rel in rels.findall("pr:Relationship", ns):
                if rel.get("Id") == rid:
                    target = rel.get("Target", "")
                    sheet_path = target.lstrip("/") if target.startswith("/") else "xl/" + target
    if sheet_path not in names:
        raise ImportError_("That spreadsheet has no readable sheet.")
    out: list[list[str]] = []
    for row in _xml(z, sheet_path).iter("{%s}row" % ns["m"]):
        cells: dict[int, str] = {}
        for c in row.findall("m:c", ns):
            idx = _col_index(c.get("r", "A1"))
            if idx >= MAX_TABLE_COLS:
                continue
            t = c.get("t")
            v = c.find("m:v", ns)
            if t == "s" and v is not None:
                try:
                    cells[idx] = shared[int(v.text or 0)]
                except (ValueError, IndexError):
                    cells[idx] = ""
            elif t == "inlineStr":
                cells[idx] = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
            elif t == "b" and v is not None:
                cells[idx] = "TRUE" if v.text == "1" else "FALSE"
            elif v is not None:
                text = v.text or ""
                try:
                    f = float(text)
                    text = str(int(f)) if f.is_integer() and abs(f) < 1e15 else f"{f:g}"
                except ValueError:
                    pass
                cells[idx] = text
        if cells:
            out.append([cells.get(i, "") for i in range(max(cells) + 1)])
        if len(out) > MAX_TABLE_ROWS + 1:
            break
    return out


# ── Warehouse Designer Pro projects ─────────────────────────────────────────


def _rect(cx: float, cy: float, w: float, h: float, rot: float) -> list[tuple[float, float]]:
    c, s = math.cos(rot), math.sin(rot)
    pts = []
    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        pts.append((cx + dx * c - dy * s, cy + dx * s + dy * c))
    return pts


def _num(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def wdp_to_dxf(data: bytes) -> tuple[Drawing, str, list[str]]:
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ImportError_("That isn't a Warehouse Designer Pro project (.wdp) file.") from e
    if isinstance(obj, dict) and obj.get("format") == "wdp" and isinstance(obj.get("state"), dict):
        kind, name, state = obj.get("kind") or "quick", str(obj.get("name") or "Warehouse project"), obj["state"]
    elif isinstance(obj, dict) and "walls" in obj and "racks" in obj:
        kind, name, state = "builder", "Warehouse project", obj
    else:
        raise ImportError_("That isn't a Warehouse Designer Pro project (.wdp) file.")
    if kind != "builder":
        raise ImportError_("This is a Quick Layout project. Open it in Warehouse Designer Pro and export a DXF, or use a Custom Builder project.")

    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 6  # the designer works in metres
    for layer, color in (("A-WALL", 7), ("S-COLS", 8), ("A-AREA", 3), ("A-DOOR", 2), ("A-EQPM-RACK", 5), ("A-EQPM", 30),
                         ("A-ANNO-DIMS", 1), ("A-ANNO-TEXT", 7)):
        doc.layers.add(layer, color=color)
    msp = doc.modelspace()
    notes: list[str] = []
    counts = {"walls": 0, "racks": 0, "columns": 0, "zones": 0, "doors": 0, "dims": 0, "other": 0}

    for wl in state.get("walls") or []:
        pts = [(_num(p.get("x")), _num(p.get("y"))) for p in (wl.get("pts") or []) if isinstance(p, dict)]
        if len(pts) < 2:
            continue
        kind_ = wl.get("kind", "poly")
        if kind_ == "arc" and len(pts) == 3:
            try:
                arc = ConstructionArc.from_3p(Vec2(pts[0]), Vec2(pts[2]), Vec2(pts[1]))
                arc.add_to_layout(msp, dxfattribs={"layer": "A-WALL"})
            except Exception:  # noqa: BLE001 - collinear points: draw it straight
                msp.add_lwpolyline(pts, dxfattribs={"layer": "A-WALL"})
        elif kind_ == "spline" and len(pts) >= 3:
            msp.add_spline(fit_points=[(x, y, 0) for x, y in pts], dxfattribs={"layer": "A-WALL"})
        else:
            msp.add_lwpolyline(pts, close=bool(wl.get("closed")), dxfattribs={"layer": "A-WALL"})
        counts["walls"] += 1

    for c in state.get("columns") or []:
        msp.add_lwpolyline(_rect(_num(c.get("x")), _num(c.get("y")), _num(c.get("w"), 0.4), _num(c.get("h"), 0.4), 0), close=True, dxfattribs={"layer": "S-COLS"})
        counts["columns"] += 1

    for z in state.get("zones") or []:
        cx, cy, w, h, rot = _num(z.get("cx")), _num(z.get("cy")), _num(z.get("w"), 1), _num(z.get("h"), 1), _num(z.get("rot"))
        msp.add_lwpolyline(_rect(cx, cy, w, h, rot), close=True, dxfattribs={"layer": "A-AREA"})
        if z.get("name"):
            t = msp.add_text(str(z["name"])[:80], height=max(min(w, h) * 0.12, 0.2), dxfattribs={"layer": "A-ANNO-TEXT", "rotation": math.degrees(rot)})
            t.set_placement((cx, cy), align=TextEntityAlignment.MIDDLE_CENTER)
        counts["zones"] += 1

    for d in state.get("doors") or []:
        x, y, w, rot = _num(d.get("x")), _num(d.get("y")), _num(d.get("w"), 3), _num(d.get("rot"))
        c, s = math.cos(rot), math.sin(rot)
        p = lambda dx, dy: (x + dx * c - dy * s, y + dx * s + dy * c)  # noqa: E731
        msp.add_line(p(-w / 2, 0), p(w / 2, 0), dxfattribs={"layer": "A-DOOR"})
        hinge = p(-w / 2, 0)
        msp.add_arc(hinge, w, math.degrees(rot), math.degrees(rot) + 90, dxfattribs={"layer": "A-DOOR"})
        counts["doors"] += 1

    types = state.get("rackTypes") or {}
    blocks: dict[str, str] = {}
    for r in state.get("racks") or []:
        cx, cy, w, h, rot = _num(r.get("cx")), _num(r.get("cy")), _num(r.get("w"), 1), _num(r.get("h"), 1), _num(r.get("rot"))
        kind_ = r.get("kind") or "rack"
        if kind_ == "rack" and r.get("type"):
            letter = re.sub(r"[^A-Za-z0-9]", "", str(r["type"]))[:3] or "X"
            key = f"RACK_{letter}_{round(w * 1000)}x{round(h * 1000)}"
            if key not in blocks:
                blk = doc.blocks.new(key)
                blk.add_lwpolyline(_rect(0, 0, w, h, 0), close=True)
                blk.add_line((-w / 2, -h / 2), (w / 2, h / 2))
                blk.add_line((-w / 2, h / 2), (w / 2, -h / 2))
                blk.add_attdef("TYPE", (-w / 2 + 0.05, -h / 2 + 0.05), dxfattribs={"height": min(h * 0.3, 0.25)})
                blocks[key] = key
            ins = msp.add_blockref(key, (cx, cy), dxfattribs={"layer": "A-EQPM-RACK", "rotation": math.degrees(rot)})
            ins.add_auto_attribs({"TYPE": letter})
            counts["racks"] += 1
        else:
            msp.add_lwpolyline(_rect(cx, cy, w, h, rot), close=True, dxfattribs={"layer": "A-EQPM"})
            t = msp.add_text(str(kind_).upper()[:20], height=max(min(w, h) * 0.25, 0.1), dxfattribs={"layer": "A-EQPM", "rotation": math.degrees(rot)})
            t.set_placement((cx, cy), align=TextEntityAlignment.MIDDLE_CENTER)
            counts["other"] += 1

    for dm in state.get("dims") or []:
        a, b = (_num(dm.get("ax")), _num(dm.get("ay"))), (_num(dm.get("bx")), _num(dm.get("by")))
        if a == b:
            continue
        try:
            msp.add_aligned_dim(p1=a, p2=b, distance=_num(dm.get("off"), 1.0) or 1.0, dimstyle="EZDXF",
                                override={"dimtxt": 0.25, "dimasz": 0.15, "dimexe": 0.1, "dimexo": 0.1, "dimlfac": 1, "dimdec": 2},
                                dxfattribs={"layer": "A-ANNO-DIMS"}).render()
            counts["dims"] += 1
        except Exception:  # noqa: BLE001
            continue

    if types:
        rows = [["TYPE", "BAY (m)", "DEPTH (m)", "HEIGHT (m)", "LEVELS"]]
        for letter, t in sorted(types.items()):
            rows.append([letter, f"{_num(t.get('bay')):g}", f"{_num(t.get('depth')):g}", f"{_num(t.get('height')):g}", str(t.get("shelves", ""))])
        notes.append("Rack types: " + ", ".join(f"{r[0]} {r[1]}×{r[2]} m" for r in rows[1:]))
    if state.get("underlay"):
        notes.append("The project's underlay image wasn't imported.")
    if not any(counts.values()):
        raise ImportError_("That project is empty.")
    notes.insert(0, "Imported from Warehouse Designer Pro: " + ", ".join(f"{v} {k}" for k, v in counts.items() if v) + ".")
    return doc, re.sub(r"[^\w\- .()]+", "", name).strip() or "Warehouse project", notes


# ── vector PDF ──────────────────────────────────────────────────────────────


def _mul(m1: list[float], m2: list[float]) -> list[float]:
    a, b, c, d, e, f = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [a * a2 + b * c2, a * b2 + b * d2, c * a2 + d * c2, c * b2 + d * d2, e * a2 + f * c2 + e2, e * b2 + f * d2 + f2]


def _apply(m: list[float], x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _bezier(p0, p1, p2, p3, n: int = 10) -> list[tuple[float, float]]:
    out = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        out.append((mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t ** 3 * p3[0],
                    mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def _hex(rgb) -> str:
    return "%02X%02X%02X" % tuple(max(0, min(255, int(round(float(v) * 255)))) for v in rgb)


def pdf_to_dxf(data: bytes, page_number: int = 1, scale: float = 1.0) -> tuple[Drawing, list[str]]:
    """Lines, curves and text from one page of a vector PDF.

    Output is in millimetres: paper millimetres times ``scale`` (so a 1:100
    plan imported with scale 100 comes out at real size).
    """
    try:
        import pypdf
        from pypdf.generic import ContentStream
    except ImportError as e:  # pragma: no cover - pypdf is in requirements.txt
        raise ImportError_("PDF import needs the pypdf package on the server.") from e
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = reader.pages
        if not 1 <= page_number <= len(pages):
            raise ImportError_(f"That PDF has {len(pages)} page{'s' if len(pages) != 1 else ''}.")
        page = pages[page_number - 1]
    except ImportError_:
        raise
    except Exception as e:  # noqa: BLE001 - any parser failure is "can't read it"
        raise ImportError_(f"That PDF couldn't be read ({type(e).__name__}).") from e
    if scale <= 0 or scale > 100000:
        raise ImportError_("Scale must be between 0 and 100,000.")
    k = 25.4 / 72 * scale  # PDF points → millimetres

    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    notes: list[str] = []
    state = {"segments": 0, "images": 0, "paths": 0, "truncated": False}
    layers: set[str] = set()

    def layer_for(color: str, filled: bool) -> str:
        name = f"PDF-{'FILL' if filled else 'LINE'}-{color}"
        if name not in layers:
            layers.add(name)
            if not doc.layers.has_entry(name):
                lay = doc.layers.add(name)
                lay.rgb = tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))
        return name

    def run(ops, resources, ctm0: list[float], depth: int) -> None:
        ctm = list(ctm0)
        stack: list = []
        stroke, fill = "000000", "000000"
        subpaths: list[list[tuple[float, float]]] = []
        closed: list[bool] = []
        cur: list[tuple[float, float]] = []
        start = (0.0, 0.0)

        def flush(paint_stroke: bool, paint_fill: bool) -> None:
            nonlocal subpaths, closed, cur
            if cur:
                subpaths.append(cur)
                closed.append(False)
            if paint_stroke or paint_fill:
                lay = layer_for(stroke if paint_stroke else fill, not paint_stroke)
                for pts, cl in zip(subpaths, closed):
                    if len(pts) < 2 or state["truncated"]:
                        continue
                    state["segments"] += len(pts)
                    if state["segments"] > MAX_PDF_SEGMENTS:
                        state["truncated"] = True
                        break
                    world = [(x * k, y * k) for x, y in (_apply(ctm, px, py) for px, py in pts)]
                    msp.add_lwpolyline(world, close=cl, dxfattribs={"layer": lay})
                    state["paths"] += 1
            subpaths, closed, cur = [], [], []

        for operands, op in ops:
            op = op.decode("latin-1") if isinstance(op, bytes) else str(op)
            try:
                nums = [float(v) for v in operands] if op in ("m", "l", "c", "v", "y", "re", "cm", "RG", "rg", "G", "g", "K", "k") else []
            except (TypeError, ValueError):
                continue
            if op == "q":
                stack.append((list(ctm), stroke, fill))
            elif op == "Q" and stack:
                ctm, stroke, fill = stack.pop()
            elif op == "cm" and len(nums) == 6:
                ctm = _mul(nums, ctm)
            elif op == "m" and len(nums) == 2:
                if cur:
                    subpaths.append(cur)
                    closed.append(False)
                cur = [(nums[0], nums[1])]
                start = (nums[0], nums[1])
            elif op == "l" and len(nums) == 2 and cur:
                cur.append((nums[0], nums[1]))
            elif op == "c" and len(nums) == 6 and cur:
                cur += _bezier(cur[-1], (nums[0], nums[1]), (nums[2], nums[3]), (nums[4], nums[5]))
            elif op == "v" and len(nums) == 4 and cur:
                cur += _bezier(cur[-1], cur[-1], (nums[0], nums[1]), (nums[2], nums[3]))
            elif op == "y" and len(nums) == 4 and cur:
                cur += _bezier(cur[-1], (nums[0], nums[1]), (nums[2], nums[3]), (nums[2], nums[3]))
            elif op == "re" and len(nums) == 4:
                if cur:
                    subpaths.append(cur)
                    closed.append(False)
                x, y, w, h = nums
                subpaths.append([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])
                closed.append(True)
                cur = []
            elif op == "h" and cur:
                if cur[-1] != start:
                    cur.append(start)
                subpaths.append(cur)
                closed.append(True)
                cur = []
            elif op in ("S", "s"):
                if op == "s" and cur:
                    cur.append(start)
                flush(True, False)
            elif op in ("f", "F", "f*"):
                flush(False, True)
            elif op in ("B", "B*", "b", "b*"):
                flush(True, True)
            elif op == "n":
                subpaths, closed, cur = [], [], []
            elif op == "RG" and len(nums) == 3:
                stroke = _hex(nums)
            elif op == "rg" and len(nums) == 3:
                fill = _hex(nums)
            elif op == "G" and len(nums) == 1:
                stroke = _hex(nums * 3)
            elif op == "g" and len(nums) == 1:
                fill = _hex(nums * 3)
            elif op == "K" and len(nums) == 4:
                c_, m_, y_, k_ = nums
                stroke = _hex(((1 - c_) * (1 - k_), (1 - m_) * (1 - k_), (1 - y_) * (1 - k_)))
            elif op == "Do" and operands and depth < 6:
                try:
                    xobjs = resources.get("/XObject") if resources else None
                    xo = xobjs[operands[0]].get_object() if xobjs and operands[0] in xobjs else None
                except Exception:  # noqa: BLE001
                    xo = None
                if xo is None:
                    continue
                if xo.get("/Subtype") == "/Form":
                    m = [float(v) for v in xo.get("/Matrix", [1, 0, 0, 1, 0, 0])]
                    try:
                        sub = ContentStream(xo, reader)
                        run(sub.operations, xo.get("/Resources") or resources, _mul(m, ctm), depth + 1)
                    except Exception:  # noqa: BLE001
                        continue
                elif xo.get("/Subtype") == "/Image":
                    state["images"] += 1
            if state["truncated"]:
                break

    try:
        content = page.get_contents()
        if content is not None:
            ops = ContentStream(content, reader).operations
            run(ops, page.get("/Resources"), [1, 0, 0, 1, 0, 0], 0)
    except Exception as e:  # noqa: BLE001
        raise ImportError_(f"That PDF page couldn't be read ({type(e).__name__}).") from e

    texts = 0
    if not doc.layers.has_entry("PDF-TEXT"):
        doc.layers.add("PDF-TEXT")

    def visitor(text, cm, tm, font_dict, font_size):
        nonlocal texts
        t = " ".join(str(text).split())
        if not t or texts > 20000:
            return
        m = _mul(list(tm), list(cm))
        x, y = m[4], m[5]
        size = float(font_size or 10) * math.hypot(m[2], m[3])
        rot = math.degrees(math.atan2(m[1], m[0]))
        if size <= 0:
            return
        msp.add_text(t[:250], height=size * k, dxfattribs={"layer": "PDF-TEXT", "rotation": rot}).set_placement((x * k, y * k))
        texts += 1

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:  # noqa: BLE001 - text is a bonus; lines are the drawing
        notes.append("Text on this page couldn't be read; only lines were imported.")

    if not state["paths"] and not texts:
        raise ImportError_("That PDF page has no vector lines or text. It may be a scanned image; try it as a sketch instead.")
    notes.insert(0, f"Imported {state['paths']:,} lines and {texts:,} text items from page {page_number} of {len(reader.pages)}"
                    + (f" at 1:{scale:g}" if scale != 1 else " at paper size (scale 1)") + ".")
    if state["images"]:
        notes.append(f"{state['images']} embedded image{'s were' if state['images'] != 1 else ' was'} skipped.")
    if state["truncated"]:
        notes.append(f"The page is very detailed; only the first {MAX_PDF_SEGMENTS:,} line segments were imported.")
    if page.get("/Rotate"):
        notes.append(f"The PDF page is rotated {page.get('/Rotate')}°; the drawing is imported unrotated.")
    return doc, notes


# ── sketch photo via a vision model ─────────────────────────────────────────


def image_size(data: bytes) -> tuple[int, int, str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return w, h, "image/png"
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return w, h, "image/jpeg"
            seg = struct.unpack(">H", data[i + 2 : i + 4])[0]
            i += 2 + seg
        raise ImportError_("That JPEG image couldn't be read.")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return w, h, "image/webp"
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF, "image/webp"
        if chunk == b"VP8L":
            b = data[21:25]
            bits = int.from_bytes(b, "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, "image/webp"
    raise ImportError_("Sketches can be PNG, JPEG or WebP images.")


SKETCH_PROMPT = """You are tracing a floor plan from an image (a hand sketch, a photo of a sketch, or a scanned plan) into CAD geometry.

Return ONE JSON object and nothing else:
{"polylines": [{"kind": "wall|door|window|rack|column|other", "closed": true, "points": [[x, y], ...]}],
 "texts": [{"x": 0, "y": 0, "text": "OFFICE"}],
 "notes": "<one sentence about anything you were unsure of>"}

Coordinates are normalised: x from 0 (left edge of the image) to 1000 (right edge), y from 0 (TOP edge) to 1000 (BOTTOM edge).
Trace walls as polylines along their centre lines; straighten lines that are meant to be straight and make corners square when they clearly are.
Include room labels and written dimensions as texts at their position. Leave out shading, scribbles and the paper edge.
At most 300 polylines and 200 texts."""


def sketch_to_dxf(data: bytes, width_m: float, model) -> tuple[Drawing, list[str]]:
    if model is None:
        raise ImportError_("Tracing a sketch needs an AI model that can see images. Set OPENROUTER_API_KEY (or AI_VISION_MODEL).")
    if len(data) > 8 * 1024 * 1024:
        raise ImportError_("Sketch images can be up to 8 MB.")
    w_px, h_px, mime = image_size(data)
    if not (w_px and h_px):
        raise ImportError_("That image has no size.")
    if not 0.5 <= width_m <= 5000:
        raise ImportError_("Give the real width of the sketched area in metres (0.5 to 5000).")
    url = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    messages = [{"role": "user", "content": [{"type": "text", "text": SKETCH_PROMPT}, {"type": "image_url", "image_url": {"url": url}}]}]
    from .agent import parse_reply

    raw = model.complete(messages)
    obj = parse_reply(raw)
    if not obj:
        raise ImportError_("The vision model didn't return a drawing. Try a clearer photo, taken straight on.")
    height_m = width_m * h_px / w_px
    sx, sy = width_m / 1000.0, height_m / 1000.0
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4  # millimetres, like most plans
    msp = doc.modelspace()
    layer_of = {"wall": ("A-WALL", 7), "door": ("A-DOOR", 2), "window": ("A-GLAZ", 4), "rack": ("A-EQPM-RACK", 5),
                "column": ("S-COLS", 8), "other": ("A-SKCH", 6)}
    for name, color in layer_of.values():
        if not doc.layers.has_entry(name):
            doc.layers.add(name, color=color)
    doc.layers.add("A-ANNO-TEXT", color=7)
    lines = texts = 0
    for pl in (obj.get("polylines") or [])[:300]:
        if not isinstance(pl, dict):
            continue
        pts = []
        for p in pl.get("points") or []:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                x, y = _num(p[0], None), _num(p[1], None)
                if x is None or y is None:
                    continue
                x, y = max(-100, min(1100, x)), max(-100, min(1100, y))
                pts.append((x * sx * 1000, (1000 - y) * sy * 1000))
        if len(pts) < 2:
            continue
        layer = layer_of.get(str(pl.get("kind", "other")).lower(), layer_of["other"])[0]
        msp.add_lwpolyline(pts, close=bool(pl.get("closed")), dxfattribs={"layer": layer})
        lines += 1
    th = max(width_m * 1000 / 120, 100)
    for t in (obj.get("texts") or [])[:200]:
        if not isinstance(t, dict) or not str(t.get("text", "")).strip():
            continue
        x, y = _num(t.get("x"), 500), _num(t.get("y"), 500)
        msp.add_text(str(t["text"])[:120], height=th, dxfattribs={"layer": "A-ANNO-TEXT"}).set_placement((x * sx * 1000, (1000 - y) * sy * 1000))
        texts += 1
    if not lines:
        raise ImportError_("The vision model couldn't find any lines in that image.")
    notes = [f"Traced {lines} line{'s' if lines != 1 else ''} and {texts} label{'s' if texts != 1 else ''} from the sketch, "
             f"scaled to {width_m:g} m × {height_m:.3g} m. Check it against the original: tracing from a photo is approximate."]
    if obj.get("notes"):
        notes.append("The model noted: " + str(obj["notes"])[:300])
    return doc, notes
