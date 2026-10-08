"""Comparing revisions, revision clouds, PDF/SVG exports, change logs and the proof pack."""

import io
import json
import zipfile
import xml.etree.ElementTree as ET

import ezdxf
import pypdf
import pytest

from app.editor import compare, exports, sample
from app.editor.session import EditorStore


def two_revisions():
    old = ezdxf.new("R2018")
    old.header["$INSUNITS"] = 4
    m = old.modelspace()
    keep = m.add_line((0, 0), (1000, 0), dxfattribs={"layer": "A-WALL"})
    moved = m.add_circle((500, 500), 100, dxfattribs={"layer": "A-COLS"})
    gone = m.add_line((0, 5000), (1000, 5000), dxfattribs={"layer": "A-WALL"})
    m.add_text("REV A", dxfattribs={"layer": "A-ANNO"}).set_placement((0, 9000))
    buf = io.StringIO()
    old.write(buf)
    new = ezdxf.read(io.StringIO(buf.getvalue()))  # same handles, like an edited copy
    nm = new.modelspace()
    new.entitydb.get(moved.dxf.handle).dxf.center = (800, 500)
    nm.delete_entity(new.entitydb.get(gone.dxf.handle))
    nm.add_line((2000, 0), (3000, 0), dxfattribs={"layer": "A-WALL"})
    new.layers.add("NEW-LAYER")
    for t in nm.query("TEXT"):
        t.dxf.text = "REV B"
    return old, new, keep.dxf.handle, moved.dxf.handle, gone.dxf.handle


def test_compare_by_handle():
    old, new, keep, moved, gone = two_revisions()
    r = compare.compare(old, new)
    assert r["counts"] == {"added": 1, "removed": 1, "changed": 2, "unchanged": 1}
    assert r["removed"] == [gone] and moved in r["changed"]
    assert r["layersAdded"] == ["NEW-LAYER"] and r["textRemoved"] == ["REV A"] and r["textAdded"] == ["REV B"]
    assert "“REV A” → “REV B”" in r["summary"] and "1 object added" in r["summary"]
    assert r["overlay"]["removed"] and r["overlay"]["removed"][0]["h"] == gone
    assert r["regions"]


def test_compare_matches_by_geometry_when_handles_differ():
    a = ezdxf.new("R2018")
    a.modelspace().add_line((0, 0), (10, 0))
    a.modelspace().add_circle((5, 5), 1)
    b = ezdxf.new("R2018")
    for _ in range(5):
        b.modelspace().add_point((0, 0))  # shifts every handle
    b.modelspace().add_circle((5, 5), 1)
    b.modelspace().add_line((10, 0), (0, 0))  # same line, drawn backwards
    r = compare.compare(a, b)
    assert r["counts"]["unchanged"] == 2 and r["counts"]["removed"] == 0 and r["counts"]["added"] == 5


def test_identical_revisions():
    d = sample.build()
    r = compare.compare(d, d)
    assert r["summary"] == "The two revisions draw exactly the same thing." and r["regions"] == []


def test_regions_merge_nearby_changes():
    old = ezdxf.new("R2018")
    new = ezdxf.new("R2018")
    for x in (0, 10, 20, 10000):
        new.modelspace().add_circle((x, 0), 1)
    for x in range(0, 100000, 2500):
        old.modelspace().add_point((x, 50000))
        new.modelspace().add_point((x, 50000))
    r = compare.compare(old, new)
    assert len(r["regions"]) == 2  # the three close circles share a cloud; the far one has its own


def test_cloud_steps_shape():
    old, new, *_ = two_revisions()
    r = compare.compare(old, new)
    steps = compare.cloud_steps(r, "B", "Moved column", "2026-10-08", (5000, 0), 100)
    assert steps[0]["ops"][0]["op"] == "revision_cloud" and steps[0]["ops"][0]["rev"] == "B"
    assert steps[-1]["ops"][0]["op"] == "add_table" and steps[-1]["ops"][0]["rows"][1] == ["B", "2026-10-08", "Moved column"]


# ── PDF / SVG ───────────────────────────────────────────────────────────────


