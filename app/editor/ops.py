"""The vocabulary of edits, and the code that carries them out.

An operation is a small JSON object: ``{"op": "move", "selector": {...}, "dx": "2m"}``.
The model can ask for nothing outside this list. Each operation is validated,
applied to a copy of the drawing, and reported as sentences a person can read
before anything is committed.
"""

from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from ezdxf import bbox
from ezdxf.document import Drawing
from ezdxf.enums import TextEntityAlignment
from ezdxf.lldxf.const import DXFError
from ezdxf.math import Matrix44

from .units import UnitError, Units

MAX_AFFECTED = 20_000
WARN_AFFECTED = 500
MAX_ARRAY = 1_000

TYPE_ALIASES: dict[str, set[str]] = {
    "line": {"LINE"},
    "polyline": {"LWPOLYLINE", "POLYLINE"},
    "lwpolyline": {"LWPOLYLINE"},
    "circle": {"CIRCLE"},
    "arc": {"ARC"},
    "ellipse": {"ELLIPSE"},
    "spline": {"SPLINE"},
    "text": {"TEXT", "MTEXT"},
    "mtext": {"MTEXT"},
    "block": {"INSERT"},
    "insert": {"INSERT"},
    "dimension": {"DIMENSION"},
    "hatch": {"HATCH"},
    "point": {"POINT"},
    "leader": {"LEADER", "MULTILEADER"},
    "solid": {"SOLID"},
}

COLOR_NAMES = {"red": 1, "yellow": 2, "green": 3, "cyan": 4, "blue": 5, "magenta": 6, "white": 7, "black": 7, "grey": 8, "gray": 8, "orange": 30}
PROTECTED_LAYERS = {"0", "defpoints"}
SELECTOR_KEYS = {"handles", "layer", "type", "text", "color", "block", "bbox", "selection", "all"}


class OpError(ValueError):
    """An operation that can't be carried out. The message is written for the model and for people."""


class NothingToChange(OpError):
    """Every operation was valid but none of them found anything to change."""


@dataclass
class Touched:
    changed: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)
    created: set[str] = field(default_factory=set)
    tables: bool = False  # layers or other tables changed


@dataclass
class Applied:
    summaries: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    touched: Touched = field(default_factory=Touched)


@dataclass
class Ctx:
    doc: Drawing
    units: Units
    selection: list[str]
    text_height: float
    out: Applied

    @property
    def msp(self):
        return self.doc.modelspace()

    def length(self, value, what: str) -> float:
        try:
            return self.units.length(value)
        except UnitError as e:
            raise OpError(f"{what}: {e}") from e

    def show(self, value: float) -> str:
        return self.units.show(value)


# ── selectors ───────────────────────────────────────────────────────────────


def _as_list(v) -> list:
    return list(v) if isinstance(v, (list, tuple, set)) else [v]


