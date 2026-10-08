"""A demo drawing: a small warehouse with racking, docks and a few rough edges.

It exists so the editor can be tried without a file, and it includes the kinds
of mess people ask to clean up: unused layers, a stray note, an old revision tag.
"""

from __future__ import annotations

import ezdxf
from ezdxf.document import Drawing


def build() -> Drawing:
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4  # millimetres
    for name, aci in (("A-WALL", 7), ("A-COLS", 8), ("S-RACK", 5), ("A-DOCK", 30), ("A-ZONE", 3), ("A-ANNO", 7), ("A-DIMS", 1), ("TEMP-NOTES", 2), ("OLD-LAYOUT", 6), ("XREF-GHOST", 4)):
        doc.layers.add(name, color=aci)
    msp = doc.modelspace()

    W, H, T = 60_000, 36_000, 300
    msp.add_lwpolyline([(0, 0), (W, 0), (W, H), (0, H)], close=True, dxfattribs={"layer": "A-WALL"})
    msp.add_lwpolyline([(T, T), (W - T, T), (W - T, H - T), (T, H - T)], close=True, dxfattribs={"layer": "A-WALL"})

    for i in range(6):  # loading docks on the south wall
        x = 6_000 + i * 8_000
        msp.add_lwpolyline([(x, -500), (x + 3_000, -500), (x + 3_000, 0), (x, 0)], close=True, dxfattribs={"layer": "A-DOCK"})
        msp.add_text(f"DOCK {i + 1}", height=450, dxfattribs={"layer": "A-ANNO"}).set_placement((x + 300, -1_300))

    msp.add_lwpolyline([(T, 24_000), (12_000, 24_000), (12_000, H - T)], dxfattribs={"layer": "A-ZONE"})
    msp.add_mtext("OFFICE\\P& AMENITIES", dxfattribs={"layer": "A-ANNO", "char_height": 600, "insert": (1_500, 32_500)})
    msp.add_lwpolyline([(14_000, 2_000), (58_000, 2_000), (58_000, 8_000), (14_000, 8_000)], close=True, dxfattribs={"layer": "A-ZONE"})
    msp.add_text("DOCK STAGING", height=600, dxfattribs={"layer": "A-ANNO"}).set_placement((15_000, 6_800))

    for x in range(12_000, 60_000, 12_000):  # structural columns
        for y in (12_000, 24_000):
            msp.add_lwpolyline([(x - 200, y - 200), (x + 200, y - 200), (x + 200, y + 200), (x - 200, y + 200)], close=True, dxfattribs={"layer": "A-COLS"})

    bay = doc.blocks.new("RACK_BAY")
    bay.add_lwpolyline([(0, 0), (2_700, 0), (2_700, 1_100), (0, 1_100)], close=True)
    bay.add_line((0, 0), (2_700, 1_100))
    bay.add_line((0, 1_100), (2_700, 0))
    for row in range(4):  # four double rows of racking
        y0 = 11_000 + row * 5_600
        for line in (0, 1):
            for n in range(15):
                msp.add_blockref("RACK_BAY", (15_000 + n * 2_700, y0 + line * 1_300), dxfattribs={"layer": "S-RACK"})
        msp.add_text(f"ROW {'ABCD'[row]}", height=500, dxfattribs={"layer": "A-ANNO"}).set_placement((13_000, y0 + 900))

    msp.add_aligned_dim(p1=(0, 0), p2=(W, 0), distance=-3_200, dimstyle="EZDXF", override={"dimtxt": 500, "dimasz": 400, "dimexe": 200, "dimexo": 200, "dimlfac": 1, "dimdec": 0}, dxfattribs={"layer": "A-DIMS"}).render()
    msp.add_aligned_dim(p1=(W, 0), p2=(W, H), distance=-2_500, dimstyle="EZDXF", override={"dimtxt": 500, "dimasz": 400, "dimexe": 200, "dimexo": 200, "dimlfac": 1, "dimdec": 0}, dxfattribs={"layer": "A-DIMS"}).render()

    msp.add_text("WAREHOUSE B  -  RACK LAYOUT", height=900, dxfattribs={"layer": "A-ANNO"}).set_placement((0, H + 1_800))
    msp.add_text("REV A", height=600, dxfattribs={"layer": "TEMP-NOTES"}).set_placement((W - 3_000, H + 1_800))
    msp.add_text("TODO: check flue spacing with fire consultant", height=400, dxfattribs={"layer": "TEMP-NOTES"}).set_placement((0, H + 800))
    return doc
