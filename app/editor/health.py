"""Drawing health check: what's untidy, risky or wrong, each with a one-click fix.

Every finding carries the handles it concerns (so the viewer can highlight
them) and, where a safe fix exists, the operations that fix it. A fix goes
through the same proposal/accept flow as any other edit.
"""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from typing import Optional

from ezdxf.document import Drawing

from . import ops as ops_mod
from .geometry import Scene
from .units import Units

MAX_HANDLES = 500
SEVERITY_WEIGHT = {"error": 12, "warn": 5, "info": 1}


def _finding(fid: str, severity: str, title: str, detail: str, count: int, handles=(), fix: Optional[dict] = None) -> dict:
    return {"id": fid, "severity": severity, "title": title, "detail": detail, "count": count,
            "handles": list(handles)[:MAX_HANDLES], "fix": fix}


def _fix(label: str, op_list: list) -> dict:
    return {"label": label, "ops": op_list}


def handle_boxes(scene: Scene) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for it in scene.items:
        if it["k"] == "p":
            p = it["p"]
            b = [min(p[0::2]), min(p[1::2]), max(p[0::2]), max(p[1::2])]
        else:
            b = [it["x"], it["y"], it["x"], it["y"]]
        cur = out.get(it["h"])
        if cur is None:
            out[it["h"]] = b
        else:
            cur[0], cur[1] = min(cur[0], b[0]), min(cur[1], b[1])
            cur[2], cur[3] = max(cur[2], b[2]), max(cur[3], b[3])
    return out


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def stray_entities(scene: Scene) -> list[str]:
    """Entities far outside the main body of the drawing (a classic cause of 'zoom extents shows nothing')."""
    boxes = handle_boxes(scene)
    if len(boxes) < 20:
        return []
    cx = sorted((b[0] + b[2]) / 2 for b in boxes.values())
    cy = sorted((b[1] + b[3]) / 2 for b in boxes.values())
    x_lo, x_hi = _percentile(cx, 0.05), _percentile(cx, 0.95)
    y_lo, y_hi = _percentile(cy, 0.05), _percentile(cy, 0.95)
    span = max(x_hi - x_lo, y_hi - y_lo, 1e-9)
    reach = span * 10
    out = []
    for h, b in boxes.items():
        mx, my = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if mx < x_lo - reach or mx > x_hi + reach or my < y_lo - reach or my > y_hi + reach:
            out.append(h)
    return out


def _dimension_mismatches(doc: Drawing) -> list[tuple[str, str, float]]:
    out = []
    for e in doc.modelspace():
        if e.dxftype() != "DIMENSION":
            continue
        override = str(e.dxf.get("text", "") or "")
        if override in ("", "<>") or "<>" in override:
            continue
        nums = re.findall(r"-?\d+(?:[.,]\d+)?", override.replace(" ", ""))
        if not nums:
            continue
        try:
            measured = float(e.get_measurement())
        except Exception:  # noqa: BLE001
            continue
        try:
            style = e.doc.dimstyles.get(e.dxf.get("dimstyle", "Standard"))
            lfac = float(style.dxf.get("dimlfac", 1.0)) if style else 1.0
        except Exception:  # noqa: BLE001
            lfac = 1.0
        shown = float(nums[0].replace(",", "."))
        target = measured * lfac
        if abs(target) < 1e-9:
            continue
        if abs(shown - target) / abs(target) > 0.005:
            out.append((e.dxf.handle, override, target))
    return out


