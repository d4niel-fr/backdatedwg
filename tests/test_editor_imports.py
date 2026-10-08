"""Imports: PDF plans, sketch photos (vision model), rack-designer projects, CSV/XLSX tables."""

import io
import json
import struct
import zipfile
import zlib

import pytest

from app.editor import exports, importers


# ── tables ──────────────────────────────────────────────────────────────────


def test_csv_with_bom_and_semicolons():
    t = importers.parse_table("﻿Tag;Value\nPROJECT;Warehouse B\nDATE;2026-10-08\n\n".encode("utf-8"), "tb.csv")
    assert t["columns"] == ["Tag", "Value"] and t["rows"] == [["PROJECT", "Warehouse B"], ["DATE", "2026-10-08"]]


def make_xlsx(rows, inline=False):
    shared = []
    sheet_rows = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, value in enumerate(row):
            ref = chr(65 + c) + str(r)
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            elif inline:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
            else:
                shared.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns}><sheets><sheet name="Doors" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
    return buf.getvalue()


@pytest.mark.parametrize("inline", [False, True])
def test_xlsx(inline):
    t = importers.parse_table(make_xlsx([["Door", "Width", "Qty"], ["D1", 900, 2], ["D2", 1200.5, 1]], inline), "doors.xlsx")
    assert t["columns"] == ["Door", "Width", "Qty"] and t["rows"] == [["D1", "900", "2"], ["D2", "1200.5", "1"]]


def test_xlsx_with_entities_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><worksheet/>')
    with pytest.raises(importers.ImportError_, match="unsupported XML"):
        importers.parse_table(buf.getvalue(), "evil.xlsx")


def test_bad_tables():
    with pytest.raises(importers.ImportError_, match="empty"):
        importers.parse_table(b"\n\n", "x.csv")
    with pytest.raises(importers.ImportError_):
        importers.parse_table(b"PK not a zip", "x.xlsx")


# ── rack designer projects ──────────────────────────────────────────────────


def wdp(kind="builder"):
    state = {
        "units": "m", "name": "DC North",
        "walls": [{"kind": "poly", "closed": True, "pts": [{"x": 0, "y": 0}, {"x": 40, "y": 0}, {"x": 40, "y": 25}, {"x": 0, "y": 25}]},
                  {"kind": "arc", "pts": [{"x": 40, "y": 0}, {"x": 45, "y": 12.5}, {"x": 40, "y": 25}]}],
        "columns": [{"x": 10, "y": 10, "w": 0.4, "h": 0.4}],
        "zones": [{"cx": 5, "cy": 20, "w": 8, "h": 6, "rot": 0, "name": "Office"}],
        "doors": [{"x": 20, "y": 0, "rot": 0, "w": 3}],
        "rackTypes": {"A": {"letter": "A", "bay": 2.7, "depth": 1.1, "height": 6, "shelves": 4}},
        "racks": [{"type": "A", "cx": 15 + i * 2.7, "cy": 8, "w": 2.7, "h": 1.1, "rot": 0, "kind": "rack"} for i in range(5)]
                 + [{"type": "A", "cx": 15, "cy": 12, "w": 2.7, "h": 1.1, "rot": 1.5707963, "kind": "rack"},
                    {"type": None, "cx": 30, "cy": 20, "w": 2.2, "h": 1.4, "rot": 0, "kind": "forklift"}],
        "dims": [{"ax": 0, "ay": 0, "bx": 40, "by": 0, "off": -2}],
    }
    return json.dumps({"format": "wdp", "version": 1, "kind": kind, "name": "DC North", "app": "Warehouse Designer Pro", "state": state}).encode()


