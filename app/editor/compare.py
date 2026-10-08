"""Compare two revisions of a drawing.

Entities are matched first by handle (an edited copy keeps its handles), then
by an exact geometric signature (a re-saved or re-exported file may renumber
handles but draw the same thing). Whatever is left is added or removed.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Optional

from ezdxf import bbox
from ezdxf.document import Drawing

from . import geometry, ops
from .units import Units


def _sig(e) -> tuple:
    s = ops._signature(e, 1e-4, False)
    if s is not None:
        return s
    try:
        box = bbox.extents([e], fast=True)
        b = (round(box.extmin.x, 3), round(box.extmin.y, 3), round(box.extmax.x, 3), round(box.extmax.y, 3)) if box.has_data else ()
    except Exception:  # noqa: BLE001
        b = ()
    return (e.dxftype(), e.dxf.get("layer", "0"), b)


def _texts(doc: Drawing) -> Counter:
    c: Counter = Counter()
    for e in doc.modelspace():
        t = ops._entity_text(e)
        if t.strip():
            c[" ".join(t.split())[:120]] += 1
    return c


def compare(old: Drawing, new: Drawing, units: Optional[Units] = None) -> dict:
    a = {e.dxf.handle: e for e in old.modelspace()}
    b = {e.dxf.handle: e for e in new.modelspace()}
    sig_a = {h: _sig(e) for h, e in a.items()}
    sig_b = {h: _sig(e) for h, e in b.items()}

    unchanged, changed = [], []
    left_a, left_b = set(a), set(b)
    # 1. Same handle, identical geometry.
    for h in set(a) & set(b):
        if sig_a[h] == sig_b[h]:
            left_a.discard(h)
            left_b.discard(h)
            unchanged.append(h)
    # 2. Identical geometry under another handle (a re-saved file renumbers handles).
    pool: dict[tuple, list[str]] = defaultdict(list)
    for h in sorted(left_a):
        pool[sig_a[h]].append(h)
    for h in sorted(left_b):
        bucket = pool.get(sig_b[h])
        if bucket:
            left_a.discard(bucket.pop())
            left_b.discard(h)
            unchanged.append(h)
    # 3. What is left under the same handle and type was edited.
    for h in sorted(left_a & left_b):
        if a[h].dxftype() == b[h].dxftype():
            left_a.discard(h)
            left_b.discard(h)
            changed.append(h)
    removed, added = sorted(left_a), sorted(left_b)

    per_layer: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for h in added:
        per_layer[b[h].dxf.get("layer", "0")][0] += 1
    for h in removed:
        per_layer[a[h].dxf.get("layer", "0")][1] += 1
    for h in changed:
        per_layer[b[h].dxf.get("layer", "0")][2] += 1
    layers_a = {l.dxf.name for l in old.layers}
    layers_b = {l.dxf.name for l in new.layers}
    blocks_a = {blk.name for blk in old.blocks if not blk.name.startswith("*")}
    blocks_b = {blk.name for blk in new.blocks if not blk.name.startswith("*")}
    ta, tb = _texts(old), _texts(new)
    text_removed = sorted((ta - tb).elements())[:50]
    text_added = sorted((tb - ta).elements())[:50]

    result = {
        "added": added,
        "removed": removed,
        "changed": sorted(changed),
        "unchanged": len(unchanged),
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed), "unchanged": len(unchanged)},
        "layers": sorted(({"layer": l, "added": v[0], "removed": v[1], "changed": v[2]} for l, v in per_layer.items()), key=lambda r: -(r["added"] + r["removed"] + r["changed"])),
        "layersAdded": sorted(layers_b - layers_a),
        "layersRemoved": sorted(layers_a - layers_b),
        "blocksAdded": sorted(blocks_b - blocks_a),
        "blocksRemoved": sorted(blocks_a - blocks_b),
        "textRemoved": text_removed,
        "textAdded": text_added,
        "unitsChanged": old.header.get("$INSUNITS", 0) != new.header.get("$INSUNITS", 0),
    }
    result["summary"] = summarise(result)
    # Geometry for the viewer: what disappeared (from the old file) and what is new or changed (in the current one).
    result["overlay"] = {
        "removed": geometry.extract(old, handles=removed[:20000]).items,
        "added": added[:20000],
        "changed": result["changed"][:20000],
    }
    result["regions"] = regions(old, new, removed, added + result["changed"])
    return result


def summarise(r: dict) -> str:
    c = r["counts"]
    if not (c["added"] or c["removed"] or c["changed"] or r["layersAdded"] or r["layersRemoved"] or r["textAdded"] or r["textRemoved"]):
        return "The two revisions draw exactly the same thing."
    parts = []
    for key, word in (("added", "added"), ("removed", "removed"), ("changed", "changed")):
        if c[key]:
            top = [row["layer"] for row in r["layers"] if row[key]][:3]
            parts.append(f"{c[key]:,} object{'s' if c[key] != 1 else ''} {word}" + (f" (mostly on {', '.join(top)})" if top else ""))
    s = "; ".join(parts) + "." if parts else ""
    if r["layersAdded"]:
        s += " New layers: " + ", ".join(r["layersAdded"][:8]) + "."
    if r["layersRemoved"]:
        s += " Layers gone: " + ", ".join(r["layersRemoved"][:8]) + "."
    if r["textRemoved"] or r["textAdded"]:
        pairs = []
        for old_t, new_t in zip(r["textRemoved"], r["textAdded"]):
            pairs.append(f"“{old_t[:40]}” → “{new_t[:40]}”")
        if pairs:
            s += " Text changed: " + ", ".join(pairs[:5]) + "."
        elif r["textAdded"]:
            s += " New text: " + ", ".join(f"“{t[:40]}”" for t in r["textAdded"][:5]) + "."
        else:
            s += " Text removed: " + ", ".join(f"“{t[:40]}”" for t in r["textRemoved"][:5]) + "."
    if r["unitsChanged"]:
        s += " The drawing units changed."
    return s.strip()


def _boxes(doc: Drawing, handles: list[str]) -> list[list[float]]:
    out = []
    for h in handles[:5000]:
        e = doc.entitydb.get(h)
        if e is None:
            continue
        try:
            box = bbox.extents([e], fast=True)
        except Exception:  # noqa: BLE001
            continue
        if box.has_data:
            out.append([box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y])
    return out


def regions(old: Drawing, new: Drawing, removed: list[str], current: list[str], max_regions: int = 8) -> list[list[float]]:
    """Changed places grouped into a few rectangles (for revision clouds)."""
    boxes = _boxes(old, removed) + _boxes(new, current)
    if not boxes:
        return []
    span = max(max(b[2] for b in boxes) - min(b[0] for b in boxes), max(b[3] for b in boxes) - min(b[1] for b in boxes), 1e-9)
    gap = span * 0.05
    clusters: list[list[float]] = []
    for b in sorted(boxes, key=lambda b: (b[0], b[1])):
        for c in clusters:
            if b[0] <= c[2] + gap and b[2] >= c[0] - gap and b[1] <= c[3] + gap and b[3] >= c[1] - gap:
                c[0], c[1], c[2], c[3] = min(c[0], b[0]), min(c[1], b[1]), max(c[2], b[2]), max(c[3], b[3])
                break
        else:
            clusters.append(list(b))
    # Merging can make clusters overlap; repeat until stable, then keep the biggest few.
    merged = True
    while merged and len(clusters) > 1:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, c = clusters[i], clusters[j]
                if a[0] <= c[2] + gap and a[2] >= c[0] - gap and a[1] <= c[3] + gap and a[3] >= c[1] - gap:
                    clusters[i] = [min(a[0], c[0]), min(a[1], c[1]), max(a[2], c[2]), max(a[3], c[3])]
                    clusters.pop(j)
                    merged = True
                    break
            if merged:
                break
    while len(clusters) > max_regions:
        clusters.sort(key=lambda c: (c[2] - c[0]) * (c[3] - c[1]))
        small = clusters.pop(0)
        nearest = min(clusters, key=lambda c: math.hypot((c[0] + c[2]) / 2 - (small[0] + small[2]) / 2, (c[1] + c[3]) / 2 - (small[1] + small[3]) / 2))
        nearest[0], nearest[1] = min(nearest[0], small[0]), min(nearest[1], small[1])
        nearest[2], nearest[3] = max(nearest[2], small[2]), max(nearest[3], small[3])
    return [[round(v, 3) for v in c] for c in clusters]


def cloud_steps(result: dict, rev: str, description: str, date: str, at: Optional[tuple[float, float]], text_height: float) -> list:
    """Steps: a revision cloud around each changed region, then a revision-table row."""
    steps = []
    clouds = [{"op": "revision_cloud", "bbox": r, "rev": rev if i == 0 else None, "layer": "REV-CLOUD"} for i, r in enumerate(result.get("regions", [])[:8])]
    for c in clouds:
        if c["rev"] is None:
            c.pop("rev")
    if clouds:
        steps.append({"title": f"Revision cloud{'s' if len(clouds) != 1 else ''} around {len(clouds)} changed area{'s' if len(clouds) != 1 else ''}", "ops": clouds})
    if at is not None:
        steps.append({"title": "Revision table entry", "ops": [{
            "op": "add_table", "x": at[0], "y": at[1], "rows": [["REV", "DATE", "DESCRIPTION"], [rev, date, description[:100]]],
            "text_height": text_height, "layer": "REV-CLOUD", "title": "REVISIONS"}]})
    return steps


def report_text(result: dict, old_name: str, new_name: str) -> str:
    c = result["counts"]
    lines = [
        "Drawing comparison",
        "==================",
        f"Older: {old_name}",
        f"Newer: {new_name}",
        "",
        result["summary"],
        "",
        f"Added: {c['added']}   Removed: {c['removed']}   Changed: {c['changed']}   Unchanged: {c['unchanged']}",
        "",
        "By layer (added / removed / changed):",
    ]
    for row in result["layers"]:
        lines.append(f"  {row['layer']}: {row['added']} / {row['removed']} / {row['changed']}")
    for label, key in (("Layers added", "layersAdded"), ("Layers removed", "layersRemoved"), ("Blocks added", "blocksAdded"),
                       ("Blocks removed", "blocksRemoved"), ("Text added", "textAdded"), ("Text removed", "textRemoved")):
        if result[key]:
            lines.append("")
            lines.append(f"{label}:")
            lines += [f"  {v}" for v in result[key]]
    return "\n".join(lines) + "\n"
