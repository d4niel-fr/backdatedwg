"""Reading a drawing: what things are, how much there is, where the rooms are.

Everything here is read-only and deterministic. It answers "what is this?",
"what's in this area?", quantity take-off, rooms and areas, door/window
schedules and bills of materials, a plain-English explanation of the drawing,
and a warehouse check of rack rows and aisle widths.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections import Counter, defaultdict
from typing import Iterable, Optional

from ezdxf import bbox
from ezdxf import path as ezpath
from ezdxf.document import Drawing

from .geometry import Scene
from .units import Units

CURVES = {"LINE", "ARC", "CIRCLE", "ELLIPSE", "LWPOLYLINE", "POLYLINE", "SPLINE"}


# ── small geometry helpers ──────────────────────────────────────────────────


def flat_points(e, segments_per_unit: Optional[float] = None) -> list[tuple[float, float]]:
    """An entity's outline as a list of (x, y)."""
    p = ezpath.make_path(e)
    cv = p.control_vertices()
    if not cv:
        return []
    xs = [v.x for v in cv]
    ys = [v.y for v in cv]
    size = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
    return [(v.x, v.y) for v in p.flattening(size / 400.0)]


def polyline_length(pts: list[tuple[float, float]], closed: bool = False) -> float:
    total = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))
    if closed and len(pts) > 2 and pts[0] != pts[-1]:
        total += math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1])
    return total