def test_wdp_builder_project():
    doc, name, notes = importers.wdp_to_dxf(wdp())
    msp = doc.modelspace()
    assert name == "DC North" and doc.header["$INSUNITS"] == 6
    racks = [e for e in msp.query("INSERT") if e.dxf.layer == "A-EQPM-RACK"]
    assert len(racks) == 6 and racks[0].dxf.name == "RACK_A_2700x1100" and racks[0].get_attrib_text("TYPE") == "A"
    assert any(abs(r.dxf.rotation - 90) < 0.01 for r in racks)
    assert msp.query("ARC") and len(msp.query("ARC")) == 2  # wall arc + door swing
    assert any(t.dxf.text == "Office" for t in msp.query("TEXT")) and any(t.dxf.text == "FORKLIFT" for t in msp.query("TEXT"))
    assert msp.query("DIMENSION")
    assert "6 racks" in notes[0] and "Rack types: A 2.7×1.1 m" in notes[1]


def test_wdp_errors():
    with pytest.raises(importers.ImportError_, match="Quick Layout"):
        importers.wdp_to_dxf(wdp("quick"))
    with pytest.raises(importers.ImportError_, match="isn't a Warehouse"):
        importers.wdp_to_dxf(b'{"hello": 1}')
    with pytest.raises(importers.ImportError_, match="isn't a Warehouse"):
        importers.wdp_to_dxf(b"\xff\xfe garbage")


def test_wdp_import_feeds_the_warehouse_check():
    from app.editor import analysis
    from app.editor.units import Units

    doc, _n, _notes = importers.wdp_to_dxf(wdp())
    w = analysis.warehouse(doc, Units(6, "metres", "m", 1.0))
    assert w["found"] and w["bays"] == 6


# ── PDF ─────────────────────────────────────────────────────────────────────


def raw_pdf(objects: list[bytes]) -> bytes:
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offs = []
    for i, body in enumerate(objects, 1):
        offs.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    x = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs))
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, x))
    return out.getvalue()


def stream(content: bytes, extra: bytes = b"") -> bytes:
    z = zlib.compress(content)
    return b"<< /Length %d /Filter /FlateDecode " % len(z) + extra + b">>\nstream\n" + z + b"\nendstream"


def cad_style_pdf() -> bytes:
    """A page that draws through a Form XObject under a transform, like CAD exports do."""
    page_content = b"q 1 0 0 1 100 100 cm /X1 Do Q 0 0 1 RG 10 10 m 50 10 l S 1 0 0 rg 200 200 20 10 re f"
    form = b"0.5 G 0 0 m 72 0 l 72 72 l h S 0 0 m 0 0 72 72 72 72 c S"
    return raw_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /XObject << /X1 5 0 R >> >> >>",
        stream(page_content),
        stream(form, b"/Type /XObject /Subtype /Form /BBox [0 0 100 100] /Matrix [2 0 0 2 0 0] "),
    ])


def test_pdf_lines_through_form_xobjects():
    doc, notes = importers.pdf_to_dxf(cad_style_pdf(), 1, 1.0)
    msp = doc.modelspace()
    pls = list(msp.query("LWPOLYLINE"))
    assert len(pls) == 4 and doc.header["$INSUNITS"] == 4
    k = 25.4 / 72
    tri = next(p for p in pls if p.closed and len(p) == 4 and p.dxf.layer.startswith("PDF-LINE"))
    xs = sorted(round(x / k, 3) for x, y in tri.get_points("xy"))
    # form points (0..72) scaled by its /Matrix (x2) then moved by the page cm (+100)
    assert xs[0] == 100 and xs[-1] == 244
    blue = [p for p in pls if p.dxf.layer == "PDF-LINE-0000FF"]
    assert blue and doc.layers.get("PDF-LINE-0000FF").rgb == (0, 0, 255)
    assert any(p.dxf.layer == "PDF-FILL-FF0000" for p in pls)
    curve = next(p for p in pls if len(p) > 5)
    assert curve  # the bezier was flattened
    assert "Imported 4 lines" in notes[0]