def scene_of(doc):
    from app.editor import geometry

    return geometry.extract(doc)


def test_pdf_is_valid_to_scale_and_has_a_title_block():
    data, info = exports.drawing_pdf(scene_of(sample.build()), units_to_m=0.001, units_name="millimetres", units_guessed=False,
                                     name="Warehouse B.dxf", paper="A3", fields={"project": "Acme DC", "rev": "C", "drawn_by": "DR"})
    reader = pypdf.PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1
    page = reader.pages[0]
    w_mm, h_mm = float(page.mediabox.width) / exports.MM, float(page.mediabox.height) / exports.MM
    assert round(w_mm) == 420 and round(h_mm) == 297
    text = page.extract_text()
    for needle in ("TITLE", "Warehouse B.dxf", "Acme DC", "SCALE", "1:200 @ A3", "DOCK 1", "ROW A"):
        assert needle in text, needle
    # 62.9 m x 41.2 m on a 400 x 259 mm drawing area: 1:157 and 1:159 needed, so the next standard scale is 1:200
    assert info["scale"] == 200


def test_pdf_hidden_layers_and_portrait():
    data, info = exports.drawing_pdf(scene_of(sample.build()), units_to_m=0.001, units_name="mm", units_guessed=True, name="x",
                                     hidden=["A-ANNO"], paper="a4", orientation="portrait")
    text = pypdf.PdfReader(io.BytesIO(data)).pages[0].extract_text()
    assert "DOCK 1" not in text and "units guessed" in text and "Hidden layers: A-ANNO" in text
    assert info["paper"] == "A4"


def test_pdf_text_escaping():
    pdf = exports.PDF("t")
    pg = pdf.page(200, 200)
    pg.text(10, 10, 10, "a (b) \\c → d ×2 “q”")
    text = pypdf.PdfReader(io.BytesIO(pdf.render())).pages[0].extract_text()
    assert "a (b) \\c -> d ×2 “q”" in text


def test_nice_scale():
    assert exports.nice_scale(161) == 200 and exports.nice_scale(1) == 1 and exports.nice_scale(1.2) == 2 and exports.nice_scale(250000) == 300000


def test_changelog_pdf_paginates():
    log = [{"rev": i, "time": 1_700_000_000 + i, "source": "local", "prompt": f"request {i}", "summaries": [f"Change number {i} " * 8]} for i in range(1, 60)]
    reader = pypdf.PdfReader(io.BytesIO(exports.changelog_pdf("plan.dxf", log, "summary")))
    assert len(reader.pages) > 1
    assert "Change log: plan.dxf" in reader.pages[0].extract_text()


def test_svg_is_well_formed_with_layer_groups():
    svg = exports.drawing_svg(scene_of(sample.build()), hidden=["A-DIMS"], title="W <B> & co")
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    layers = {g.get("data-layer") for g in root.iter(ns + "g") if g.get("data-layer")}
    assert "S-RACK" in layers and "A-DIMS" not in layers
    assert root.find(ns + "title").text == "W <B> & co"
    assert any(t.text == "DOCK 1" or any(s.text == "DOCK 1" for s in t) for t in root.iter(ns + "text"))


# ── HTTP ────────────────────────────────────────────────────────────────────


def accept_cmd(client, sid, message):
    p = client.post(f"/api/editor/sessions/{sid}/chat", json={"message": message}).json()["proposal"]
    assert p, message
    return client.post(f"/api/editor/sessions/{sid}/proposals/{p['id']}/accept").json()


def test_compare_with_original_then_clouds(client):
    sid = client.post("/api/editor/sessions/sample").json()["id"]
    accept_cmd(client, sid, "move layer S-RACK 2 m up")
    accept_cmd(client, sid, 'replace "REV A" with "REV B"')
    r = client.post(f"/api/editor/sessions/{sid}/compare/original").json()
    assert r["counts"]["changed"] == 121 and "S-RACK" in r["summary"] and r["regions"]
    rep = client.get(f"/api/editor/sessions/{sid}/compare/report.txt")
    assert "Drawing comparison" in rep.text and "S-RACK: 0 / 0 / 120" in rep.text
    clouds = client.post(f"/api/editor/sessions/{sid}/compare/clouds", json={"rev": "b", "description": "Racks moved"}).json()["proposal"]
    assert clouds["steps"][0]["title"].startswith("Revision cloud") and clouds["steps"][-1]["title"] == "Revision table entry"
    acc = client.post(f"/api/editor/sessions/{sid}/proposals/{clouds['id']}/accept").json()
    assert "REV-CLOUD" in {l["name"] for l in acc["summary"]["digest"]["layers"]}