def polygon_area(pts: list[tuple[float, float]]) -> float:
    if len(pts) < 3:
        return 0.0
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def point_in_polygon(x: float, y: float, pts: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi:
            inside = not inside
        j = i
    return inside


def is_closed(e) -> bool:
    kind = e.dxftype()
    if kind == "LWPOLYLINE":
        return bool(e.closed)
    if kind == "POLYLINE":
        return bool(e.is_closed)
    if kind in ("CIRCLE", "ELLIPSE"):
        return kind == "CIRCLE" or abs((e.dxf.end_param - e.dxf.start_param) - math.tau) < 1e-6
    if kind == "SPLINE":
        return bool(e.closed)
    return False


def measure(e) -> dict:
    """Length, and area when closed, in drawing units."""
    out: dict = {}
    kind = e.dxftype()
    try:
        if kind == "LINE":
            out["length"] = e.dxf.start.distance(e.dxf.end)
        elif kind == "CIRCLE":
            out["length"] = math.tau * e.dxf.radius
            out["area"] = math.pi * e.dxf.radius ** 2
            out["radius"] = e.dxf.radius
        elif kind == "ARC":
            sweep = (e.dxf.end_angle - e.dxf.start_angle) % 360 or 360
            out["length"] = math.radians(sweep) * e.dxf.radius
            out["radius"] = e.dxf.radius
        elif kind in CURVES:
            pts = flat_points(e)
            closed = is_closed(e)
            out["length"] = polyline_length(pts, closed)
            if closed:
                out["area"] = polygon_area(pts)
        elif kind == "HATCH":
            total = 0.0
            for p in ezpath.from_hatch(e):
                total += polygon_area([(v.x, v.y) for v in p.flattening(1e-3)])
            out["area"] = total
    except Exception:  # noqa: BLE001 - odd geometry just has no measurements
        pass
    return out


def _text_of(e) -> str:
    kind = e.dxftype()
    try:
        if kind == "MTEXT":
            return e.plain_text()
        if kind in ("TEXT", "ATTRIB"):
            return e.dxf.get("text", "")
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _round(v: float, n: int = 3) -> float:
    return float(f"{v:.{n}g}") if v else 0.0


# ── what is this? ────────────────────────────────────────────────────────────


def inspect(doc: Drawing, handles: Iterable[str], units: Units, limit: int = 50) -> list[dict]:
    rows = []
    for h in list(handles)[:limit]:
        e = doc.entitydb.get(str(h).upper())
        if e is None or not e.is_alive:
            continue
        kind = e.dxftype()
        d = e.dxf
        row: dict = {"handle": d.handle, "type": kind, "layer": d.get("layer", "0")}
        color = d.get("color", 256)
        row["color"] = "by layer" if color == 256 else ("by block" if color == 0 else color)
        if d.get("linetype", "BYLAYER").upper() != "BYLAYER":
            row["linetype"] = d.get("linetype")
        try:
            box = bbox.extents([e], fast=True)
            if box.has_data:
                row["bbox"] = [round(box.extmin.x, 3), round(box.extmin.y, 3), round(box.extmax.x, 3), round(box.extmax.y, 3)]
                row["size"] = [round(box.extmax.x - box.extmin.x, 3), round(box.extmax.y - box.extmin.y, 3)]
        except Exception:  # noqa: BLE001
            pass
        m = measure(e)
        row.update({k: round(v, 3) for k, v in m.items()})
        text = _text_of(e)
        if text:
            row["text"] = text[:300]
            row["textHeight"] = d.get("char_height") if kind == "MTEXT" else d.get("height")
            row["style"] = d.get("style", "Standard")
        if kind == "INSERT":
            row["block"] = d.get("name")
            row["insert"] = [round(d.insert.x, 3), round(d.insert.y, 3)]
            row["rotation"] = round(d.get("rotation", 0), 3)
            row["scale"] = [d.get("xscale", 1), d.get("yscale", 1)]
            row["attributes"] = {a.dxf.tag: a.dxf.text for a in e.attribs}
        if kind == "DIMENSION":
            try:
                row["measurement"] = round(float(e.get_measurement()), 4)
                row["override"] = d.get("text", "<>")
            except Exception:  # noqa: BLE001
                pass
        row["sentence"] = _sentence(row, units)
        rows.append(row)
    return rows


def _sentence(row: dict, units: Units) -> str:
    kind = row["type"]
    names = {"INSERT": "a block reference", "LWPOLYLINE": "a polyline", "LINE": "a line", "CIRCLE": "a circle", "ARC": "an arc",
             "TEXT": "a text label", "MTEXT": "a text block", "DIMENSION": "a dimension", "HATCH": "a hatch", "SPLINE": "a spline"}
    s = names.get(kind, f"a {kind.lower()}")
    if kind == "INSERT":
        s += f" of block {row.get('block')}"
    s = s[0].upper() + s[1:] + f" on layer {row['layer']}"
    bits = []
    if "length" in row and kind not in ("TEXT", "MTEXT"):
        bits.append(f"length {units.show(row['length'])}")
    if row.get("area"):
        bits.append(f"area {_area_text(row['area'], units)}")
    if "radius" in row:
        bits.append(f"radius {units.show(row['radius'])}")
    if "text" in row:
        bits.append(f"reading “{row['text'][:60]}”")
    if row.get("attributes"):
        bits.append("attributes " + ", ".join(f"{k}={v}" for k, v in list(row["attributes"].items())[:6]))
    if "measurement" in row:
        bits.append(f"measuring {units.show(row['measurement'])}")
    return s + (": " + "; ".join(bits) if bits else "") + "."


def _area_text(area: float, units: Units) -> str:
    m2 = area * units.to_m ** 2
    if m2 >= 0.01:
        return f"{m2:,.2f} m²"
    return f"{area:,.4g} {units.short}²"


# ── what's in this area? ─────────────────────────────────────────────────────


def area_summary(doc: Drawing, scene: Scene, box: list[float], units: Units) -> dict:
    x0, y0, x1, y1 = min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3])
    inside: set[str] = set()
    texts = []
    for it in scene.items:
        if it["k"] == "p":
            p = it["p"]
            if any(x0 <= p[i] <= x1 and y0 <= p[i + 1] <= y1 for i in range(0, len(p), 2)):
                inside.add(it["h"])
        elif x0 <= it["x"] <= x1 and y0 <= it["y"] <= y1:
            inside.add(it["h"])
            texts.append(" ".join(it["v"].split())[:60])
    types: Counter = Counter()
    layers: Counter = Counter()
    blocks: Counter = Counter()
    for h in inside:
        e = doc.entitydb.get(h)
        if e is None:
            continue
        types[e.dxftype()] += 1
        layers[e.dxf.get("layer", "0")] += 1
        if e.dxftype() == "INSERT":
            blocks[e.dxf.get("name", "?")] += 1
    w, hgt = x1 - x0, y1 - y0
    return {
        "bbox": [x0, y0, x1, y1],
        "size": [w, hgt],
        "sizeText": f"{units.show(w)} × {units.show(hgt)}",
        "count": len(inside),
        "handles": sorted(inside)[:2000],
        "types": dict(types.most_common(10)),
        "layers": dict(layers.most_common(10)),
        "blocks": dict(blocks.most_common(10)),
        "texts": sorted(set(texts))[:40],
    }


