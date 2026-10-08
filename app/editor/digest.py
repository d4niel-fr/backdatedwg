"""A compact description of a drawing, for people and for the model.

A drawing can hold hundreds of thousands of entities, so the model is never
shown the file. It gets this digest (units, size, layers, block names, labels)
and asks follow-up questions through the query tools in ``agent.py``.
"""

from __future__ import annotations

import json
from collections import Counter

from ezdxf.document import Drawing

from .geometry import Scene, aci_hex
from .units import Units

MAX_TEXTS = 60
MAX_BLOCKS = 25
MAX_LAYERS = 120


def build(doc: Drawing, scene: Scene, units: Units, name: str) -> dict:
    msp = doc.modelspace()
    types: Counter = Counter()
    per_layer: dict[str, Counter] = {}
    blocks: Counter = Counter()
    for e in msp:
        kind = e.dxftype()
        types[kind] += 1
        per_layer.setdefault(e.dxf.get("layer", "0"), Counter())[kind] += 1
        if kind == "INSERT":
            blocks[e.dxf.get("name", "?")] += 1

    layers = []
    for layer in doc.layers:
        n = layer.dxf.name
        counts = per_layer.get(n, Counter())
        layers.append(
            {
                "name": n,
                "color": aci_hex(layer.dxf.color) or "#201e1d",
                "on": not layer.is_off() and not layer.is_frozen(),
                "frozen": layer.is_frozen(),
                "locked": layer.is_locked(),
                "count": sum(counts.values()),
                "types": dict(counts.most_common(6)),
            }
        )
    layers.sort(key=lambda r: (-r["count"], r["name"].lower()))

    seen: dict[str, dict] = {}
    for it in scene.items:
        if it["k"] != "t":
            continue
        value = " ".join(it["v"].split())[:60]
        rec = seen.setdefault(value, {"text": value, "count": 0, "layer": it["l"]})
        rec["count"] += 1
    texts = sorted(seen.values(), key=lambda r: (-r["count"], r["text"]))[:MAX_TEXTS]

    ext = scene.extents
    size = [round(ext[2] - ext[0], 3), round(ext[3] - ext[1], 3)] if ext else None
    return {
        "name": name,
        "dxfVersion": doc.acad_release,
        "units": units.describe(),
        "extents": ext,
        "size": size,
        "sizeMetres": [round(v * units.to_m, 2) for v in size] if size else None,
        "entityCount": sum(types.values()),
        "types": dict(types.most_common()),
        "layers": layers[:MAX_LAYERS],
        "layerCount": len(layers),
        "blocks": [{"name": b, "count": c} for b, c in blocks.most_common(MAX_BLOCKS)],
        "texts": texts,
        "notShown": dict(scene.not_shown),
        "truncated": scene.truncated,
    }


def for_prompt(digest: dict, max_chars: int = 14000) -> str:
    """The digest as compact JSON, trimmed from the least useful end."""
    d = dict(digest)
    d.pop("notShown", None)
    d["layers"] = [
        {k: v for k, v in row.items() if k in ("name", "count", "on", "locked", "types")} for row in d["layers"]
    ]
    text = json.dumps(d, separators=(",", ":"), ensure_ascii=False)
    while len(text) > max_chars and (d["texts"] or len(d["layers"]) > 20):
        if d["texts"]:
            d["texts"] = d["texts"][: len(d["texts"]) // 2]
        else:
            d["layers"] = d["layers"][: len(d["layers"]) * 2 // 3]
        text = json.dumps(d, separators=(",", ":"), ensure_ascii=False)
    return text
