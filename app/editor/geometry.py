"""Turn a drawing into something a browser can draw, and compare two states.

Everything reduces to two kinds of item:

* ``p`` — a polyline: flat ``[x, y, x, y, ...]``, ``z`` = closed. Lines, arcs,
  circles, ellipses, splines and hatch boundaries all become these.
* ``t`` — a piece of text with a position, height and rotation.

Block references are expanded, but every piece keeps the handle of the
top-level entity it came from, so clicking any part of a door selects the door.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

from ezdxf import colors as ezcolors
from ezdxf import path as ezpath
from ezdxf.document import Drawing

MAX_ITEMS = 250_000
MAX_INSERT_DEPTH = 4
_PATH_TYPES = {"LINE", "ARC", "CIRCLE", "ELLIPSE", "LWPOLYLINE", "POLYLINE", "SPLINE", "HATCH", "SOLID", "TRACE", "3DFACE"}
_TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB"}
_EXPAND_TYPES = {"INSERT", "DIMENSION", "LEADER", "MLEADER", "MULTILEADER", "ARC_DIMENSION"}
_QUIET_TYPES = {"POINT", "VIEWPORT", "IMAGE", "WIPEOUT", "ATTDEF", "XLINE", "RAY"}


@dataclass
class Scene:
    items: list[dict] = field(default_factory=list)
    extents: Optional[list[float]] = None  # minx, miny, maxx, maxy
    truncated: bool = False
    not_shown: Counter = field(default_factory=Counter)

    def by_handle(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for it in self.items:
            out.setdefault(it["h"], []).append(it)
        return out

    def to_json(self, rev: int) -> dict:
        return {
            "rev": rev,
            "extents": self.extents,
            "items": self.items,
            "truncated": self.truncated,
            "notShown": dict(self.not_shown),
        }


def aci_hex(aci: int) -> Optional[str]:
    """Colour index to ``#rrggbb``. 7 (white/black) is ``None``: the viewer's ink."""
    aci = abs(int(aci))
    if aci in (0, 7, 256) or aci > 255:
        return None
    r, g, b = ezcolors.DXF_DEFAULT_COLORS[aci] >> 16 & 255, ezcolors.DXF_DEFAULT_COLORS[aci] >> 8 & 255, ezcolors.DXF_DEFAULT_COLORS[aci] & 255
    return f"#{r:02x}{g:02x}{b:02x}"


def _layer_color(doc: Drawing, name: str) -> Optional[str]:
    try:
        return aci_hex(doc.layers.get(name).dxf.color)
    except Exception:  # noqa: BLE001 - missing layer, odd table: fall back to ink
        return None


def _color(doc: Drawing, prim, parent_layer: Optional[str], parent_color: Optional[str]) -> Optional[str]:
    if prim.dxf.hasattr("true_color"):
        return "#%06x" % (int(prim.dxf.true_color) & 0xFFFFFF)
    aci = prim.dxf.get("color", 256)
    if aci == 0:  # BYBLOCK
        return parent_color
    if aci != 256:
        return aci_hex(aci)
    layer = prim.dxf.get("layer", "0")
    if parent_layer is not None and layer == "0":
        layer = parent_layer
    return _layer_color(doc, layer)


def _expand(entity, depth: int) -> Iterator:
    """Primitives of an entity: block references and dimensions are opened up."""
    kind = entity.dxftype()
    if kind in _EXPAND_TYPES:
        if depth >= MAX_INSERT_DEPTH:
            return
        try:
            parts = list(entity.virtual_entities())
        except Exception:  # noqa: BLE001 - a block that can't be opened is skipped
            return
        for part in parts:
            yield from _expand(part, depth + 1)
        if kind == "INSERT":
            for attrib in getattr(entity, "attribs", []):
                yield attrib
    else:
        yield entity


def _round(v: float) -> float:
    return round(v, 3)


def _path_item(prim, dist_hint: float) -> Iterator[tuple[list[float], bool]]:
    path = ezpath.make_path(prim)
    cv = path.control_vertices()
    if not cv:
        return
    xs = [v.x for v in cv]
    ys = [v.y for v in cv]
    diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
    dist = max(min(diag / 150.0, dist_hint), dist_hint / 500.0, 1e-9)
    for sub in path.sub_paths():
        pts: list[float] = []
        for v in sub.flattening(dist):
            pts.append(_round(v.x))
            pts.append(_round(v.y))
        if len(pts) >= 4:
            yield pts, bool(sub.is_closed)