def _color_index(value, what="color") -> int:
    if isinstance(value, bool):
        raise OpError(f"{what}: expected a colour name or AutoCAD colour number 1-255.")
    if isinstance(value, (int, float)) and 0 < int(value) <= 255:
        return int(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in COLOR_NAMES:
            return COLOR_NAMES[v]
        if v.isdigit() and 0 < int(v) <= 255:
            return int(v)
    raise OpError(f"{what}: {value!r} isn't a colour. Use a name ({', '.join(sorted(COLOR_NAMES))}) or a number 1-255.")


def resolve(ctx: Ctx, selector, *, what: str = "selector") -> list:
    """The model-space entities a selector matches. Never empty: no match is an error."""
    if selector == "selection":
        selector = {"selection": True}
    if isinstance(selector, list):
        selector = {"handles": selector}
    if not isinstance(selector, dict):
        raise OpError(f"{what} must be an object like {{\"layer\": \"A-WALL\"}}.")
    extra = set(selector) - SELECTOR_KEYS
    if extra:
        raise OpError(f"{what} has unknown keys {sorted(extra)}. Allowed: {sorted(SELECTOR_KEYS)}.")
    filters = {k: v for k, v in selector.items() if k != "all" and v not in (None, False, "", [])}
    if not filters and not selector.get("all"):
        raise OpError(f"{what} would match every entity. Give at least one filter (layer, type, handles, text, bbox) or {{\"all\": true}}.")

    handles: Optional[set[str]] = None
    if "handles" in filters:
        handles = {str(h).upper() for h in _as_list(filters["handles"])}
    if filters.get("selection"):
        sel = {h.upper() for h in ctx.selection}
        if not sel:
            raise OpError("Nothing is selected. Click something in the drawing first, or describe what to change.")
        handles = sel if handles is None else handles & sel

    layers = [str(x).lower() for x in _as_list(filters["layer"])] if "layer" in filters else None
    types: Optional[set[str]] = None
    if "type" in filters:
        types = set()
        for t in _as_list(filters["type"]):
            key = str(t).strip().lower()
            types |= TYPE_ALIASES.get(key, {key.upper()})
    needle = str(filters["text"]).lower() if "text" in filters else None
    aci = _color_index(filters["color"]) if "color" in filters else None
    blocks = [str(b).lower() for b in _as_list(filters["block"])] if "block" in filters else None
    box = None
    if "bbox" in filters:
        b = filters["bbox"]
        if not isinstance(b, (list, tuple)) or len(b) != 4:
            raise OpError(f"{what}.bbox must be [xmin, ymin, xmax, ymax].")
        x0, y0, x1, y1 = (ctx.length(v, f"{what}.bbox") for v in b)
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    found = []
    for e in ctx.msp:
        if handles is not None and e.dxf.handle.upper() not in handles:
            continue
        kind = e.dxftype()
        if types is not None and kind not in types:
            continue
        if layers is not None and not any(fnmatch.fnmatchcase(e.dxf.get("layer", "0").lower(), p) for p in layers):
            continue
        if aci is not None and e.dxf.get("color", 256) != aci:
            continue
        if blocks is not None and (kind != "INSERT" or not any(fnmatch.fnmatchcase(e.dxf.get("name", "").lower(), p) for p in blocks)):
            continue
        if needle is not None and needle not in _entity_text(e).lower():
            continue
        if box is not None and not _in_box(e, box):
            continue
        found.append(e)
        if len(found) > MAX_AFFECTED:
            raise OpError(f"{what} matches more than {MAX_AFFECTED:,} entities. Narrow it down.")
    if not found:
        raise OpError(f"{what} matched no entities ({_describe(selector)}). Check the layer names, types and text against the drawing summary.")
    if len(found) > WARN_AFFECTED:
        ctx.out.warnings.append(f"A selector matched {len(found):,} entities.")
    return found


def _entity_text(e) -> str:
    kind = e.dxftype()
    try:
        if kind == "MTEXT":
            return e.plain_text()
        if kind == "TEXT":
            return e.dxf.get("text", "")
        if kind == "INSERT":
            return " ".join(a.dxf.get("text", "") for a in e.attribs)
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _in_box(e, box) -> bool:
    try:
        ext = bbox.extents([e], fast=True)
    except Exception:  # noqa: BLE001
        return False
    if not ext.has_data:
        return False
    return not (ext.extmax.x < box[0] or ext.extmin.x > box[2] or ext.extmax.y < box[1] or ext.extmin.y > box[3])


def _describe(selector: dict) -> str:
    parts = [f"{k}={v!r}" for k, v in selector.items() if v not in (None, False, "", [])]
    return ", ".join(parts)[:160] or "all"


_NAMES = {"INSERT": ("block reference", "block references"), "LWPOLYLINE": ("polyline", "polylines"), "MTEXT": ("text", "text items"), "TEXT": ("text", "text items")}


def _noun(entities: list, ctx: Ctx) -> str:
    n = len(entities)
    layers = {e.dxf.get("layer", "0") for e in entities}
    types = {e.dxftype() for e in entities}
    if len(types) == 1:
        kind = next(iter(types))
        one, many = _NAMES.get(kind, (kind.lower(), kind.lower() + "s"))
    else:
        one, many = "entity", "entities"
    where = f" on layer {next(iter(layers))}" if len(layers) == 1 else f" on {len(layers)} layers"
    return f"{n:,} {one if n == 1 else many}{where}"


# ── transforms ──────────────────────────────────────────────────────────────


def _transform(ctx: Ctx, entities: list, matrix: Matrix44) -> int:
    done = skipped = 0
    for e in entities:
        try:
            e.transform(matrix)
            ctx.out.touched.changed.add(e.dxf.handle)
            done += 1
        except (NotImplementedError, TypeError, DXFError, AttributeError, ArithmeticError):
            skipped += 1
    if skipped:
        ctx.out.warnings.append(f"{skipped:,} entities couldn't be transformed (unsupported type) and were left alone.")
    if not done:
        raise OpError("None of the selected entities can be transformed.")
    return done


def _centre(entities: list) -> tuple[float, float]:
    ext = bbox.extents(entities, fast=True)
    if not ext.has_data:
        return 0.0, 0.0
    return (ext.extmin.x + ext.extmax.x) / 2, (ext.extmin.y + ext.extmax.y) / 2


def _pivot(ctx: Ctx, args: dict, entities: list) -> tuple[float, float]:
    if args.get("cx") is not None or args.get("cy") is not None:
        cx, cy = _centre(entities)
        return (ctx.length(args["cx"], "cx") if args.get("cx") is not None else cx,
                ctx.length(args["cy"], "cy") if args.get("cy") is not None else cy)
    return _centre(entities)


def _number(args: dict, key: str, what: str, *, default=None) -> float:
    v = args.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise OpError(f"{what}: '{key}' must be a number.")
    try:
        f = float(v)
    except ValueError as e:
        raise OpError(f"{what}: '{key}' must be a number, got {v!r}.") from e
    if not math.isfinite(f):
        raise OpError(f"{what}: '{key}' must be finite.")
    return f


# ── operations ──────────────────────────────────────────────────────────────

OPS: dict[str, "OpSpec"] = {}


@dataclass
class OpSpec:
    name: str
    fn: Callable[[Ctx, dict], None]
    keys: set[str]
    doc: str


def op(name: str, keys: Iterable[str], doc: str):
    def deco(fn):
        OPS[name] = OpSpec(name, fn, set(keys), doc)
        return fn

    return deco


@op("move", ["selector", "dx", "dy"], 'move(selector, dx, dy)  shift entities. Distances are numbers in drawing units or strings like "2m", "500mm". +x is right/east, +y is up/north.')
def _move(ctx: Ctx, a: dict) -> None:
    ents = resolve(ctx, a.get("selector"))
    dx = ctx.length(a.get("dx", 0), "dx")
    dy = ctx.length(a.get("dy", 0), "dy")
    if dx == 0 and dy == 0:
        raise OpError("move: give a non-zero dx or dy.")
    n = _transform(ctx, ents, Matrix44.translate(dx, dy, 0))
    ctx.out.summaries.append(f"Move {_noun(ents, ctx)} by ({ctx.show(dx)}, {ctx.show(dy)})" if n == len(ents) else f"Move {n:,} entities by ({ctx.show(dx)}, {ctx.show(dy)})")


def _copy_entities(ctx: Ctx, ents: list, matrix: Matrix44) -> int:
    made = 0
    for e in ents:
        try:
            c = e.copy()
            ctx.msp.add_entity(c)
            c.transform(matrix)
            ctx.out.touched.created.add(c.dxf.handle)
            made += 1
        except (NotImplementedError, TypeError, DXFError, AttributeError):
            continue
    return made


@op("copy", ["selector", "dx", "dy"], "copy(selector, dx, dy)  duplicate entities, offset by dx, dy.")
def _copy(ctx: Ctx, a: dict) -> None:
    ents = resolve(ctx, a.get("selector"))
    dx, dy = ctx.length(a.get("dx", 0), "dx"), ctx.length(a.get("dy", 0), "dy")
    made = _copy_entities(ctx, ents, Matrix44.translate(dx, dy, 0))
    if not made:
        raise OpError("copy: none of the selected entities can be copied.")
    ctx.out.summaries.append(f"Copy {_noun(ents, ctx)} by ({ctx.show(dx)}, {ctx.show(dy)})")


@op("array", ["selector", "count", "dx", "dy"], "array(selector, count, dx, dy)  make `count` extra copies, each offset a further dx, dy from the last.")
def _array(ctx: Ctx, a: dict) -> None:
    ents = resolve(ctx, a.get("selector"))
    count = int(_number(a, "count", "array"))
    if not 1 <= count <= MAX_ARRAY:
        raise OpError(f"array: count must be between 1 and {MAX_ARRAY}.")
    if len(ents) * count > MAX_AFFECTED:
        raise OpError(f"array: that would create more than {MAX_AFFECTED:,} entities.")
    dx, dy = ctx.length(a.get("dx", 0), "dx"), ctx.length(a.get("dy", 0), "dy")
    if dx == 0 and dy == 0:
        raise OpError("array: give a non-zero dx or dy.")
    total = 0
    for i in range(1, count + 1):
        total += _copy_entities(ctx, ents, Matrix44.translate(dx * i, dy * i, 0))
    if not total:
        raise OpError("array: none of the selected entities can be copied.")
    ctx.out.summaries.append(f"Array {_noun(ents, ctx)} ×{count} at ({ctx.show(dx)}, {ctx.show(dy)}) steps")


@op("rotate", ["selector", "angle", "cx", "cy"], "rotate(selector, angle, cx?, cy?)  rotate by `angle` degrees anticlockwise about (cx, cy), default the selection's centre.")
def _rotate(ctx: Ctx, a: dict) -> None:
    ents = resolve(ctx, a.get("selector"))
    angle = _number(a, "angle", "rotate")
    if angle % 360 == 0:
        raise OpError("rotate: angle is zero (or a full turn).")
    cx, cy = _pivot(ctx, a, ents)
    m = Matrix44.chain(Matrix44.translate(-cx, -cy, 0), Matrix44.z_rotate(math.radians(angle)), Matrix44.translate(cx, cy, 0))
    _transform(ctx, ents, m)
    ctx.out.summaries.append(f"Rotate {_noun(ents, ctx)} by {angle:g}°")


@op("scale", ["selector", "factor", "cx", "cy"], "scale(selector, factor, cx?, cy?)  uniform scale about (cx, cy), default the selection's centre.")
def _scale(ctx: Ctx, a: dict) -> None:
    ents = resolve(ctx, a.get("selector"))
    f = _number(a, "factor", "scale")
    if not 1e-4 <= f <= 1e4:
        raise OpError("scale: factor must be between 0.0001 and 10000.")
    if f == 1:
        raise OpError("scale: factor 1 changes nothing.")
    cx, cy = _pivot(ctx, a, ents)
    m = Matrix44.chain(Matrix44.translate(-cx, -cy, 0), Matrix44.scale(f, f, f), Matrix44.translate(cx, cy, 0))
    _transform(ctx, ents, m)
    ctx.out.summaries.append(f"Scale {_noun(ents, ctx)} by {f:g}×")


@op("delete", ["selector"], "delete(selector)  remove entities.")
def _delete(ctx: Ctx, a: dict) -> None:
    ents = resolve(ctx, a.get("selector"))
    noun = _noun(ents, ctx)  # describe them while they still exist
    for e in ents:
        h = e.dxf.handle
        ctx.msp.delete_entity(e)
        ctx.out.touched.deleted.add(h)
    ctx.out.summaries.append(f"Delete {noun}")


def _ensure_layer(ctx: Ctx, name: str) -> None:
    if not ctx.doc.layers.has_entry(name):
        ctx.doc.layers.add(name)
        ctx.out.touched.tables = True


def _layer_name(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpError(f"{what}: layer name must be a non-empty string.")
    name = value.strip()
    if re.search(r'[<>/\\":;?*|=`]', name) or len(name) > 255:
        raise OpError(f"{what}: {name!r} isn't a valid layer name.")
    return name


@op("set_layer", ["selector", "layer"], "set_layer(selector, layer)  move entities onto a layer (created if it doesn't exist).")
def _set_layer(ctx: Ctx, a: dict) -> None:
    ents = resolve(ctx, a.get("selector"))
    name = _layer_name(a.get("layer"), "set_layer")
    _ensure_layer(ctx, name)
    n = 0
    for e in ents:
        if e.dxf.get("layer", "0") != name:
            e.dxf.layer = name
            ctx.out.touched.changed.add(e.dxf.handle)
            n += 1
    if not n:
        raise OpError(f"set_layer: everything selected is already on layer {name}.")
    ctx.out.summaries.append(f"Move {n:,} entities to layer {name}")


@op("set_color", ["selector", "color"], 'set_color(selector, color)  colour name or number 1-255, or "bylayer".')
def _set_color(ctx: Ctx, a: dict) -> None:
    ents = resolve(ctx, a.get("selector"))
    c = a.get("color")
    by_layer = isinstance(c, str) and c.strip().lower() in ("bylayer", "by layer")
    idx = 256 if by_layer else _color_index(c)
    for e in ents:
        e.dxf.color = idx
        if e.dxf.hasattr("true_color"):
            e.dxf.discard("true_color")
        ctx.out.touched.changed.add(e.dxf.handle)
    ctx.out.summaries.append(f"Set colour of {_noun(ents, ctx)} to {'by layer' if by_layer else c}")


@op("create_layer", ["name", "color"], "create_layer(name, color?)  add a layer.")
def _create_layer(ctx: Ctx, a: dict) -> None:
    name = _layer_name(a.get("name"), "create_layer")
    if ctx.doc.layers.has_entry(name):
        raise OpError(f"create_layer: layer {name} already exists.")
    layer = ctx.doc.layers.add(name)
    if a.get("color") is not None:
        layer.color = _color_index(a["color"])
    ctx.out.touched.tables = True
    ctx.out.summaries.append(f"Create layer {name}")


@op("layer_props", ["layer", "color", "on", "frozen", "locked"], "layer_props(layer, color?, on?, frozen?, locked?)  change a layer's colour or visibility.")
def _layer_props(ctx: Ctx, a: dict) -> None:
    name = _layer_name(a.get("layer"), "layer_props")
    if not ctx.doc.layers.has_entry(name):
        raise OpError(f"layer_props: there is no layer named {name}.")
    layer = ctx.doc.layers.get(name)
    changes = []
    if a.get("color") is not None:
        layer.color = _color_index(a["color"])  # the property keeps the layer's on/off state
        changes.append(f"colour {a['color']}")
    for key in ("on", "frozen", "locked"):
        if a.get(key) is None:
            continue
        v = a[key]
        if not isinstance(v, bool):
            raise OpError(f"layer_props: '{key}' must be true or false.")
        if key == "on":
            layer.on() if v else layer.off()
        elif key == "frozen":
            layer.freeze() if v else layer.thaw()
        else:
            layer.lock() if v else layer.unlock()
        changes.append(f"{key}={'yes' if v else 'no'}")
    if not changes:
        raise OpError("layer_props: nothing to change. Give color, on, frozen or locked.")
    ctx.out.touched.tables = True
    ctx.out.summaries.append(f"Layer {name}: {', '.join(changes)}")


@op("rename_layer", ["old", "new"], "rename_layer(old, new)  rename a layer; if `new` exists, the two are merged.")
def _rename_layer(ctx: Ctx, a: dict) -> None:
    old, new = _layer_name(a.get("old"), "rename_layer"), _layer_name(a.get("new"), "rename_layer")
    if old.lower() in PROTECTED_LAYERS:
        raise OpError(f"rename_layer: layer {old} can't be renamed.")
    if not ctx.doc.layers.has_entry(old):
        raise OpError(f"rename_layer: there is no layer named {old}.")
    if old.lower() == new.lower():
        raise OpError("rename_layer: old and new names are the same.")
    merge = ctx.doc.layers.has_entry(new)
    if not merge:
        src = ctx.doc.layers.get(old)
        dst = ctx.doc.layers.add(new)
        dst.dxf.color, dst.dxf.linetype = src.dxf.color, src.dxf.linetype
    n = 0
    msp_handles = {e.dxf.handle for e in ctx.msp}
    for e in list(ctx.doc.entitydb.values()):
        if e.dxf.hasattr("layer") and e.dxf.layer.lower() == old.lower():
            e.dxf.layer = new
            if e.dxf.handle in msp_handles:
                ctx.out.touched.changed.add(e.dxf.handle)
                n += 1
    try:
        ctx.doc.layers.remove(old)
    except DXFError as e:
        raise OpError(f"rename_layer: layer {old} can't be removed ({e}).") from e
    ctx.out.touched.tables = True
    ctx.out.summaries.append(f"{'Merge' if merge else 'Rename'} layer {old} → {new} ({n:,} {'entity' if n == 1 else 'entities'})")


@op("purge_unused_layers", [], "purge_unused_layers()  delete layers nothing is drawn on.")
def _purge_layers(ctx: Ctx, a: dict) -> None:
    used = {e.dxf.layer.lower() for e in ctx.doc.entitydb.values() if e.dxf.hasattr("layer")}
    current = str(ctx.doc.header.get("$CLAYER", "0")).lower()
    dead = [l.dxf.name for l in ctx.doc.layers if l.dxf.name.lower() not in used | PROTECTED_LAYERS | {current}]
    if not dead:
        raise OpError("purge_unused_layers: there are no unused layers.")
    for name in dead:
        ctx.doc.layers.remove(name)
    ctx.out.touched.tables = True
    shown = ", ".join(sorted(dead)[:8]) + (f" and {len(dead) - 8} more" if len(dead) > 8 else "")
    ctx.out.summaries.append(f"Delete {len(dead)} unused layer{'s' if len(dead) != 1 else ''}: {shown}")


def _target_layer(ctx: Ctx, a: dict, what: str) -> dict:
    name = _layer_name(a["layer"], what) if a.get("layer") else "0"
    _ensure_layer(ctx, name)
    return {"layer": name}


def _created(ctx: Ctx, entity, label: str) -> None:
    ctx.out.touched.created.add(entity.dxf.handle)
    ctx.out.summaries.append(label)


@op("add_line", ["x1", "y1", "x2", "y2", "layer"], "add_line(x1, y1, x2, y2, layer?)")
def _add_line(ctx: Ctx, a: dict) -> None:
    p = [ctx.length(a.get(k), k) for k in ("x1", "y1", "x2", "y2")]
    if p[0] == p[2] and p[1] == p[3]:
        raise OpError("add_line: start and end are the same point.")
    e = ctx.msp.add_line((p[0], p[1]), (p[2], p[3]), dxfattribs=_target_layer(ctx, a, "add_line"))
    _created(ctx, e, f"Add a line of length {ctx.show(math.hypot(p[2] - p[0], p[3] - p[1]))}")


@op("add_polyline", ["points", "closed", "layer"], "add_polyline(points=[[x,y],...], closed?, layer?)")
def _add_polyline(ctx: Ctx, a: dict) -> None:
    pts = a.get("points")
    if not isinstance(pts, list) or len(pts) < 2 or len(pts) > 5000:
        raise OpError("add_polyline: points must be a list of 2 to 5000 [x, y] pairs.")
    try:
        xy = [(ctx.length(p[0], "points"), ctx.length(p[1], "points")) for p in pts]
    except (TypeError, IndexError, KeyError) as e:
        raise OpError("add_polyline: each point must be [x, y].") from e
    e = ctx.msp.add_lwpolyline(xy, close=bool(a.get("closed")), dxfattribs=_target_layer(ctx, a, "add_polyline"))
    _created(ctx, e, f"Add a {'closed ' if a.get('closed') else ''}polyline with {len(xy)} points")


@op("add_rect", ["x", "y", "width", "height", "layer"], "add_rect(x, y, width, height, layer?)  x, y is the lower-left corner.")
def _add_rect(ctx: Ctx, a: dict) -> None:
    x, y = ctx.length(a.get("x"), "x"), ctx.length(a.get("y"), "y")
    w, h = ctx.length(a.get("width"), "width"), ctx.length(a.get("height"), "height")
    if w == 0 or h == 0:
        raise OpError("add_rect: width and height can't be zero.")
    e = ctx.msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True, dxfattribs=_target_layer(ctx, a, "add_rect"))
    _created(ctx, e, f"Add a {ctx.show(abs(w))} × {ctx.show(abs(h))} rectangle")


@op("add_circle", ["cx", "cy", "radius", "layer"], "add_circle(cx, cy, radius, layer?)")
def _add_circle(ctx: Ctx, a: dict) -> None:
    cx, cy, r = ctx.length(a.get("cx"), "cx"), ctx.length(a.get("cy"), "cy"), ctx.length(a.get("radius"), "radius")
    if r <= 0:
        raise OpError("add_circle: radius must be positive.")
    e = ctx.msp.add_circle((cx, cy), r, dxfattribs=_target_layer(ctx, a, "add_circle"))
    _created(ctx, e, f"Add a circle of radius {ctx.show(r)}")


@op("add_text", ["x", "y", "text", "height", "rotation", "layer"], "add_text(x, y, text, height?, rotation?, layer?)")
def _add_text(ctx: Ctx, a: dict) -> None:
    text = a.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise OpError("add_text: text must be a non-empty string (max 500 characters).")
    x, y = ctx.length(a.get("x"), "x"), ctx.length(a.get("y"), "y")
    h = ctx.length(a["height"], "height") if a.get("height") is not None else ctx.text_height
    if h <= 0:
        raise OpError("add_text: height must be positive.")
    attribs = {**_target_layer(ctx, a, "add_text"), "height": h, "rotation": _number(a, "rotation", "add_text", default=0)}
    e = ctx.msp.add_text(text, dxfattribs=attribs)
    e.set_placement((x, y))
    _created(ctx, e, f"Add text “{text[:40]}”")


def _case_like(right: str, found: str) -> str:
    if found.isupper():
        return right.upper()
    if found[:1].isupper():
        return right[:1].upper() + right[1:]
    return right


@op("replace_text", ["find", "replace", "selector", "case_sensitive", "match_case_of_found"], "replace_text(find, replace, selector?, case_sensitive?)  replace text in TEXT/MTEXT/block attributes; the whole drawing unless a selector is given.")
def _replace_text(ctx: Ctx, a: dict) -> None:
    find, repl = a.get("find"), a.get("replace")
    if not isinstance(find, str) or not find or not isinstance(repl, str):
        raise OpError("replace_text: 'find' (non-empty) and 'replace' must be strings.")
    flags = 0 if a.get("case_sensitive") else re.IGNORECASE
    pattern = re.compile(re.escape(find), flags)
    if a.get("selector"):
        ents = resolve(ctx, a["selector"])
    else:
        ents = [e for e in ctx.msp if e.dxftype() in ("TEXT", "MTEXT", "INSERT")]
    hits = changed = 0
    for e in ents:
        kind = e.dxftype()
        targets = []
        if kind == "MTEXT":
            targets.append((e, "text"))
        elif kind == "TEXT":
            targets.append((e, "text"))
        elif kind == "INSERT":
            targets.extend((att, "text") for att in e.attribs)
        touched = False
        for obj, field_name in targets:
            current = obj.text if kind == "MTEXT" and obj is e else obj.dxf.get(field_name, "")
            if a.get("match_case_of_found"):
                new, n = pattern.subn(lambda m: _case_like(repl, m.group(0)), current)
            else:
                new, n = pattern.subn(repl.replace("\\", "\\\\"), current)
            if n:
                if kind == "MTEXT" and obj is e:
                    obj.text = new
                else:
                    obj.dxf.set(field_name, new)
                hits += n
                touched = True
        if touched:
            changed += 1
            ctx.out.touched.changed.add(e.dxf.handle)
    if not hits:
        raise OpError(f"replace_text: no text contains {find!r}.")
    ctx.out.summaries.append(f"Replace “{find}” with “{repl}” in {changed:,} text item{'s' if changed != 1 else ''} ({hits:,} occurrence{'s' if hits != 1 else ''})")


# ── cleanup ─────────────────────────────────────────────────────────────────


def _optional(ctx: Ctx, a: dict, *, types: Optional[set] = None) -> list:
    """Entities a selector matches, or every model-space entity (of ``types``) when none is given."""
    if a.get("selector"):
        ents = resolve(ctx, a["selector"])
    else:
        ents = list(ctx.msp)
    if types is not None:
        ents = [e for e in ents if e.dxftype() in types]
    return ents


@op("explode", ["selector"], "explode(selector)  break block references, polylines and dimensions into their parts.")
def _explode(ctx: Ctx, a: dict) -> None:
    ents = [e for e in resolve(ctx, a.get("selector")) if e.dxftype() in ("INSERT", "LWPOLYLINE", "POLYLINE", "DIMENSION", "ARC_DIMENSION")]
    if not ents:
        raise OpError("explode: nothing selected can be exploded (block references, polylines or dimensions).")
    noun = _noun(ents, ctx)
    made = 0
    for e in ents:
        h = e.dxf.handle
        try:
            parts = e.explode()
        except (DXFError, TypeError, ValueError, AttributeError) as ex:
            ctx.out.warnings.append(f"Couldn't explode {e.dxftype()} {h}: {ex}")
            continue
        ctx.out.touched.deleted.add(h)
        for part in parts:
            ctx.out.touched.created.add(part.dxf.handle)
            made += 1
    if not made:
        raise OpError("explode: nothing could be exploded.")
    ctx.out.summaries.append(f"Explode {noun} into {made:,} pieces")


def _zero_z(e) -> bool:
    """Put an entity at elevation 0. True if anything changed."""
    kind = e.dxftype()
    d = e.dxf
    changed = False

    def flat(attr):
        nonlocal changed
        if d.hasattr(attr):
            v = d.get(attr)
            if abs(v.z) > 1e-9:
                d.set(attr, (v.x, v.y, 0))
                changed = True

    if kind == "LINE":
        flat("start"); flat("end")
    elif kind in ("POINT",):
        flat("location")
    elif kind in ("CIRCLE", "ARC", "ELLIPSE"):
        flat("center")
        if kind == "ELLIPSE":
            flat("major_axis")
    elif kind == "LWPOLYLINE":
        if abs(d.get("elevation", 0)) > 1e-9:
            d.elevation = 0
            changed = True
    elif kind == "POLYLINE":
        flat("elevation")
        for v in e.vertices:
            if abs(v.dxf.location.z) > 1e-9:
                loc = v.dxf.location
                v.dxf.location = (loc.x, loc.y, 0)
                changed = True
    elif kind in ("TEXT", "MTEXT", "INSERT"):
        flat("insert")
        flat("align_point")
    elif kind == "SPLINE":
        for name in ("control_points", "fit_points"):
            pts = list(getattr(e, name))
            if any(abs(p[2]) > 1e-9 for p in pts):
                setattr(e, name, [(p[0], p[1], 0) for p in pts])
                changed = True
    elif kind == "HATCH":
        flat("elevation")
    return changed


@op("flatten", ["selector"], "flatten(selector?)  move everything (or the selection) to elevation 0, fixing drawings that are subtly 3D.")
def _flatten(ctx: Ctx, a: dict) -> None:
    n = 0
    for e in _optional(ctx, a):
        try:
            if _zero_z(e):
                ctx.out.touched.changed.add(e.dxf.handle)
                n += 1
        except (DXFError, AttributeError, TypeError, ValueError):
            continue
    if not n:
        raise OpError("flatten: everything is already at elevation 0.")
    ctx.out.summaries.append(f"Flatten {n:,} entities to elevation 0")


def _unused_blocks(doc: Drawing) -> list[str]:
    from ezdxf.blkrefs import BlockReferenceCounter

    counter = BlockReferenceCounter(doc)
    out = []
    for blk in doc.blocks:
        name = blk.name
        if name.startswith("*") or name.upper().startswith("_"):  # layouts, anonymous, arrowheads
            continue
        if counter.by_name(name) == 0:
            out.append(name)
    return out


@op("purge_unused_blocks", [], "purge_unused_blocks()  delete block definitions nothing uses.")
def _purge_blocks(ctx: Ctx, a: dict) -> None:
    removed: list[str] = []
    for _ in range(8):  # nested blocks free up their children
        dead = _unused_blocks(ctx.doc)
        if not dead:
            break
        for name in dead:
            try:
                ctx.doc.blocks.delete_block(name, safe=False)
                removed.append(name)
            except (DXFError, KeyError, ValueError):
                continue
    if not removed:
        raise OpError("purge_unused_blocks: there are no unused blocks.")
    ctx.out.touched.tables = True
    shown = ", ".join(sorted(removed)[:8]) + (f" and {len(removed) - 8} more" if len(removed) > 8 else "")
    ctx.out.summaries.append(f"Delete {len(removed)} unused block{'s' if len(removed) != 1 else ''}: {shown}")


def _signature(e, tol: float, ignore_layer: bool):
    q = lambda v: round(float(v) / tol)  # noqa: E731
    kind = e.dxftype()
    d = e.dxf
    base = (kind,) if ignore_layer else (kind, d.get("layer", "0"))
    try:
        if kind == "LINE":
            a, b = (q(d.start.x), q(d.start.y), q(d.start.z)), (q(d.end.x), q(d.end.y), q(d.end.z))
            return base + tuple(sorted((a, b)))
        if kind == "CIRCLE":
            return base + (q(d.center.x), q(d.center.y), q(d.radius))
        if kind == "ARC":
            return base + (q(d.center.x), q(d.center.y), q(d.radius), round(d.start_angle, 3) % 360, round(d.end_angle, 3) % 360)
        if kind == "LWPOLYLINE":
            pts = tuple((q(x), q(y), round(bulge, 4)) for x, y, _s, _e, bulge in e.get_points("xyseb"))
            return base + (bool(e.closed), pts)
        if kind == "POINT":
            return base + (q(d.location.x), q(d.location.y))
        if kind == "TEXT":
            return base + (q(d.insert.x), q(d.insert.y), d.get("text", ""), q(d.get("height", 1)))
        if kind == "MTEXT":
            return base + (q(d.insert.x), q(d.insert.y), e.text, q(d.get("char_height", 1)))
        if kind == "INSERT":
            return base + (d.get("name"), q(d.insert.x), q(d.insert.y), round(d.get("rotation", 0), 3),
                           round(d.get("xscale", 1), 4), round(d.get("yscale", 1), 4),
                           tuple(sorted((att.dxf.tag, att.dxf.text) for att in e.attribs)))
    except (AttributeError, TypeError, ValueError):
        return None
    return None


@op("delete_duplicates", ["selector", "tolerance", "ignore_layer"], "delete_duplicates(selector?, tolerance?, ignore_layer?)  remove exact copies drawn on top of each other (lines, arcs, circles, polylines, text, blocks). Keeps the first.")
def _delete_duplicates(ctx: Ctx, a: dict) -> None:
    tol = ctx.length(a.get("tolerance"), "tolerance") if a.get("tolerance") is not None else 1e-4
    if tol <= 0:
        raise OpError("delete_duplicates: tolerance must be positive.")
    seen: dict = {}
    dupes = []
    for e in _optional(ctx, a):
        sig = _signature(e, tol, bool(a.get("ignore_layer")))
        if sig is None:
            continue
        if sig in seen:
            dupes.append(e)
        else:
            seen[sig] = e
    if not dupes:
        raise OpError("delete_duplicates: no duplicates found.")
    noun = _noun(dupes, ctx)
    for e in dupes:
        h = e.dxf.handle
        ctx.msp.delete_entity(e)
        ctx.out.touched.deleted.add(h)
    ctx.out.summaries.append(f"Delete {noun} that duplicate other entities exactly")


UNIT_CODES = {"in": 1, "inch": 1, "inches": 1, "ft": 2, "foot": 2, "feet": 2, "mm": 4, "millimetres": 4, "millimeters": 4,
              "cm": 5, "centimetres": 5, "centimeters": 5, "m": 6, "metres": 6, "meters": 6, "km": 7, "yd": 10, "yards": 10, "dm": 14}


@op("set_units", ["units", "convert"], 'set_units(units, convert?)  units: mm|cm|m|km|in|ft|yd. convert=false (default) only corrects the label; convert=true rescales the geometry so real sizes stay the same.')
def _set_units(ctx: Ctx, a: dict) -> None:
    from .units import INSUNITS

    raw = str(a.get("units", "")).strip().lower()
    code = UNIT_CODES.get(raw)
    if code is None:
        raise OpError(f"set_units: unknown unit {a.get('units')!r}. Use mm, cm, m, km, in, ft or yd.")
    new_name, _short, new_m = INSUNITS[code]
    current = int(ctx.doc.header.get("$INSUNITS", 0) or 0)
    if a.get("convert"):
        if current == 0 or INSUNITS.get(current, (0, 0, None))[2] is None:
            raise OpError("set_units: the drawing has no units recorded, so it can't be converted. Set the units first (convert=false).")
        old_m = INSUNITS[current][2]
        factor = old_m / new_m
        if abs(factor - 1) < 1e-12:
            raise OpError("set_units: the drawing is already in those units.")
        _transform(ctx, list(ctx.msp), Matrix44.scale(factor, factor, factor))
        ctx.doc.header["$INSUNITS"] = code
        ctx.out.touched.tables = True
        ctx.out.summaries.append(f"Convert the drawing from {INSUNITS[current][0]} to {new_name} (geometry ×{factor:g})")
        return
    if current == code:
        raise OpError(f"set_units: the drawing is already labelled {new_name}.")
    ctx.doc.header["$INSUNITS"] = code
    ctx.doc.header["$MEASUREMENT"] = 0 if code in (1, 2, 10) else 1
    ctx.out.touched.tables = True
    ctx.out.summaries.append(f"Label the drawing's units as {new_name} (geometry unchanged)")


_TEXTS = {"TEXT", "MTEXT"}


@op("set_text_style", ["selector", "style", "font"], 'set_text_style(selector?, style, font?)  put text on a text style; with `font` (e.g. "arial.ttf") the style is created or its font changed.')
def _set_text_style(ctx: Ctx, a: dict) -> None:
    name = a.get("style")
    if not isinstance(name, str) or not name.strip():
        raise OpError("set_text_style: 'style' must be a style name.")
    name = name.strip()
    font = a.get("font")
    if not ctx.doc.styles.has_entry(name):
        if not font:
            have = ", ".join(s.dxf.name for s in ctx.doc.styles)
            raise OpError(f"set_text_style: there is no text style {name!r} (have: {have}). Give a font to create it.")
        ctx.doc.styles.add(name, font=str(font))
        ctx.out.touched.tables = True
    elif font:
        ctx.doc.styles.get(name).dxf.font = str(font)
        ctx.out.touched.tables = True
    ents = _optional(ctx, a, types=_TEXTS)
    if not ents:
        raise OpError("set_text_style: no text to change.")
    for e in ents:
        e.dxf.style = name
        ctx.out.touched.changed.add(e.dxf.handle)
    ctx.out.summaries.append(f"Put {len(ents):,} text item{'s' if len(ents) != 1 else ''} on style {name}")


def _text_height(e) -> float:
    return float(e.dxf.get("char_height", 1.0) if e.dxftype() == "MTEXT" else e.dxf.get("height", 1.0))


def _set_height(e, h: float) -> None:
    if e.dxftype() == "MTEXT":
        e.dxf.char_height = h
    else:
        e.dxf.height = h


@op("set_text_height", ["selector", "height"], "set_text_height(selector?, height)  set the height of text.")
def _set_text_height(ctx: Ctx, a: dict) -> None:
    h = ctx.length(a.get("height"), "height")
    if h <= 0:
        raise OpError("set_text_height: height must be positive.")
    ents = [e for e in _optional(ctx, a, types=_TEXTS) if abs(_text_height(e) - h) > 1e-9]
    if not ents:
        raise OpError("set_text_height: all of that text is already that height.")
    for e in ents:
        _set_height(e, h)
        ctx.out.touched.changed.add(e.dxf.handle)
    ctx.out.summaries.append(f"Set {len(ents):,} text item{'s' if len(ents) != 1 else ''} to height {ctx.show(h)}")


@op("normalize_text_heights", ["selector", "heights"], "normalize_text_heights(selector?, heights?)  snap every text height to the nearest of `heights` (default: the 3 most common heights already used).")
def _normalize_heights(ctx: Ctx, a: dict) -> None:
    from collections import Counter

    ents = _optional(ctx, a, types=_TEXTS)
    if not ents:
        raise OpError("normalize_text_heights: there is no text.")
    if a.get("heights"):
        if not isinstance(a["heights"], list) or not 1 <= len(a["heights"]) <= 12:
            raise OpError("normalize_text_heights: heights must be a list of 1 to 12 values.")
        targets = sorted({ctx.length(v, "heights") for v in a["heights"]})
        if targets[0] <= 0:
            raise OpError("normalize_text_heights: heights must be positive.")
    else:
        common = Counter(round(_text_height(e), 6) for e in ents).most_common(3)
        targets = sorted(h for h, _ in common)
    n = 0
    for e in ents:
        h = _text_height(e)
        best = min(targets, key=lambda t: abs(t - h))
        if abs(best - h) > 1e-9:
            _set_height(e, best)
            ctx.out.touched.changed.add(e.dxf.handle)
            n += 1
    if not n:
        raise OpError("normalize_text_heights: every text height is already standard.")
    shown = ", ".join(ctx.show(t) for t in targets)
    ctx.out.summaries.append(f"Snap {n:,} text height{'s' if n != 1 else ''} to the standard set {shown}")


@op("replace_fonts", ["find", "font"], 'replace_fonts(find?, font)  change text styles whose font matches `find` (default "*.shx") to `font`, e.g. "arial.ttf".')
def _replace_fonts(ctx: Ctx, a: dict) -> None:
    font = a.get("font")
    if not isinstance(font, str) or not font.strip():
        raise OpError("replace_fonts: 'font' must be a font file name such as arial.ttf.")
    pattern = str(a.get("find") or "*.shx").lower()
    changed = []
    for style in ctx.doc.styles:
        current = str(style.dxf.get("font", "") or "")
        if fnmatch.fnmatchcase(current.lower(), pattern) and current.lower() != font.lower():
            style.dxf.font = font.strip()
            if style.dxf.hasattr("bigfont"):
                style.dxf.discard("bigfont")
            changed.append(style.dxf.name)
    if not changed:
        raise OpError(f"replace_fonts: no text style uses a font matching {pattern!r}.")
    ctx.out.touched.tables = True
    names = {c.lower() for c in changed}
    for e in ctx.msp:
        if e.dxftype() in _TEXTS and str(e.dxf.get("style", "Standard")).lower() in names:
            ctx.out.touched.changed.add(e.dxf.handle)
    ctx.out.summaries.append(f"Switch {len(changed)} text style{'s' if len(changed) != 1 else ''} ({', '.join(changed[:6])}) to {font}")


@op("fill_attributes", ["values", "block", "selector"], 'fill_attributes(values={"TAG": "value"}, block?, selector?)  fill block attributes (a title block, for example) and {{TAG}} placeholders in text.')
def _fill_attributes(ctx: Ctx, a: dict) -> None:
    values = a.get("values")
    if not isinstance(values, dict) or not values or len(values) > 200:
        raise OpError("fill_attributes: 'values' must be an object of up to 200 TAG: value pairs.")
    upper = {str(k).strip().upper(): str(v) for k, v in values.items()}
    if a.get("selector"):
        ents = resolve(ctx, a["selector"])
    else:
        ents = list(ctx.msp)
    block = str(a["block"]).lower() if a.get("block") else None
    filled = 0
    for e in ents:
        kind = e.dxftype()
        hit = False
        if kind == "INSERT":
            if block and not fnmatch.fnmatchcase(e.dxf.get("name", "").lower(), block):
                continue
            for att in e.attribs:
                tag = att.dxf.get("tag", "").upper()
                if tag in upper and att.dxf.get("text", "") != upper[tag]:
                    att.dxf.text = upper[tag]
                    filled += 1
                    hit = True
        elif kind in _TEXTS and not block:
            text = e.text if kind == "MTEXT" else e.dxf.get("text", "")
            new = re.sub(r"\{\{\s*([A-Za-z0-9_\-]+)\s*\}\}", lambda m: upper.get(m.group(1).upper(), m.group(0)), text)
            if new != text:
                if kind == "MTEXT":
                    e.text = new
                else:
                    e.dxf.text = new
                filled += 1
                hit = True
        if hit:
            ctx.out.touched.changed.add(e.dxf.handle)
    if not filled:
        raise OpError("fill_attributes: no block attribute or {{TAG}} placeholder matched those tags: " + ", ".join(list(upper)[:10]))
    ctx.out.summaries.append(f"Fill {filled:,} field{'s' if filled != 1 else ''} ({', '.join(list(upper)[:6])})")


_ORDERS = ("left-right", "top-bottom", "right-left", "bottom-top")


def _position(e):
    d = e.dxf
    p = d.get("insert") or d.get("location") or d.get("center")
    return (p.x, p.y) if p is not None else (0.0, 0.0)


@op("renumber", ["selector", "match", "prefix", "start", "step", "order", "pad", "tag"], 'renumber(selector?, match?, prefix?, start=1, step=1, order="left-right", pad?, tag?)  renumber labels (circuits, panels, doors, grid tags) in reading order. `match` is a regex the labels must fit, e.g. "^C-\\d+$"; `tag` renumbers a block attribute instead of text.')
def _renumber(ctx: Ctx, a: dict) -> None:
    order = str(a.get("order") or "left-right").lower()
    if order not in _ORDERS:
        raise OpError(f"renumber: order must be one of {', '.join(_ORDERS)}.")
    try:
        rx = re.compile(str(a["match"])) if a.get("match") else None
    except re.error as ex:
        raise OpError(f"renumber: 'match' isn't a valid pattern ({ex}).") from ex
    tag = str(a["tag"]).upper() if a.get("tag") else None
    start = int(_number(a, "start", "renumber", default=1))
    step = int(_number(a, "step", "renumber", default=1))
    if step == 0:
        raise OpError("renumber: step can't be 0.")
    pad = int(_number(a, "pad", "renumber", default=0))
    prefix = a.get("prefix")

    items = []  # (entity, getter, setter, text)
    for e in _optional(ctx, a):
        kind = e.dxftype()
        if tag:
            if kind != "INSERT":
                continue
            for att in e.attribs:
                if att.dxf.get("tag", "").upper() == tag:
                    items.append((e, att, "attrib", att.dxf.get("text", "")))
        elif kind in _TEXTS:
            items.append((e, e, kind, e.text if kind == "MTEXT" else e.dxf.get("text", "")))
    if rx:
        items = [it for it in items if rx.search(it[3])]
    if not prefix:
        items = [it for it in items if re.search(r"\d+", it[3])]
    if not items:
        raise OpError("renumber: no labels matched." + (" (Without a prefix, labels need a number in them to renumber.)" if not prefix else ""))
    if len(items) > 5000:
        raise OpError("renumber: more than 5,000 labels matched. Narrow it down.")

    heights = sorted(_text_height(it[1]) if it[2] in _TEXTS else 1.0 for it in items)
    band = max(heights[len(heights) // 2], 1e-6)
    pos = {id(it): _position(it[0]) for it in items}
    if order in ("left-right", "right-left"):
        key = lambda it: (-round(pos[id(it)][1] / band), pos[id(it)][0] * (1 if order == "left-right" else -1))  # noqa: E731
    else:
        key = lambda it: (round(pos[id(it)][0] / band), -pos[id(it)][1] * (1 if order == "top-bottom" else -1))  # noqa: E731
    items.sort(key=key)

    n = start
    changed = 0
    for e, target, kind, text in items:
        if prefix is not None:
            new = f"{prefix}{str(n).zfill(pad) if pad else n}"
        else:
            m = list(re.finditer(r"\d+", text))[-1]
            width = pad or (len(m.group(0)) if m.group(0).startswith("0") else 0)
            new = text[: m.start()] + (str(n).zfill(width) if width else str(n)) + text[m.end():]
        if new != text:
            if kind == "MTEXT":
                target.text = new
            else:
                target.dxf.text = new
            ctx.out.touched.changed.add(e.dxf.handle)
            changed += 1
        n += step
    if not changed:
        raise OpError("renumber: the labels are already numbered in that order.")
    ctx.out.summaries.append(f"Renumber {len(items):,} label{'s' if len(items) != 1 else ''} {order.replace('-', ' to ')} from {start} ({changed:,} changed)")


def _cloud_points(x0: float, y0: float, x1: float, y1: float, arc: float) -> list[tuple[float, float, float, float, float]]:
    """A rectangle traced with outward-bulging arcs: (x, y, start_width, end_width, bulge)."""
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    pts = []
    for i in range(4):
        ax, ay = corners[i]
        bx, by = corners[(i + 1) % 4]
        length = math.hypot(bx - ax, by - ay)
        n = max(1, round(length / arc))
        for k in range(n):
            t = k / n
            pts.append((ax + (bx - ax) * t, ay + (by - ay) * t, 0.0, 0.0, -0.45))
    return pts


def _bounds(ctx: Ctx, a: dict, what: str) -> tuple[float, float, float, float]:
    if a.get("bbox"):
        b = a["bbox"]
        if not isinstance(b, (list, tuple)) or len(b) != 4:
            raise OpError(f"{what}: bbox must be [xmin, ymin, xmax, ymax].")
        x0, y0, x1, y1 = (ctx.length(v, f"{what}.bbox") for v in b)
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    ents = resolve(ctx, a.get("selector"))
    ext = bbox.extents(ents, fast=True)
    if not ext.has_data:
        raise OpError(f"{what}: the selection has no extent.")
    return ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y


@op("revision_cloud", ["bbox", "selector", "layer", "rev", "arc"], 'revision_cloud(bbox | selector, layer?, rev?, arc?)  draw a revision cloud around an area or the selection; `rev` adds a revision tag such as "B".')
def _revision_cloud(ctx: Ctx, a: dict) -> None:
    x0, y0, x1, y1 = _bounds(ctx, a, "revision_cloud")
    size = max(x1 - x0, y1 - y0, 1e-6)
    margin = size * 0.06 + ctx.text_height
    x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
    arc = ctx.length(a["arc"], "arc") if a.get("arc") is not None else max((x1 - x0 + y1 - y0) / 24, ctx.text_height)
    if arc <= 0:
        raise OpError("revision_cloud: arc length must be positive.")
    layer = _layer_name(a["layer"], "revision_cloud") if a.get("layer") else "REV-CLOUD"
    if not ctx.doc.layers.has_entry(layer):
        ctx.doc.layers.add(layer, color=1)
        ctx.out.touched.tables = True
    pl = ctx.msp.add_lwpolyline(_cloud_points(x0, y0, x1, y1, arc), format="xyseb", close=True, dxfattribs={"layer": layer})
    ctx.out.touched.created.add(pl.dxf.handle)
    if a.get("rev"):
        rev = str(a["rev"])[:8]
        h = ctx.text_height * 1.4
        tri = ctx.msp.add_lwpolyline([(x1, y1), (x1 + h * 2.2, y1), (x1 + h * 1.1, y1 + h * 1.9)], close=True, dxfattribs={"layer": layer})
        txt = ctx.msp.add_text(rev, height=h * 0.8, dxfattribs={"layer": layer})
        txt.set_placement((x1 + h * 1.1, y1 + h * 0.45), align=TextEntityAlignment.BOTTOM_CENTER)
        ctx.out.touched.created.update({tri.dxf.handle, txt.dxf.handle})
    ctx.out.summaries.append(f"Draw a revision cloud{(' (rev ' + str(a['rev']) + ')') if a.get('rev') else ''} on layer {layer}")


@op("add_table", ["x", "y", "rows", "col_widths", "text_height", "layer", "title"], "add_table(x, y, rows=[[cell,...],...], col_widths?, text_height?, layer?, title?)  draw a table; x, y is the top-left corner; the first row is the header.")
def _add_table(ctx: Ctx, a: dict) -> None:
    rows = a.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > 500 or not all(isinstance(r, list) for r in rows):
        raise OpError("add_table: rows must be a list of 1 to 500 lists.")
    ncols = max(len(r) for r in rows)
    if not 1 <= ncols <= 30:
        raise OpError("add_table: tables can have 1 to 30 columns.")
    cells = [[str(c if c is not None else "")[:120] for c in r] + [""] * (ncols - len(r)) for r in rows]
    th = ctx.length(a["text_height"], "text_height") if a.get("text_height") is not None else ctx.text_height
    if th <= 0:
        raise OpError("add_table: text_height must be positive.")
    pad = th * 0.6
    if a.get("col_widths"):
        cw = a["col_widths"]
        if not isinstance(cw, list) or len(cw) != ncols:
            raise OpError(f"add_table: col_widths needs exactly {ncols} values.")
        widths = [ctx.length(v, "col_widths") for v in cw]
    else:
        widths = [max(len(r[c]) for r in cells) * th * 0.75 + pad * 2 for c in range(ncols)]
        widths = [max(w, th * 3) for w in widths]
    x, y = ctx.length(a.get("x"), "x"), ctx.length(a.get("y"), "y")
    layer = _layer_name(a["layer"], "add_table") if a.get("layer") else "A-ANNO-TABL"
    _ensure_layer(ctx, layer)
    rh = th + pad * 2
    total_w = sum(widths)
    top = y
    made = []
    if a.get("title"):
        t = ctx.msp.add_text(str(a["title"])[:120], height=th * 1.3, dxfattribs={"layer": layer})
        t.set_placement((x, top + pad))
        made.append(t)
    nrows = len(cells)
    for i in range(nrows + 1):
        made.append(ctx.msp.add_line((x, top - i * rh), (x + total_w, top - i * rh), dxfattribs={"layer": layer}))
    cx = x
    for c in range(ncols + 1):
        made.append(ctx.msp.add_line((cx, top), (cx, top - nrows * rh), dxfattribs={"layer": layer}))
        if c < ncols:
            cx += widths[c]
    for i, row in enumerate(cells):
        cx = x
        for c, value in enumerate(row):
            if value:
                t = ctx.msp.add_text(value, height=th, dxfattribs={"layer": layer})
                t.set_placement((cx + pad, top - (i + 1) * rh + pad))
                made.append(t)
            cx += widths[c]
    for e in made:
        ctx.out.touched.created.add(e.dxf.handle)
    ctx.out.summaries.append(f"Draw a {nrows}-row × {ncols}-column table on layer {layer}")


@op("rename_block", ["old", "new"], "rename_block(old, new)  rename a block definition (every reference follows).")
def _rename_block(ctx: Ctx, a: dict) -> None:
    old, new = str(a.get("old") or "").strip(), str(a.get("new") or "").strip()
    if not old or not new or re.search(r'[<>/\\":;?*|=`]', new):
        raise OpError("rename_block: give an existing block name and a valid new name.")
    if old not in ctx.doc.blocks:
        raise OpError(f"rename_block: there is no block named {old!r}.")
    if new in ctx.doc.blocks:
        raise OpError(f"rename_block: a block named {new!r} already exists.")
    ctx.doc.blocks.rename_block(old, new)
    # ezdxf renames the definition only; every reference (in model space, paper
    # space and inside other blocks) has to follow, or it would dangle.
    msp_handles = {e.dxf.handle for e in ctx.msp}
    for e in list(ctx.doc.entitydb.values()):
        if e.dxftype() == "INSERT" and e.dxf.get("name", "").lower() == old.lower():
            e.dxf.name = new
            if e.dxf.handle in msp_handles:
                ctx.out.touched.changed.add(e.dxf.handle)
    ctx.out.touched.tables = True
    ctx.out.summaries.append(f"Rename block {old} → {new}")


def _xref_block(ctx: Ctx, name, what: str):
    if not isinstance(name, str) or name not in ctx.doc.blocks:
        raise OpError(f"{what}: there is no block named {name!r}.")
    blk = ctx.doc.blocks.get(name)
    if not blk.block.is_xref:
        raise OpError(f"{what}: {name} isn't an external reference.")
    return blk


@op("set_xref_path", ["block", "path"], "set_xref_path(block, path)  point an external reference at a new file path.")
def _set_xref_path(ctx: Ctx, a: dict) -> None:
    blk = _xref_block(ctx, a.get("block"), "set_xref_path")
    path = a.get("path")
    if not isinstance(path, str) or not path.strip() or len(path) > 1024:
        raise OpError("set_xref_path: 'path' must be a file path.")
    old = blk.block.dxf.get("xref_path", "")
    blk.block.dxf.xref_path = path.strip()
    ctx.out.touched.tables = True
    ctx.out.summaries.append(f"Re-point xref {blk.name}: {old or '(none)'} → {path.strip()}")


@op("detach_xref", ["block"], "detach_xref(block)  remove an external reference and every placement of it.")
def _detach_xref(ctx: Ctx, a: dict) -> None:
    blk = _xref_block(ctx, a.get("block"), "detach_xref")
    name = blk.name
    refs = [e for e in ctx.msp if e.dxftype() == "INSERT" and e.dxf.get("name") == name]
    for e in refs:
        h = e.dxf.handle
        ctx.msp.delete_entity(e)
        ctx.out.touched.deleted.add(h)
    for layout in ctx.doc.layouts:
        if layout.name == "Model":
            continue
        for e in list(layout):
            if e.dxftype() == "INSERT" and e.dxf.get("name") == name:
                layout.delete_entity(e)
    try:
        ctx.doc.blocks.delete_block(name, safe=False)
    except (DXFError, KeyError) as ex:
        raise OpError(f"detach_xref: couldn't remove {name} ({ex}).") from ex
    ctx.out.touched.tables = True
    ctx.out.summaries.append(f"Detach xref {name} ({len(refs)} placement{'s' if len(refs) != 1 else ''})")


@op("map_layers", ["mapping", "colors"], 'map_layers(mapping={"old": "NEW", ...}, colors?={"NEW": 3})  rename or merge many layers at once (layer standards).')
def _map_layers(ctx: Ctx, a: dict) -> None:
    mapping = a.get("mapping")
    if not isinstance(mapping, dict) or not mapping or len(mapping) > 2000:
        raise OpError("map_layers: 'mapping' must be an object of up to 2000 old: new layer names.")
    existing = {l.dxf.name.lower(): l.dxf.name for l in ctx.doc.layers}
    pairs = []
    for old, new in mapping.items():
        if str(old).lower() in PROTECTED_LAYERS or str(old).lower() not in existing:
            continue
        new = _layer_name(new, "map_layers")
        if existing[str(old).lower()] == new:
            continue
        pairs.append((existing[str(old).lower()], new))
    if not pairs:
        raise OpError("map_layers: none of those layers exist or need renaming.")
    by_old = {o.lower(): n for o, n in pairs}
    for old, new in pairs:
        if not ctx.doc.layers.has_entry(new):
            src = ctx.doc.layers.get(old)
            dst = ctx.doc.layers.add(new)
            dst.dxf.color, dst.dxf.linetype = abs(src.dxf.color), src.dxf.linetype
    msp_handles = {e.dxf.handle for e in ctx.msp}
    moved = 0
    for e in list(ctx.doc.entitydb.values()):
        if e.dxf.hasattr("layer"):
            new = by_old.get(e.dxf.layer.lower())
            if new and new != e.dxf.layer:
                e.dxf.layer = new
                if e.dxf.handle in msp_handles:
                    ctx.out.touched.changed.add(e.dxf.handle)
                    moved += 1
    for old, _new in pairs:
        if ctx.doc.layers.has_entry(old) and old.lower() not in {n.lower() for _o, n in pairs}:
            try:
                ctx.doc.layers.remove(old)
            except DXFError:
                pass
    colors = a.get("colors") or {}
    if not isinstance(colors, dict):
        raise OpError("map_layers: 'colors' must be an object of layer: colour.")
    for name, c in colors.items():
        if ctx.doc.layers.has_entry(str(name)):
            ctx.doc.layers.get(str(name)).color = _color_index(c, "map_layers.colors")
    ctx.out.touched.tables = True
    merged = len(pairs) - len({n.lower() for _o, n in pairs})
    ctx.out.summaries.append(f"Rename {len(pairs)} layer{'s' if len(pairs) != 1 else ''} to the standard ({moved:,} objects moved" + (f", {merged} merged" if merged else "") + ")")


def vocabulary() -> str:
    """The operation list, in the form given to the model."""
    return "\n".join(spec.doc for spec in OPS.values())


SELECTOR_HELP = (
    'A selector is an object; every key you give must match (AND): '
    '{"layer": "A-WALL" or ["A-*", ...] (wildcards ok, case-insensitive), '
    '"type": line|polyline|circle|arc|text|block|dimension|hatch|spline|ellipse, '
    '"text": substring of text content, "color": name or 1-255, "block": block name (wildcards ok), '
    '"bbox": [xmin, ymin, xmax, ymax], "handles": ["2F", ...], "selection": true (the entities the person has clicked), "all": true}.'
)


def apply_ops(doc: Drawing, ops: list, units: Units, selection: list[str], text_height: float, *, max_ops: int = 12) -> Applied:
    """Carry out a list of operations on ``doc`` (mutating it). Raises OpError."""
    if not isinstance(ops, list) or not ops:
        raise OpError("ops must be a non-empty list.")
    if len(ops) > max_ops:
        raise OpError(f"At most {max_ops} operations per proposal; got {len(ops)}.")
    out = Applied()
    ctx = Ctx(doc, units, selection, text_height, out)
    for i, raw in enumerate(ops, 1):
        if not isinstance(raw, dict) or not isinstance(raw.get("op"), str):
            raise OpError(f"Operation {i} must be an object with an 'op' name.")
        spec = OPS.get(raw["op"])
        if spec is None:
            raise OpError(f"Operation {i}: unknown op {raw['op']!r}. Available: {', '.join(OPS)}.")
        extra = set(raw) - spec.keys - {"op", "optional"}
        if extra:
            raise OpError(f"Operation {i} ({spec.name}): unknown fields {sorted(extra)}. Allowed: {sorted(spec.keys)}.")
        try:
            spec.fn(ctx, {k: v for k, v in raw.items() if k != "optional"})
        except OpError as e:
            if raw.get("optional") is True:  # "if there's anything to do": nothing to do is fine
                out.warnings.append(f"Skipped {spec.name.replace('_', ' ')}: {str(e).removeprefix(spec.name + ': ').rstrip('. ')}")
                continue
            raise OpError(f"Operation {i} ({spec.name}): {e}" if not str(e).startswith(spec.name) else f"Operation {i}: {e}") from e
    # An entity that was changed and then deleted (or created and deleted) is just deleted / nothing.
    t = out.touched
    both = t.created & t.deleted  # made and then removed in the same proposal: never existed
    t.created -= both
    t.deleted -= both
    t.changed -= t.deleted | both
    return out