def check(doc: Drawing, scene: Scene, units: Units, digest: dict) -> dict:
    msp = doc.modelspace()
    findings: list[dict] = []

    # Units
    code = int(doc.header.get("$INSUNITS", 0) or 0)
    size = max(digest.get("size") or [0, 0])
    if code == 0:
        guess = "mm" if size >= 1000 else "m"
        findings.append(_finding("units-missing", "warn", "Units aren't recorded",
                                 f"The file doesn't say what one unit means. From its size it looks like {guess}. Other programs may import it at the wrong scale.",
                                 1, fix=_fix(f"Label as {guess}", [{"op": "set_units", "units": guess}])))
    elif code == 4 and 0 < size < 200:
        findings.append(_finding("units-suspect", "warn", "Units look wrong",
                                 f"The file says millimetres, but the whole drawing is only {size:g} mm across. It was probably drawn in metres.",
                                 1, fix=_fix("Relabel as metres", [{"op": "set_units", "units": "m"}])))
    elif code == 6 and size > 20000:
        findings.append(_finding("units-suspect", "warn", "Units look wrong",
                                 f"The file says metres, but the drawing is {size:,.0f} m across. It was probably drawn in millimetres.",
                                 1, fix=_fix("Relabel as millimetres", [{"op": "set_units", "units": "mm"}])))

    # Unused layers / blocks
    used_layers = {e.dxf.layer.lower() for e in doc.entitydb.values() if e.dxf.hasattr("layer")}
    current = str(doc.header.get("$CLAYER", "0")).lower()
    dead_layers = [l.dxf.name for l in doc.layers if l.dxf.name.lower() not in used_layers | ops_mod.PROTECTED_LAYERS | {current}]
    if dead_layers:
        findings.append(_finding("unused-layers", "info", f"{len(dead_layers)} unused layer{'s' if len(dead_layers) != 1 else ''}",
                                 "Nothing is drawn on " + ", ".join(dead_layers[:10]) + ("…" if len(dead_layers) > 10 else "") + ".",
                                 len(dead_layers), fix=_fix("Purge them", [{"op": "purge_unused_layers"}])))
    try:
        dead_blocks = ops_mod._unused_blocks(doc)
    except Exception:  # noqa: BLE001
        dead_blocks = []
    if dead_blocks:
        findings.append(_finding("unused-blocks", "info", f"{len(dead_blocks)} unused block definition{'s' if len(dead_blocks) != 1 else ''}",
                                 "Defined but never placed: " + ", ".join(dead_blocks[:10]) + ("…" if len(dead_blocks) > 10 else "") + ".",
                                 len(dead_blocks), fix=_fix("Purge them", [{"op": "purge_unused_blocks"}])))

    # Duplicates, degenerate geometry, empty text, overrides, 3D, layer 0
    seen: dict = {}
    dupes, zero, empty, overrides, raised, on_zero = [], [], [], [], [], []
    heights: Counter = Counter()
    proxies = 0
    for e in msp:
        kind = e.dxftype()
        h = e.dxf.handle
        sig = ops_mod._signature(e, 1e-4, False)
        if sig is not None:
            if sig in seen:
                dupes.append(h)
            else:
                seen[sig] = h
        try:
            if kind == "LINE" and e.dxf.start.isclose(e.dxf.end, abs_tol=1e-9):
                zero.append(h)
            elif kind in ("CIRCLE", "ARC") and e.dxf.radius <= 1e-12:
                zero.append(h)
            elif kind == "LWPOLYLINE" and len({(round(x, 9), round(y, 9)) for x, y in e.get_points("xy")}) < 2:
                zero.append(h)
        except Exception:  # noqa: BLE001
            pass
        if kind in ("TEXT", "MTEXT"):
            text = e.plain_text() if kind == "MTEXT" else e.dxf.get("text", "")
            if not str(text).strip():
                empty.append(h)
            else:
                heights[round(float(e.dxf.get("char_height", 1) if kind == "MTEXT" else e.dxf.get("height", 1)), 4)] += 1
        if e.dxf.get("color", 256) not in (256, 0) or e.dxf.hasattr("true_color"):
            overrides.append(h)
        try:
            if _has_z(e):
                raised.append(h)
        except Exception:  # noqa: BLE001
            pass
        if e.dxf.get("layer", "0") == "0" and kind != "VIEWPORT":
            on_zero.append(h)
        if kind == "ACAD_PROXY_ENTITY" or type(e).__name__ == "DXFTagStorage":
            proxies += 1

    if dupes:
        findings.append(_finding("duplicates", "warn", f"{len(dupes):,} duplicate object{'s' if len(dupes) != 1 else ''}",
                                 "Exact copies drawn on top of others. They double counts and make lines print darker.",
                                 len(dupes), dupes, _fix("Delete the copies", [{"op": "delete_duplicates"}])))
    if zero:
        findings.append(_finding("zero-length", "warn", f"{len(zero):,} zero-size object{'s' if len(zero) != 1 else ''}",
                                 "Lines with no length, circles with no radius or polylines that are a single point.",
                                 len(zero), zero, _fix("Delete them", [{"op": "delete", "selector": {"handles": zero[:MAX_HANDLES]}}])))
    if empty:
        findings.append(_finding("empty-text", "info", f"{len(empty):,} empty text object{'s' if len(empty) != 1 else ''}",
                                 "Text with nothing in it. Invisible, but it still gets selected and counted.",
                                 len(empty), empty, _fix("Delete them", [{"op": "delete", "selector": {"handles": empty[:MAX_HANDLES]}}])))
    if raised:
        findings.append(_finding("not-flat", "warn", f"{len(raised):,} object{'s are' if len(raised) != 1 else ' is'} not at elevation 0",
                                 "A 2D drawing with objects lifted off the ground plane snaps, trims and measures wrongly.",
                                 len(raised), raised, _fix("Flatten to 0", [{"op": "flatten"}])))
    strays = stray_entities(scene)
    if strays:
        findings.append(_finding("strays", "warn", f"{len(strays):,} object{'s' if len(strays) != 1 else ''} far from everything else",
                                 "Stray objects far away make 'zoom extents' show an empty screen and bloat plots.",
                                 len(strays), strays, _fix("Delete them", [{"op": "delete", "selector": {"handles": strays[:MAX_HANDLES]}}])))
    if len(heights) > 8:
        findings.append(_finding("text-heights", "info", f"{len(heights)} different text heights",
                                 "Text uses many slightly different heights. A standard set looks tidier and plots consistently.",
                                 len(heights), fix=_fix("Snap to the 3 most common", [{"op": "normalize_text_heights"}])))
    shx = [s.dxf.name for s in doc.styles if fnmatch.fnmatchcase(str(s.dxf.get("font", "")).lower(), "*.shx")]
    if shx:
        findings.append(_finding("shx-fonts", "info", f"{len(shx)} text style{'s' if len(shx) != 1 else ''} use SHX fonts",
                                 "SHX fonts may be missing on other machines and aren't searchable in PDFs: " + ", ".join(shx[:8]) + ".",
                                 len(shx), fix=_fix("Switch to Arial", [{"op": "replace_fonts", "font": "arial.ttf"}])))
    if overrides and len(overrides) > 0.25 * max(1, len(msp)):
        findings.append(_finding("color-overrides", "info", f"{len(overrides):,} objects have their own colour",
                                 "Colours set per object instead of by layer make layer standards and plot styles unreliable.",
                                 len(overrides), overrides, _fix("Set to ByLayer", [{"op": "set_color", "selector": {"handles": overrides[:MAX_HANDLES]}, "color": "bylayer"}])))
    if on_zero and len(on_zero) > 0.25 * max(1, len(msp)):
        findings.append(_finding("layer-zero", "info", f"{len(on_zero):,} objects on layer 0",
                                 "Layer 0 is meant for block contents. Objects drawn on it are hard to control. Ask the assistant to move them to a proper layer.",
                                 len(on_zero), on_zero))
    hidden = [l for l in digest.get("layers", []) if not l["on"] and l["count"]]
    if hidden:
        findings.append(_finding("hidden-content", "info", f"{len(hidden)} hidden layer{'s' if len(hidden) != 1 else ''} with content",
                                 "Objects on layers that are off or frozen: " + ", ".join(f"{l['name']} ({l['count']})" for l in hidden[:8]) + ". Check nothing important is hidden.",
                                 sum(l["count"] for l in hidden)))
    dims = _dimension_mismatches(doc)
    if dims:
        findings.append(_finding("dimension-overrides", "error", f"{len(dims)} dimension{'s' if len(dims) != 1 else ''} show the wrong value",
                                 "Typed-over dimension text that doesn't match what is drawn: " + "; ".join(f"says “{t}”, measures {units.show(m)}" for _, t, m in dims[:5]) + ".",
                                 len(dims), [d[0] for d in dims]))
    typos = spelling(doc)
    if typos:
        handles = sorted({h for _r, _n, hs in typos.values() for h in hs})
        fix_ops = []
        for wrong, (right, _n, _hs) in sorted(typos.items())[:12]:
            fix_ops.append({"op": "replace_text", "find": wrong, "replace": right, "match_case_of_found": True, "optional": True})
        listed = ", ".join(f"{w} → {r}" for w, (r, _n, _h) in sorted(typos.items())[:8])
        findings.append(_finding("spelling", "warn", f"{sum(n for _r, n, _h in typos.values())} spelling mistake{'s' if len(typos) != 1 else ''} in text",
                                 f"Common misspellings found: {listed}. (Use the assistant for a full proofread.)",
                                 sum(n for _r, n, _h in typos.values()), handles, _fix("Correct them", fix_ops)))
    xrefs = [b for b in doc.blocks if b.block is not None and b.block.is_xref]
    if xrefs:
        findings.append(_finding("xrefs", "info", f"{len(xrefs)} external reference{'s' if len(xrefs) != 1 else ''}",
                                 "Referenced files must travel with this drawing: " + ", ".join(f"{b.name} → {b.block.dxf.get('xref_path', '?')}" for b in xrefs[:6]) + ".",
                                 len(xrefs)))
    ext = digest.get("extents")
    if ext and max(abs(v) for v in ext) > 1e7:
        findings.append(_finding("far-origin", "warn", "Drawing is very far from the origin",
                                 "Coordinates over 10 million units lose precision in CAD software (jittery zoom, inaccurate snaps). Consider moving it closer to 0,0.", 1))
    if proxies:
        findings.append(_finding("proxies", "info", f"{proxies} add-on (proxy) objects",
                                 "Objects from CAD add-ons that other programs can't edit. They are kept, but may not convert to older versions.", proxies))

    penalty = sum(SEVERITY_WEIGHT[f["severity"]] for f in findings)
    score = max(0, 100 - penalty)
    findings.sort(key=lambda f: ({"error": 0, "warn": 1, "info": 2}[f["severity"]], -f["count"]))
    fixable = [f for f in findings if f["fix"]]
    return {"score": score, "issues": len(findings), "findings": findings,
            "fixAll": _fix("Fix everything safe", [{**op, "optional": True} for f in fixable if f["id"] in SAFE for op in f["fix"]["ops"]][:12])
            if any(f["id"] in SAFE for f in fixable) else None}