def describe_area(summary: dict) -> str:
    if not summary["count"]:
        return f"That {summary['sizeText']} area is empty."
    parts = [f"That {summary['sizeText']} area holds {summary['count']:,} entities"]
    if summary["layers"]:
        parts.append("mostly on " + ", ".join(f"{k} ({v})" for k, v in list(summary["layers"].items())[:4]))
    s = ", ".join(parts) + "."
    if summary["blocks"]:
        s += " Blocks: " + ", ".join(f"{k} ×{v}" for k, v in list(summary["blocks"].items())[:5]) + "."
    if summary["texts"]:
        s += " Labels: " + ", ".join(summary["texts"][:8]) + "."
    return s


# ── quantity take-off ────────────────────────────────────────────────────────


def takeoff(doc: Drawing, units: Units) -> dict:
    blocks: dict[str, dict] = {}
    lengths: dict[str, float] = defaultdict(float)
    areas: dict[str, list] = defaultdict(lambda: [0, 0.0])
    counts: Counter = Counter()
    for e in doc.modelspace():
        kind = e.dxftype()
        layer = e.dxf.get("layer", "0")
        counts[kind] += 1
        if kind == "INSERT":
            name = e.dxf.get("name", "?")
            rec = blocks.setdefault(name, {"block": name, "count": 0, "layers": Counter()})
            rec["count"] += 1
            rec["layers"][layer] += 1
        elif kind in CURVES:
            m = measure(e)
            if "length" in m and not m.get("area"):
                lengths[layer] += m["length"]
            if m.get("area"):
                areas[layer][0] += 1
                areas[layer][1] += m["area"]
    k = units.to_m
    return {
        "units": units.describe(),
        "blocks": sorted(({"block": r["block"], "count": r["count"], "layers": ", ".join(r["layers"])} for r in blocks.values()), key=lambda r: (-r["count"], r["block"])),
        "lengths": sorted(({"layer": l, "length": round(v, 3), "metres": round(v * k, 3)} for l, v in lengths.items()), key=lambda r: -r["length"]),
        "areas": sorted(({"layer": l, "shapes": n, "area": round(a, 3), "squareMetres": round(a * k * k, 3)} for l, (n, a) in areas.items()), key=lambda r: -r["area"]),
        "counts": dict(counts.most_common()),
    }