def test_pdf_round_trip_of_our_own_export_at_scale():
    from app.editor import geometry, sample

    data, _ = exports.drawing_pdf(geometry.extract(sample.build()), units_to_m=0.001, units_name="mm", units_guessed=False, name="W")
    doc, notes = importers.pdf_to_dxf(data, 1, 200)  # exported at 1:200, so scale 200 brings it back to real size
    msp = doc.modelspace()
    texts = [t.dxf.text for t in msp.query("TEXT")]
    assert "DOCK 1" in texts and "ROW A" in texts
    # the outer wall is 60 m wide in reality
    widths = []
    for p in msp.query("LWPOLYLINE"):
        xs = [x for x, y in p.get_points("xy")]
        widths.append(max(xs) - min(xs))
    assert any(abs(w - 60000) < 300 for w in widths), sorted(widths)[-5:]


def test_pdf_errors():
    with pytest.raises(importers.ImportError_, match="couldn't be read"):
        importers.pdf_to_dxf(b"%PDF-1.4 nonsense", 1, 1)
    with pytest.raises(importers.ImportError_, match="has 1 page"):
        importers.pdf_to_dxf(cad_style_pdf(), 3, 1)
    blank = raw_pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                     b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R >>", stream(b"")])
    with pytest.raises(importers.ImportError_, match="scanned image"):
        importers.pdf_to_dxf(blank, 1, 1)


# ── sketches ────────────────────────────────────────────────────────────────


def png(w, h):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00" + b"\x00" * 16


def jpeg(w, h):
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", h, w) + b"\x03" + b"\x00" * 12


def test_image_sizes():
    assert importers.image_size(png(800, 600)) == (800, 600, "image/png")
    assert importers.image_size(jpeg(1024, 768)) == (1024, 768, "image/jpeg")
    webp = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 8 + (639).to_bytes(3, "little") + (479).to_bytes(3, "little")
    assert importers.image_size(webp) == (640, 480, "image/webp")
    with pytest.raises(importers.ImportError_, match="PNG, JPEG or WebP"):
        importers.image_size(b"GIF89a....")


class Vision:
    name = "vision-fake"

    def __init__(self, reply):
        self.reply = reply
        self.seen = None

    def complete(self, messages, max_tokens=None, on_delta=None):
        self.seen = messages
        return json.dumps(self.reply) if not isinstance(self.reply, str) else self.reply


SKETCH = {"polylines": [{"kind": "wall", "closed": True, "points": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]]},
                        {"kind": "door", "closed": False, "points": [[400, 1000], [600, 1000]]}],
          "texts": [{"x": 500, "y": 500, "text": "STORE"}], "notes": "door width guessed"}


