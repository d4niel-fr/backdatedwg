"""Layer standards: map messy layer names onto a consistent scheme.

The built-in scheme follows the US National CAD Standard / AIA layer format
(discipline - major group - minor group, e.g. ``A-WALL``, ``A-ANNO-DIMS``).
Anyone can supply their own mapping instead, as CSV (``old,new``) or JSON.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Optional

from ezdxf.document import Drawing

# (target layer, AutoCAD colour, keywords that identify it) — first match wins.
NCS_RULES: list[tuple[str, int, str]] = [
    ("A-ANNO-DIMS", 1, r"dim|dimension|cota|mass"),
    ("A-ANNO-TTLB", 7, r"title|ttlb|border|frame|sheet|tblk|cartouche"),
    ("A-ANNO-REVS", 1, r"rev(ision)?[-_ ]?cloud|revs?\b|delta"),
    ("A-ANNO-TEXT", 7, r"text|txt|note|anno|label|tag|legend"),
    ("A-ANNO-SYMB", 7, r"symbol|symb|north|arrow"),
    ("A-AREA", 3, r"room|area|zone|space|staging"),
    ("A-DOOR", 2, r"door|dr\b|gate|dock"),
    ("A-GLAZ", 4, r"window|glaz|glass|wdw"),
    ("A-WALL", 7, r"wall|partition|wand|mur"),
    ("A-FLOR-STRS", 3, r"stair|step|ramp"),
    ("A-FLOR", 3, r"floor|flor|slab|pavement"),
    ("A-ROOF", 3, r"roof"),
    ("A-EQPM-RACK", 5, r"rack|shelv|pallet|stillage|bay\b"),
    ("A-EQPM", 5, r"equip|eqpm|machine|forklift|conveyor"),
    ("I-FURN", 6, r"furn|desk|chair|table|sofa|bed\b"),
    ("S-COLS", 8, r"column|col\b|cols|pillar|post"),
    ("S-GRID", 8, r"grid|axis|axes"),
    ("S-BEAM", 8, r"beam|joist|truss|lintel"),
    ("S-FNDN", 8, r"found|footing|fndn|pile"),
    ("E-LITE", 2, r"light|lite|lum|lamp"),
    ("E-POWR", 1, r"power|powr|socket|outlet|receptacle"),
    ("E-PANL", 1, r"panel|switchboard|db\b|distribution"),
    ("E-COMM", 4, r"data|comm|telecom|network|cctv"),
    ("M-HVAC", 4, r"hvac|duct|ventil|air\b|ahu|fcu"),
    ("P-PIPE", 4, r"plumb|pipe|sanit|drain|water|sewer"),
    ("F-PROT", 1, r"fire|sprink|sprn|hydrant|alarm"),
    ("C-TOPO", 8, r"topo|contour|survey|level|spot"),
    ("C-ROAD", 8, r"road|kerb|curb|parking|road"),
    ("L-PLNT", 3, r"tree|plant|landscape|shrub|grass"),
    ("G-XREF", 8, r"xref|ref\b|underlay|base"),
    ("G-HIDE", 9, r"hidden|construct|temp|scratch|old|ghost|defpoint"),
]

COMPLIANT = re.compile(r"^[A-Z]-[A-Z0-9]{4}(-[A-Z0-9]{4})*$")


def propose(doc: Drawing, rules: Optional[list[tuple[str, int, str]]] = None) -> dict:
    """Suggested renames for every layer that doesn't follow the standard."""
    rules = rules or NCS_RULES
    compiled = [(t, c, re.compile(p, re.I)) for t, c, p in rules]
    counts: dict[str, int] = {}
    for e in doc.modelspace():
        n = e.dxf.get("layer", "0")
        counts[n] = counts.get(n, 0) + 1
    rows = []
    for layer in doc.layers:
        name = layer.dxf.name
        if name.lower() in ("0", "defpoints"):
            continue
        if COMPLIANT.match(name) and any(name == t or name.startswith(t + "-") for t, _c, _p in rules):
            rows.append({"from": name, "to": name, "status": "ok", "reason": "already standard", "count": counts.get(name, 0)})
            continue
        words = re.sub(r"[^A-Za-z0-9]+", " ", name)
        hit = next(((t, c) for t, c, rx in compiled if rx.search(name) or rx.search(words)), None)
        if hit is None:
            rows.append({"from": name, "to": None, "status": "unmatched", "reason": "no rule matched", "count": counts.get(name, 0)})
        else:
            rows.append({"from": name, "to": hit[0], "status": "rename", "reason": f"looks like {hit[0]}", "color": hit[1], "count": counts.get(name, 0)})
    mapping = {r["from"]: r["to"] for r in rows if r["status"] == "rename"}
    colors = {r["to"]: r["color"] for r in rows if r["status"] == "rename"}
    ops = [{"op": "map_layers", "mapping": mapping, "colors": colors}] if mapping else []
    return {"rows": rows, "ops": ops, "renames": len(mapping), "unmatched": sum(1 for r in rows if r["status"] == "unmatched")}


def parse_mapping(text: str) -> dict[str, str]:
    """A custom mapping from JSON ({"old": "new"}) or CSV (old,new per line)."""
    text = text.strip()
    if not text:
        raise ValueError("The mapping is empty.")
    if text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("A JSON mapping must be an object of old: new.")
        return {str(k): str(v) for k, v in data.items() if str(v).strip()}
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            continue
        if row[0].strip().lower() in ("old", "from", "layer") and row[1].strip().lower() in ("new", "to", "standard"):
            continue
        out[row[0].strip()] = row[1].strip()
    if not out:
        raise ValueError("No old,new pairs found in the mapping.")
    return out