def takeoff_csv(t: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Section", "Item", "Quantity", "Unit", "Notes"])
    for r in t["blocks"]:
        w.writerow(["Blocks", r["block"], r["count"], "ea", r["layers"]])
    for r in t["lengths"]:
        w.writerow(["Lengths", r["layer"], r["metres"], "m", f"{r['length']} {t['units']['short']} in drawing units"])
    for r in t["areas"]:
        w.writerow(["Areas", r["layer"], r["squareMetres"], "m²", f"{r['shapes']} closed shapes"])
    for k, v in t["counts"].items():
        w.writerow(["Entities", k, v, "ea", ""])
    return buf.getvalue()


# ── rooms and areas ──────────────────────────────────────────────────────────


def rooms(doc: Drawing, units: Units, min_area_m2: float = 1.0) -> list[dict]:
    """Closed outlines big enough to be spaces, named by the label inside them."""
    msp = doc.modelspace()
    labels = []
    for e in msp:
        if e.dxftype() in ("TEXT", "MTEXT"):
            t = " ".join(_text_of(e).split())
            if t:
                ins = e.dxf.insert
                h = float(e.dxf.get("char_height", 1) if e.dxftype() == "MTEXT" else e.dxf.get("height", 1))
                labels.append((ins.x, ins.y, t, h))
    k2 = units.to_m ** 2
    found = []
    for e in msp:
        if e.dxftype() not in ("LWPOLYLINE", "POLYLINE", "CIRCLE") or not is_closed(e):
            continue
        try:
            pts = flat_points(e)
        except Exception:  # noqa: BLE001
            continue
        area = polygon_area(pts)
        if area * k2 < min_area_m2:
            continue
        inside = [lb for lb in labels if point_in_polygon(lb[0], lb[1], pts)]
        found.append({"e": e, "pts": pts, "area": area, "inside": inside})
    # A label belongs to the smallest outline containing it.
    found.sort(key=lambda r: r["area"])
    claimed: set = set()
    out = []
    for r in found:
        mine = [lb for lb in r["inside"] if id(lb) not in claimed]
        for lb in mine:
            claimed.add(id(lb))
        mine.sort(key=lambda lb: -lb[3])
        name = mine[0][2] if mine else ""
        xs = [p[0] for p in r["pts"]]
        ys = [p[1] for p in r["pts"]]
        out.append({
            "handle": r["e"].dxf.handle,
            "layer": r["e"].dxf.get("layer", "0"),
            "name": name[:80] or "(unnamed)",
            "labels": [lb[2][:60] for lb in mine[:5]],
            "area": round(r["area"], 3),
            "squareMetres": round(r["area"] * k2, 2),
            "perimeter": round(polyline_length(r["pts"], True), 3),
            "perimeterMetres": round(polyline_length(r["pts"], True) * units.to_m, 2),
            "bbox": [min(xs), min(ys), max(xs), max(ys)],
        })
    out.sort(key=lambda r: -r["area"])
    return out


# ── schedules and bills of materials ─────────────────────────────────────────


def schedule(doc: Drawing, block: Optional[str] = None, mode: str = "instances") -> dict:
    """A door/window schedule (one row per placement) or a BOM (grouped, with quantities)."""
    import fnmatch

    pattern = block.lower() if block else None
    inserts = [e for e in doc.modelspace() if e.dxftype() == "INSERT" and (not pattern or fnmatch.fnmatchcase(e.dxf.get("name", "").lower(), pattern))]
    tags: list[str] = []
    for e in inserts:
        for a in e.attribs:
            t = a.dxf.get("tag", "")
            if t and t not in tags:
                tags.append(t)
    if mode == "bom":
        # Attributes that differ on every placement (door numbers, IDs) would make
        # each line a quantity of one; a bill of materials groups without them.
        per_block: dict[str, list[dict]] = defaultdict(list)
        for e in inserts:
            per_block[e.dxf.get("name", "?")].append({a.dxf.tag: a.dxf.text for a in e.attribs})
        unique = set()
        for t in tags:
            for blk, rows_ in per_block.items():
                vals = [r.get(t, "") for r in rows_ if r.get(t, "")]
                if len(vals) > 1 and len(set(vals)) == len(vals):
                    unique.add(t)
        tags = [t for t in tags if t not in unique]
        groups: Counter = Counter()
        for e in inserts:
            attrs = {a.dxf.tag: a.dxf.text for a in e.attribs}
            groups[(e.dxf.get("name", "?"),) + tuple(attrs.get(t, "") for t in tags)] += 1
        columns = ["Item", "Block"] + tags + ["Qty"]
        rows = [[i + 1, *key, qty] for i, (key, qty) in enumerate(sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1:])))]
        return {"mode": "bom", "columns": columns, "rows": rows, "total": len(inserts)}
    def sort_key(e):
        attrs = {a.dxf.tag: a.dxf.text for a in e.attribs}
        first = attrs.get(tags[0], "") if tags else ""
        num = re.findall(r"\d+", first)
        return (e.dxf.get("name", ""), first.rstrip("0123456789"), int(num[-1]) if num else 0, first)
    columns = ["Block"] + tags + ["X", "Y", "Layer"]
    rows = []
    for e in sorted(inserts, key=sort_key):
        attrs = {a.dxf.tag: a.dxf.text for a in e.attribs}
        rows.append([e.dxf.get("name", "?"), *(attrs.get(t, "") for t in tags), round(e.dxf.insert.x, 2), round(e.dxf.insert.y, 2), e.dxf.get("layer", "0")])
    return {"mode": "instances", "columns": columns, "rows": rows, "total": len(inserts)}