def test_sketch_trace_scaled_and_flipped():
    v = Vision(SKETCH)
    doc, notes = importers.sketch_to_dxf(png(800, 400), 20, v)
    content = v.seen[0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    msp = doc.modelspace()
    wall = next(p for p in msp.query("LWPOLYLINE") if p.dxf.layer == "A-WALL")
    pts = list(wall.get_points("xy"))
    assert pts[0] == (0, 10000) and pts[2] == (20000, 0)  # 20 m wide, 10 m tall (800x400 px), y flipped
    door = next(p for p in msp.query("LWPOLYLINE") if p.dxf.layer == "A-DOOR")
    assert list(door.get_points("xy"))[0] == (8000, 0)
    assert msp.query("TEXT")[0].dxf.text == "STORE" and "door width guessed" in notes[1]


def test_sketch_errors():
    with pytest.raises(importers.ImportError_, match="needs an AI model"):
        importers.sketch_to_dxf(png(10, 10), 10, None)
    with pytest.raises(importers.ImportError_, match="real width"):
        importers.sketch_to_dxf(png(10, 10), 0, Vision(SKETCH))
    with pytest.raises(importers.ImportError_, match="didn't return a drawing"):
        importers.sketch_to_dxf(png(10, 10), 10, Vision("I see a lovely sketch"))
    with pytest.raises(importers.ImportError_, match="couldn't find any lines"):
        importers.sketch_to_dxf(png(10, 10), 10, Vision({"polylines": []}))


# ── HTTP ────────────────────────────────────────────────────────────────────


def test_open_pdf_wdp_and_sketch(client, monkeypatch):
    r = client.post("/api/editor/sessions", files={"file": ("plan.pdf", cad_style_pdf())}, data={"scale": "50"})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["name"] == "plan.dxf" and s["sourceLabel"] == "PDF page 1" and "at 1:50" in s["notes"][0]

    r = client.post("/api/editor/sessions", files={"file": ("dc.wdp", wdp())})
    assert r.status_code == 201 and r.json()["name"] == "DC North.dxf" and r.json()["digest"]["units"]["short"] == "m"

    for k in ("OPENROUTER_API_KEY", "AI_API_KEY", "NVIDIA_API_KEY", "NEMOTRON_API_KEY", "AI_VISION_MODEL"):
        monkeypatch.delenv(k, raising=False)
    r = client.post("/api/editor/sessions", files={"file": ("sketch.png", png(400, 400))}, data={"width_m": "12"})
    assert r.status_code == 422 and "needs an AI model" in r.json()["error"]["message"]
    client.app.state.editor_vision = Vision(SKETCH)
    try:
        assert client.post("/api/editor/sessions", files={"file": ("sketch.png", png(400, 400))}).json()["error"]["message"].startswith("Say how wide")
        r = client.post("/api/editor/sessions", files={"file": ("sketch.png", png(400, 400))}, data={"width_m": "12"})
    finally:
        del client.app.state.editor_vision
    assert r.status_code == 201 and r.json()["name"] == "sketch_traced.dxf"
    sid = r.json()["id"]
    proof = zipfile.ZipFile(io.BytesIO(client.get(f"/api/editor/sessions/{sid}/proof-pack.zip").content))
    assert proof.read("original/sketch.png") == png(400, 400)  # the photo itself, under its own name
    assert "edited/sketch_traced_edited.dxf" in proof.namelist()


def test_import_errors_over_http(client):
    r = client.post("/api/editor/sessions", files={"file": ("q.wdp", wdp("quick"))})
    assert r.status_code == 422 and "Quick Layout" in r.json()["error"]["message"]
    r = client.post("/api/editor/sessions", files={"file": ("x.gif", b"GIF89a")})
    assert r.status_code == 415


def test_parse_table_and_fill_title_block(client):
    import ezdxf

    doc = ezdxf.new("R2018")
    tb = doc.blocks.new("TITLEBLOCK")
    for tag in ("PROJECT", "CLIENT", "DATE"):
        tb.add_attdef(tag, (0, 0))
    doc.modelspace().add_blockref("TITLEBLOCK", (0, 0)).add_auto_attribs({"PROJECT": "", "CLIENT": "", "DATE": ""})
    buf = io.StringIO()
    doc.write(buf)
    sid = client.post("/api/editor/sessions", files={"file": ("sheet.dxf", buf.getvalue().encode())}).json()["id"]

    table = client.post("/api/editor/parse-table", files={"file": ("projects.xlsx", make_xlsx([["Project", "Client", "Date"], ["DC North", "Acme", "2026-10-08"]]))}).json()
    row = dict(zip(table["columns"], table["rows"][0]))
    p = client.post(f"/api/editor/sessions/{sid}/stage", json={"ops": [{"op": "fill_attributes", "values": row}]}).json()["proposal"]
    assert "Fill 3 fields" in p["summaries"][0]
    draw = client.post(f"/api/editor/sessions/{sid}/stage", json={"ops": [{"op": "add_table", "x": 0, "y": -10, "rows": [table["columns"]] + table["rows"]}]}).json()["proposal"]
    assert draw["stats"]["added"] > 6
    assert client.post("/api/editor/parse-table", files={"file": ("x.csv", b"")}).status_code == 400