def _text_item(prim) -> Optional[dict]:
    kind = prim.dxftype()
    try:
        if kind == "MTEXT":
            value = prim.plain_text()
            height = float(prim.dxf.get("char_height", 1.0))
            ins = prim.dxf.insert
            td = prim.dxf.get("text_direction", None)
            rot = math.degrees(math.atan2(td.y, td.x)) if td is not None else float(prim.dxf.get("rotation", 0.0))
        else:
            value = prim.dxf.get("text", "")
            height = float(prim.dxf.get("height", 1.0))
            ins = prim.dxf.insert
            if (prim.dxf.get("halign", 0) or prim.dxf.get("valign", 0)) and prim.dxf.hasattr("align_point"):
                ins = prim.dxf.align_point
            rot = float(prim.dxf.get("rotation", 0.0))
    except Exception:  # noqa: BLE001
        return None
    if not str(value).strip():
        return None
    return {"k": "t", "x": _round(ins.x), "y": _round(ins.y), "s": _round(max(height, 1e-6)), "r": _round(rot), "v": str(value)[:300]}


def _entity_items(doc: Drawing, entity, dist_hint: float, not_shown: Counter) -> Iterator[dict]:
    handle = entity.dxf.handle
    layer = entity.dxf.get("layer", "0")
    top_color = _color(doc, entity, None, None)
    base = {"h": handle, "l": layer, "t": entity.dxftype()}
    for prim in _expand(entity, 0):
        kind = prim.dxftype()
        color = top_color if prim is entity else _color(doc, prim, layer, top_color)
        if kind in _TEXT_TYPES:
            item = _text_item(prim)
            if item:
                yield {**base, **item, "c": color}
        elif kind in _PATH_TYPES:
            try:
                for pts, closed in _path_item(prim, dist_hint):
                    yield {**base, "k": "p", "p": pts, "z": 1 if closed else 0, "c": color}
            except Exception:  # noqa: BLE001 - one bad entity must not blank the drawing
                not_shown[kind] += 1
        elif kind in _EXPAND_TYPES or kind in _QUIET_TYPES:
            continue
        else:
            not_shown[kind] += 1


def model_extents(doc: Drawing) -> Optional[list[float]]:
    from ezdxf import bbox

    try:
        box = bbox.extents(doc.modelspace(), fast=True)
    except Exception:  # noqa: BLE001
        return None
    if not box.has_data:
        return None
    return [_round(box.extmin.x), _round(box.extmin.y), _round(box.extmax.x), _round(box.extmax.y)]


def extract(doc: Drawing, handles: Optional[Iterable[str]] = None, max_items: int = MAX_ITEMS) -> Scene:
    """The drawable form of model space, or just of the given entities."""
    scene = Scene()
    if handles is None:
        extents = model_extents(doc)
        entities: Iterable = doc.modelspace()
    else:
        extents = None
        entities = (e for e in (doc.entitydb.get(h) for h in handles) if e is not None and e.is_alive)
    size = max(extents[2] - extents[0], extents[3] - extents[1]) if extents else 1000.0
    dist_hint = max(size / 1500.0, 1e-6)

    minx = miny = math.inf
    maxx = maxy = -math.inf
    for entity in entities:
        for item in _entity_items(doc, entity, dist_hint, scene.not_shown):
            if len(scene.items) >= max_items:
                scene.truncated = True
                break
            scene.items.append(item)
            if item["k"] == "p":
                p = item["p"]
                minx, maxx = min(minx, min(p[0::2])), max(maxx, max(p[0::2]))
                miny, maxy = min(miny, min(p[1::2])), max(maxy, max(p[1::2]))
            else:
                minx, maxx = min(minx, item["x"]), max(maxx, item["x"])
                miny, maxy = min(miny, item["y"]), max(maxy, item["y"])
        if scene.truncated:
            break
    if minx != math.inf:
        scene.extents = [_round(minx), _round(miny), _round(maxx), _round(maxy)]
    elif extents:
        scene.extents = extents
    return scene