def table_csv(table: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(table["columns"])
    w.writerows(table["rows"])
    return buf.getvalue()


# ── warehouse check ──────────────────────────────────────────────────────────

RACK_WORDS = re.compile(r"rack|rak|pallet|shelv|bay|storage|stillage", re.I)


def warehouse(doc: Drawing, units: Units, min_aisle_m: float = 2.8) -> dict:
    """Racking rows and the aisles between them.

    Racks are block references or closed rectangles on layers (or blocks) whose
    names mention racking. Rows are found by grouping racks that share a band
    across the row direction; aisles are the clear gaps between neighbouring rows.
    """
    k = units.to_m
    boxes = []
    for e in doc.modelspace():
        kind = e.dxftype()
        name = e.dxf.get("name", "") if kind == "INSERT" else ""
        layer = e.dxf.get("layer", "0")
        if not (RACK_WORDS.search(layer) or RACK_WORDS.search(name)):
            continue
        if kind not in ("INSERT", "LWPOLYLINE", "POLYLINE"):
            continue
        if kind != "INSERT" and not is_closed(e):
            continue
        try:
            box = bbox.extents([e], fast=True)
        except Exception:  # noqa: BLE001
            continue
        if box.has_data:
            boxes.append((box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y, e.dxf.handle))
    if not boxes:
        return {"found": False, "message": "No racking found. Racks are recognised on layers or blocks named like RACK, PALLET or SHELVING."}
    w = sum(b[2] - b[0] for b in boxes)
    h = sum(b[3] - b[1] for b in boxes)
    horizontal = w >= h  # rows run along x when bays are wider than deep, summed
    lo, hi = (1, 3) if horizontal else (0, 2)
    bands: list[list] = []
    for b in sorted(boxes, key=lambda b: (b[lo], b[hi])):
        for band in bands:
            if b[lo] < band[1] - 1e-9 and b[hi] > band[0] + 1e-9:  # overlaps across the row direction
                band[0] = min(band[0], b[lo])
                band[1] = max(band[1], b[hi])
                band[2].append(b)
                break
        else:
            bands.append([b[lo], b[hi], [b]])
    bands.sort(key=lambda bd: bd[0])
    # Back-to-back racks stand a flue apart (typically 75–300 mm): one double row, not an aisle.
    flue_max = 0.6 / k
    merged: list[list] = []
    for bd in bands:
        if merged and bd[0] - merged[-1][1] < flue_max:
            merged[-1][1] = max(merged[-1][1], bd[1])
            merged[-1][2].extend(bd[2])
            merged[-1][3] += 1
        else:
            merged.append([bd[0], bd[1], list(bd[2]), 1])
    bands = merged
    rows = []
    for i, (a, z, members, lines) in enumerate(bands):
        along_lo = min(m[0 if horizontal else 1] for m in members)
        along_hi = max(m[2 if horizontal else 3] for m in members)
        rows.append({"row": i + 1, "bays": len(members), "lines": lines, "kind": "double" if lines == 2 else ("single" if lines == 1 else f"{lines}-deep"), "depth": round(z - a, 3), "length": round(along_hi - along_lo, 3),
                     "depthMetres": round((z - a) * k, 2), "lengthMetres": round((along_hi - along_lo) * k, 2),
                     "from": round(a, 3), "to": round(z, 3), "handles": [m[4] for m in members][:500]})
    aisles = []
    for prev, nxt in zip(rows, rows[1:]):
        gap = nxt["from"] - prev["to"]
        if gap <= 0:
            continue
        aisles.append({"between": [prev["row"], nxt["row"]], "width": round(gap, 3), "widthMetres": round(gap * k, 2),
                       "ok": gap * k >= min_aisle_m - 1e-9})
    narrow = [a for a in aisles if not a["ok"]]
    bays = sum(r["bays"] for r in rows)
    msg = f"{bays:,} rack bays in {len(rows)} row{'s' if len(rows) != 1 else ''} running {'east–west' if horizontal else 'north–south'}"
    if aisles:
        widths = [a["widthMetres"] for a in aisles]
        msg += f"; aisles {min(widths):g} m wide" if min(widths) == max(widths) else f"; aisles {min(widths):g}–{max(widths):g} m wide"
    if narrow:
        msg += f". {len(narrow)} aisle{'s are' if len(narrow) != 1 else ' is'} narrower than {min_aisle_m:g} m"
    return {"found": True, "direction": "x" if horizontal else "y", "bays": bays, "rows": rows, "aisles": aisles,
            "narrow": len(narrow), "minAisleMetres": min_aisle_m, "message": msg + "."}


# ── explain the drawing ──────────────────────────────────────────────────────

DISCIPLINES = {
    "A": "architectural", "S": "structural", "E": "electrical", "M": "mechanical", "P": "plumbing", "F": "fire protection",
    "C": "civil", "L": "landscape", "I": "interiors", "Q": "equipment", "T": "telecoms", "G": "general", "V": "survey",
}


def explain(doc: Drawing, digest: dict, units: Units, health_summary: Optional[dict] = None) -> dict:
    """A plain-English walkthrough built from what the drawing actually contains."""
    paragraphs = []
    size = digest.get("sizeMetres")
    kinds = digest.get("types", {})
    overview = f"This drawing has {digest['entityCount']:,} objects on {digest['layerCount']} layers"
    if size:
        overview += f", covering about {size[0]:g} × {size[1]:g} m"
    overview += f". It is drawn in {units.name}" + (" (a guess: the file doesn't say)" if units.guessed else "") + "."
    paragraphs.append(overview)

    disc = Counter()
    for l in digest["layers"]:
        m = re.match(r"^([A-Za-z])[-_ ]", l["name"])
        if m and m.group(1).upper() in DISCIPLINES and l["count"]:
            disc[DISCIPLINES[m.group(1).upper()]] += l["count"]
    if disc:
        paragraphs.append("Going by the layer names, it is mainly " + ", ".join(f"{d} ({n:,} objects)" for d, n in disc.most_common(4)) + ".")
    busy = [l for l in digest["layers"] if l["count"]][:6]
    if busy:
        paragraphs.append("The busiest layers are " + ", ".join(f"{l['name']} ({l['count']:,})" for l in busy) + ".")

    if digest.get("blocks"):
        paragraphs.append("Repeated parts (blocks): " + ", ".join(f"{b['name']} ×{b['count']}" for b in digest["blocks"][:8]) + ".")
    rms = [r for r in rooms(doc, units) if r["name"] != "(unnamed)"][:8]
    if rms:
        paragraphs.append("Named areas: " + ", ".join(f"{r['name']} ({r['squareMetres']:g} m²)" for r in rms) + ".")
    titles = sorted(_texts_with_height(doc), key=lambda t: -t[1])[:3]
    if titles:
        paragraphs.append("The largest text, probably the title, reads " + "; ".join(f"“{t[0][:60]}”" for t in titles) + ".")
    notes = []
    if kinds.get("DIMENSION"):
        notes.append(f"{kinds['DIMENSION']:,} dimensions")
    t = kinds.get("TEXT", 0) + kinds.get("MTEXT", 0)
    if t:
        notes.append(f"{t:,} text labels")
    if kinds.get("HATCH"):
        notes.append(f"{kinds['HATCH']:,} hatched areas")
    if notes:
        paragraphs.append("Annotation: " + ", ".join(notes) + ".")
    wh = warehouse(doc, units)
    if wh.get("found"):
        paragraphs.append("Racking: " + wh["message"])
    if health_summary:
        paragraphs.append(f"Health check: score {health_summary['score']}/100 with {health_summary['issues']} thing{'s' if health_summary['issues'] != 1 else ''} worth tidying.")
    return {"paragraphs": paragraphs, "text": " ".join(paragraphs)}


def _texts_with_height(doc: Drawing) -> list[tuple[str, float]]:
    out = []
    for e in doc.modelspace():
        if e.dxftype() in ("TEXT", "MTEXT"):
            t = " ".join(_text_of(e).split())
            if t:
                out.append((t, float(e.dxf.get("char_height", 1) if e.dxftype() == "MTEXT" else e.dxf.get("height", 1))))
    return out