# Common misspellings seen on drawings (title blocks, room names, notes).
MISSPELLINGS = {
    "recieve": "receive", "seperate": "separate", "accomodate": "accommodate", "occured": "occurred",
    "existant": "existing", "exisiting": "existing", "existng": "existing", "colum": "column", "columm": "column",
    "elevaton": "elevation", "elevaiton": "elevation", "staircaise": "staircase", "stareway": "stairway",
    "corridoor": "corridor", "coridor": "corridor", "warehouce": "warehouse", "wharehouse": "warehouse",
    "storgae": "storage", "stroage": "storage", "receiving": "receiving", "recieving": "receiving", "dispach": "dispatch",
    "offfice": "office", "ofice": "office", "toliet": "toilet", "toilette": "toilet", "kitchin": "kitchen",
    "electical": "electrical", "electricial": "electrical", "mechanicial": "mechanical", "plumming": "plumbing",
    "sprinker": "sprinkler", "sprinler": "sprinkler", "extinguiser": "extinguisher", "emergancy": "emergency",
    "maintainance": "maintenance", "maintenence": "maintenance", "equipement": "equipment", "loadin": "loading",
    "plaform": "platform", "platfrom": "platform", "entrence": "entrance", "enterance": "entrance", "exlusive": "exclusive",
    "dimention": "dimension", "dimesion": "dimension", "revison": "revision", "revsion": "revision", "drawng": "drawing",
    "aproved": "approved", "approvd": "approved", "cheked": "checked", "chekced": "checked", "scaal": "scale",
    "minimun": "minimum", "maximun": "maximum", "existin": "existing", "conveyer": "conveyor",
}