def test_clouds_need_a_comparison(client):
    sid = client.post("/api/editor/sessions/sample").json()["id"]
    assert client.post(f"/api/editor/sessions/{sid}/compare/clouds", json={}).status_code == 409
    assert client.get(f"/api/editor/sessions/{sid}/compare/report.txt").status_code == 404


def test_compare_with_an_uploaded_file(client, tmp_path):
    sid = client.post("/api/editor/sessions/sample").json()["id"]
    older = sample.build()
    for t in older.modelspace().query("TEXT"):
        if t.dxf.text == "REV A":
            t.dxf.text = "REV 0"
    path = tmp_path / "older.dxf"
    older.saveas(path)
    with open(path, "rb") as fh:
        r = client.post(f"/api/editor/sessions/{sid}/compare", files={"file": ("older.dxf", fh)}).json()
    assert r["other"] == "older.dxf" and r["textRemoved"] == ["REV 0"] and r["textAdded"] == ["REV A"]
    assert client.post(f"/api/editor/sessions/{sid}/compare", files={"file": ("x.txt", b"hi")}).status_code == 415


def test_export_endpoints(client):
    sid = client.post("/api/editor/sessions/sample").json()["id"]
    pdf = client.get(f"/api/editor/sessions/{sid}/export.pdf?paper=A2&hidden=A-DIMS&project=Acme&rev=D")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF-1.4")
    text = pypdf.PdfReader(io.BytesIO(pdf.content)).pages[0].extract_text()
    assert "Acme" in text and "@ A2" in text
    svg = client.get(f"/api/editor/sessions/{sid}/export.svg")
    assert svg.headers["content-type"].startswith("image/svg+xml") and svg.text.startswith("<svg")
    accept_cmd(client, sid, "purge unused layers")
    txt = client.get(f"/api/editor/sessions/{sid}/changelog.txt").text
    assert 'Asked: "purge unused layers"' in txt
    cpdf = client.get(f"/api/editor/sessions/{sid}/changelog.pdf")
    assert "purge unused layers" in pypdf.PdfReader(io.BytesIO(cpdf.content)).pages[0].extract_text()


def test_proof_pack(client, sample_dxf):
    with open(sample_dxf, "rb") as fh:
        sid = client.post("/api/editor/sessions", files={"file": ("Floorplan Level3.dxf", fh)}).json()["id"]
    accept_cmd(client, sid, "move layer A-WALL 1 m east")
    r = client.get(f"/api/editor/sessions/{sid}/proof-pack.zip")
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    for n in ("original/Floorplan Level3.dxf", "edited/Floorplan Level3_edited.dxf", "changes/changelog.txt", "changes/changelog.pdf",
              "changes/operations.json", "comparison/comparison.txt", "comparison/comparison.json", "health/before.json",
              "health/after.json", "Floorplan Level3_edited.pdf", "MANIFEST.txt", "meta.json"):
        assert n in names, n
    assert z.read("original/Floorplan Level3.dxf") == sample_dxf.read_bytes()  # byte-for-byte the upload
    ops_log = json.loads(z.read("changes/operations.json"))
    assert ops_log[0]["ops"] == [{"op": "move", "selector": {"layer": "A-WALL"}, "dx": "1m", "dy": 0}]
    manifest = z.read("MANIFEST.txt").decode()
    import hashlib

    assert hashlib.sha256(z.read("edited/Floorplan Level3_edited.dxf")).hexdigest() in manifest
    meta = json.loads(z.read("meta.json"))
    assert meta["changes"] == 1 and "healthBefore" in meta and "2 objects changed" in meta["comparison"]
