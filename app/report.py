"""Drawing inventories and the conversion report.

The report is built by comparing what was in the source drawing with what is
in the converted file, so it lists what really happened rather than what we
expected to happen.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from ezdxf.document import Drawing

from .versions import BY_CODE, FormatVersion, order

# Entity types that only exist from a given file format onwards.
INTRODUCED: dict[str, str] = {
    "ARC_DIMENSION": "AC1018",
    "LARGE_RADIAL_DIMENSION": "AC1018",
    "ACAD_TABLE": "AC1018",
    "MULTILEADER": "AC1021",
    "MLEADER": "AC1021",
    "HELIX": "AC1021",
    "SECTIONOBJECT": "AC1021",
    "SECTION": "AC1021",
    "LIGHT": "AC1021",
    "SUN": "AC1021",
    "SURFACE": "AC1021",
    "EXTRUDEDSURFACE": "AC1021",
    "LOFTEDSURFACE": "AC1021",
    "REVOLVEDSURFACE": "AC1021",
    "SWEPTSURFACE": "AC1021",
    "PLANESURFACE": "AC1021",
    "DWFUNDERLAY": "AC1021",
    "DGNUNDERLAY": "AC1021",
    "MESH": "AC1024",
    "PDFUNDERLAY": "AC1024",
    "NURBSSURFACE": "AC1024",
    "POINTCLOUD": "AC1024",
    "GEOPOSITIONMARKER": "AC1027",
    "POINTCLOUDEX": "AC1027",
}

# Entities whose geometry is an embedded ACIS model.
ACIS_TYPES = {"3DSOLID", "REGION", "BODY", "SURFACE"}

NAMES: dict[str, str] = {
    "ACAD_PROXY_ENTITY": "Proxy object",
    "ACAD_TABLE": "Table",
    "ARC_DIMENSION": "Arc length dimension",
    "LARGE_RADIAL_DIMENSION": "Jogged radius dimension",
    "MULTILEADER": "Multileader",
    "MLEADER": "Multileader",
    "HELIX": "Helix",
    "SECTIONOBJECT": "Section plane",
    "LIGHT": "Light",
    "SUN": "Sun",
    "EXTRUDEDSURFACE": "Extruded surface",
    "LOFTEDSURFACE": "Lofted surface",
    "REVOLVEDSURFACE": "Revolved surface",
    "SWEPTSURFACE": "Swept surface",
    "PLANESURFACE": "Planar surface",
    "NURBSSURFACE": "NURBS surface",
    "DWFUNDERLAY": "DWF underlay",
    "DGNUNDERLAY": "DGN underlay",
    "PDFUNDERLAY": "PDF underlay",
    "MESH": "Mesh",
    "POINTCLOUD": "Point cloud",
    "POINTCLOUDEX": "Point cloud",
    "GEOPOSITIONMARKER": "Geographic marker",
    "LWPOLYLINE": "Polyline",
    "POLYLINE": "Polyline (2D/3D/mesh)",
    "3DSOLID": "3D solid",
    "3DFACE": "3D face",
    "MTEXT": "Multiline text",
    "TEXT": "Text",
    "INSERT": "Block reference",
    "DIMENSION": "Dimension",
    "LEADER": "Leader",
    "HATCH": "Hatch",
    "SPLINE": "Spline",
    "IMAGE": "Raster image",
    "WIPEOUT": "Wipeout",
    "REGION": "Region",
    "BODY": "Body",
    "VIEWPORT": "Viewport",
    "LINE": "Line",
    "ARC": "Arc",
    "CIRCLE": "Circle",
    "ELLIPSE": "Ellipse",
    "POINT": "Point",
    "SOLID": "2D solid",
    "TOLERANCE": "Tolerance",
    "MLINE": "Multiline",
    "RAY": "Ray",
    "XLINE": "Construction line",
    "OLE2FRAME": "OLE object",
    "ATTRIB": "Attribute",
}


def entity_name(dxftype: str) -> str:
    return NAMES.get(dxftype, dxftype.replace("_", " ").title())


def newer_than(dxftype: str, target: FormatVersion) -> bool:
    intro = INTRODUCED.get(dxftype)
    return intro is not None and order(intro) > order(target.code)


def reason_for(dxftype: str, target: FormatVersion) -> str:
    intro = INTRODUCED.get(dxftype)
    if intro and order(intro) > order(target.code):
        return f"Doesn't exist in AutoCAD {target.year} files (added in {BY_CODE[intro].year})"
    if dxftype == "ACAD_PROXY_ENTITY":
        return "Unsupported entity from a third-party add-on"
    if dxftype in ACIS_TYPES or dxftype.endswith("SURFACE"):
        return "Its 3D modeling (ACIS) data couldn't be rewritten for the older format"
    return "Couldn't be carried over to the older format"


# ── inventories ────────────────────────────────────────────────────────────

Key = tuple[str, str]  # (container, layer)


def containers(doc: Drawing, include_anonymous: bool = False):
    """Yield (label, entity space) for every layout and block definition."""
    for layout in doc.layouts:
        label = "" if layout.is_modelspace else f"Layout {layout.name}"
        yield label, layout
    for block in doc.blocks:
        if block.is_any_layout:
            continue  # covered by the layouts above
        if block.name.startswith("*") and not include_anonymous:
            # Anonymous blocks (dimension geometry, dynamic block states) are
            # regenerated under new names on save; comparing them is noise.
            continue
        yield f"block {block.name}", block


def inventory(doc: Drawing) -> dict[Key, Counter]:
    inv: dict[Key, Counter] = defaultdict(Counter)
    for label, container in containers(doc):
        for e in container:
            layer = e.dxf.get("layer", "0")
            inv[(label, layer)][e.dxftype()] += 1
    return inv


def count_layers_and_blocks(doc: Drawing) -> int:
    blocks = sum(1 for b in doc.blocks if not b.is_any_layout and not b.name.startswith("*"))
    return len(doc.layers) + blocks


def where(container: str, layer: str) -> str:
    text = f"Layer {layer}"
    return f"{text}, {container}" if container else text


# ── report ─────────────────────────────────────────────────────────────────


@dataclass
class Item:
    kind: str  # "skipped" | "changed" | "repaired"
    entity: str  # display name, e.g. "Proxy object"
    where: str  # e.g. "Layer A-WALL"
    reason: str
    count: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


class Report:
    def __init__(self) -> None:
        self.items: list[Item] = []

    def add(self, kind: str, entity: str, where_: str, reason: str, count: int = 1) -> None:
        for it in self.items:
            if (it.kind, it.entity, it.where, it.reason) == (kind, entity, where_, reason):
                it.count += count
                return
        self.items.append(Item(kind, entity, where_, reason, count))

    @property
    def skipped(self) -> int:
        return sum(i.count for i in self.items if i.kind == "skipped")

    def counts(self) -> dict:
        c = Counter()
        for i in self.items:
            c[i.kind] += i.count
        return {"skipped": c["skipped"], "changed": c["changed"], "repaired": c["repaired"]}

    def sorted_items(self) -> list[Item]:
        rank = {"skipped": 0, "changed": 1, "repaired": 2}
        return sorted(self.items, key=lambda i: (rank[i.kind], -i.count, i.entity, i.where))

    def add_diff(self, before: dict[Key, Counter], after: dict[Key, Counter], target: FormatVersion) -> None:
        for key in sorted(before):
            missing = before[key] - after.get(key, Counter())
            if not missing:
                continue
            added = after.get(key, Counter()) - before[key]
            for dxftype, n in sorted(missing.items()):
                if added:
                    into = ", ".join(entity_name(t) for t in sorted(added))
                    self.add("changed", entity_name(dxftype), where(*key), f"Converted to {into}", n)
                else:
                    self.add("skipped", entity_name(dxftype), where(*key), reason_for(dxftype, target), n)

    def add_feature_notes(self, doc: Drawing, target: FormatVersion) -> None:
        """Properties that exist on entities but not in the older format."""
        if order(target.code) < order("AC1024"):
            n = sum(1 for e in doc.entitydb.values() if e.dxf.hasattr("transparency"))
            if n:
                self.add(
                    "changed",
                    "Transparency",
                    f"{n:,} object{'s' if n != 1 else ''}",
                    f"AutoCAD {target.year} has no transparency; shown fully opaque",
                    n,
                )
        if order(target.code) < order("AC1018"):
            n = sum(1 for e in doc.entitydb.values() if e.dxf.hasattr("true_color"))
            if n:
                self.add(
                    "changed",
                    "True color",
                    f"{n:,} object{'s' if n != 1 else ''}",
                    "Mapped to the nearest standard AutoCAD color",
                    n,
                )

    def add_log(self, kind: str, entity: str, lines: list[str], limit: int = 20) -> None:
        for line in lines[:limit]:
            self.add(kind, entity, "Drawing", line[:200])
        if len(lines) > limit:
            self.add(kind, entity, "Drawing", f"…and {len(lines) - limit} more messages")

    def text(self, *, source_name: str, output_name: str, source_label: str, target: FormatVersion, engine: str) -> str:
        c = self.counts()
        lines = [
            "Backdate.dwg conversion report",
            "=" * 30,
            f"Source:  {source_name} ({source_label})",
            f"Output:  {output_name} (AutoCAD {target.year} format, {target.code})",
            f"Engine:  {engine}",
            "",
            f"Skipped: {c['skipped']}   Changed: {c['changed']}   Repaired: {c['repaired']}",
            "",
        ]
        if not self.items:
            lines.append("No issues. Everything converted as-is.")
        for kind, title in (("skipped", "SKIPPED"), ("changed", "CHANGED"), ("repaired", "REPAIRED IN SOURCE")):
            rows = [i for i in self.sorted_items() if i.kind == kind]
            if not rows:
                continue
            lines.append(title)
            for i in rows:
                count = f" (x{i.count:,})" if i.count > 1 else ""
                lines.append(f"  - {i.entity}{count} · {i.where}")
                lines.append(f"    {i.reason}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