def spelling(doc: Drawing) -> dict[str, tuple[str, int, list[str]]]:
    """{wrong: (right, count, handles)} for known misspellings in text."""
    found: dict[str, list] = {}
    for e in doc.modelspace():
        kind = e.dxftype()
        if kind not in ("TEXT", "MTEXT", "INSERT"):
            continue
        if kind == "INSERT":
            texts = [a.dxf.get("text", "") for a in e.attribs]
        else:
            texts = [e.plain_text() if kind == "MTEXT" else e.dxf.get("text", "")]
        for text in texts:
            for word in re.findall(r"[A-Za-z]+", str(text)):
                right = MISSPELLINGS.get(word.lower())
                if right and right != word.lower():
                    rec = found.setdefault(word.lower(), [right, 0, []])
                    rec[1] += 1
                    if e.dxf.handle not in rec[2]:
                        rec[2].append(e.dxf.handle)
    return {k: (v[0], v[1], v[2]) for k, v in found.items()}


def _match_case(right: str, sample: str) -> str:
    if sample.isupper():
        return right.upper()
    if sample[:1].isupper():
        return right.capitalize()
    return right


# Fixes that only remove clutter or relabel; they are offered together.
SAFE = {"unused-layers", "unused-blocks", "duplicates", "zero-length", "empty-text", "not-flat"}


def _has_z(e) -> bool:
    kind = e.dxftype()
    d = e.dxf
    for attr in ("start", "end", "center", "insert", "location"):
        if d.hasattr(attr):
            v = d.get(attr)
            if hasattr(v, "z") and abs(v.z) > 1e-9:
                return True
    if kind == "LWPOLYLINE" and abs(d.get("elevation", 0)) > 1e-9:
        return True
    return False
