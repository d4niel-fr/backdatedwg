"""Health check, analysis (inspect, area, take-off, rooms, schedules, warehouse, explain) and layer standards."""

import ezdxf
import pytest

from app.editor import analysis, health, ops, sample, standards
from app.editor.session import EditorStore
from app.editor.units import Units
from conftest import wait  # noqa: F401  (keeps conftest's path setup)

MM = Units(4, "millimetres", "mm", 0.001)


def messy():
    """Rooms, doors with attributes, racks, and a catalogue of problems."""
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for name in ("walls", "Room names", "door_tags", "RACKING", "UNUSED-1", "UNUSED-2", "hidden stuff", "dims"):
        doc.layers.add(name)
    doc.layers.get("hidden stuff").off()
    # two rooms 10 m x 8 m and 5 m x 8 m
    msp.add_lwpolyline([(0, 0), (10000, 0), (10000, 8000), (0, 8000)], close=True, dxfattribs={"layer": "walls"})
    msp.add_lwpolyline([(10000, 0), (15000, 0), (15000, 8000), (10000, 8000)], close=True, dxfattribs={"layer": "walls"})
    msp.add_text("OFFICE", height=300, dxfattribs={"layer": "Room names"}).set_placement((4000, 4000))
    msp.add_text("STORE", height=300, dxfattribs={"layer": "Room names"}).set_placement((12000, 4000))
    # doors with attributes
    door = doc.blocks.new("DOOR")
    door.add_line((0, 0), (900, 0))
    door.add_attdef("NUM", (0, 0))
    door.add_attdef("WIDTH", (0, -100))
    for i, (x, w) in enumerate(((2000, "900"), (6000, "900"), (11000, "1200"))):
        msp.add_blockref("DOOR", (x, 0), dxfattribs={"layer": "door_tags"}).add_auto_attribs({"NUM": f"D{i + 1}", "WIDTH": w})
    doc.blocks.new("NEVER_USED").add_circle((0, 0), 1)
    # rack rows along x at y=20000 and y=23000 (1100 deep -> 1900 aisle), and y=27000 (2900 aisle)
    for y in (20000, 23000, 27000):
        for n in range(5):
            msp.add_lwpolyline([(n * 2700, y), (n * 2700 + 2700, y), (n * 2700 + 2700, y + 1100), (n * 2700, y + 1100)], close=True, dxfattribs={"layer": "RACKING"})
    # problems
    msp.add_line((0, 0), (1000, 0), dxfattribs={"layer": "walls"})
    msp.add_line((1000, 0), (0, 0), dxfattribs={"layer": "walls"})  # duplicate
    msp.add_line((500, 500), (500, 500), dxfattribs={"layer": "walls"})  # zero length
    msp.add_text("   ", dxfattribs={"layer": "Room names"})  # empty
    msp.add_circle((5e8, 5e8), 10, dxfattribs={"layer": "walls"})  # stray, far away
    msp.add_line((0, 100, 50), (100, 100, 50), dxfattribs={"layer": "walls"})  # not flat
    msp.add_line((0, 0), (10, 10), dxfattribs={"layer": "hidden stuff"})
    doc.styles.add("OLDFONT", font="romans.shx")
    d = msp.add_linear_dim(base=(0, -1000), p1=(0, 0), p2=(10000, 0), dxfattribs={"layer": "dims"}, override={"dimlfac": 1})
    d.render()
    d.dimension.dxf.text = "9500"  # typed over: says 9500, measures 10000
    return doc


@pytest.fixture
def s(tmp_path):
    return EditorStore(tmp_path / "ed").create(messy(), "messy.dxf")


def by_id(report):
    return {f["id"]: f for f in report["findings"]}


def test_health_finds_every_problem(s):
    r = s.health()
    f = by_id(r)
    for fid in ("unused-layers", "unused-blocks", "duplicates", "zero-length", "empty-text", "strays", "not-flat",
                "shx-fonts", "hidden-content", "dimension-overrides"):
        assert fid in f, fid
    assert "UNUSED-1" in f["unused-layers"]["detail"] and "NEVER_USED" in f["unused-blocks"]["detail"]
    assert f["dimension-overrides"]["severity"] == "error" and "9500" in f["dimension-overrides"]["detail"]
    assert len(f["strays"]["handles"]) == 1
    assert 0 <= r["score"] < 100
    assert r["findings"][0]["severity"] == "error"  # worst first


def test_health_fixes_go_through_proposals(s):
    r = s.health()
    for fid in ("duplicates", "zero-length", "strays", "unused-layers", "unused-blocks", "empty-text", "not-flat", "shx-fonts"):
        fix = by_id(r)[fid]["fix"]
        prop = s.stage(fix["ops"], [], "health")
        assert prop.summaries, fid
    everything = r["fixAll"]
    prop = s.stage(everything["ops"], [], "health")
    s.accept(prop.id)
    after = by_id(s.health())
    for fid in ("duplicates", "zero-length", "unused-layers", "unused-blocks", "empty-text", "not-flat"):
        assert fid not in after, fid
    assert s.health()["score"] > r["score"]


def test_fix_all_tolerates_fixes_that_find_nothing(s):
    # zero-length lines first, then duplicates: still valid even when one has nothing left to do
    prop = s.stage([{"op": "delete_duplicates", "optional": True}, {"op": "delete_duplicates", "optional": True}], [], "health")
    assert any("Skipped delete duplicates" in w for w in prop.warnings)


def test_units_findings(tmp_path):
    st = EditorStore(tmp_path / "ed")
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 0
    msp = doc.modelspace()
    for i in range(3):
        msp.add_line((0, i * 20000), (60000, i * 20000))
    f = by_id(st.create(doc, "a.dxf").health())
    assert f["units-missing"]["fix"]["ops"] == [{"op": "set_units", "units": "mm"}]
    doc2 = ezdxf.new("R2018")
    doc2.header["$INSUNITS"] = 4
    doc2.modelspace().add_line((0, 0), (60, 40))
    f2 = by_id(st.create(doc2, "b.dxf").health())
    assert f2["units-suspect"]["fix"]["ops"] == [{"op": "set_units", "units": "m"}]


def test_clean_drawing_scores_high(tmp_path):
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6
    doc.layers.add("A-WALL")
    doc.modelspace().add_line((0, 0), (10, 0), dxfattribs={"layer": "A-WALL"})
    r = EditorStore(tmp_path / "ed").create(doc, "c.dxf").health()
    assert r["score"] == 100 and r["findings"] == [] and r["fixAll"] is None


def test_inspect_describes_things(s):
    doc = s.doc
    room = doc.modelspace().query("LWPOLYLINE")[0]
    door = doc.modelspace().query("INSERT")[0]
    rows = {r["handle"]: r for r in analysis.inspect(doc, [room.dxf.handle, door.dxf.handle, "NOPE"], MM)}
    r = rows[room.dxf.handle]
    assert r["area"] == pytest.approx(80_000_000) and r["length"] == pytest.approx(36000)
    assert "80.00 m²" in r["sentence"] and "layer walls" in r["sentence"]
    d = rows[door.dxf.handle]
    assert d["block"] == "DOOR" and d["attributes"] == {"NUM": "D1", "WIDTH": "900"} and "NUM=D1" in d["sentence"]
    assert len(rows) == 2


def test_area_summary(s):
    a = analysis.area_summary(s.doc, s.scene(), [0, 0, 10000, 8000], MM)
    assert "OFFICE" in a["texts"] and a["blocks"].get("DOOR") == 2
    text = analysis.describe_area(a)
    assert "10,000 mm × 8,000 mm" in text and "DOOR ×2" in text
    empty = analysis.area_summary(s.doc, s.scene(), [-9e6, -9e6, -8e6, -8e6], MM)
    assert analysis.describe_area(empty).endswith("is empty.")


def test_takeoff_and_csv(s):
    t = analysis.takeoff(s.doc, MM)
    blocks = {r["block"]: r["count"] for r in t["blocks"]}
    assert blocks["DOOR"] == 3
    areas = {r["layer"]: r for r in t["areas"]}
    # two rooms plus the stray circle (r = 10 mm, negligible area)
    assert areas["walls"]["shapes"] == 3 and areas["walls"]["squareMetres"] == pytest.approx(120, abs=0.01)
    assert areas["RACKING"]["shapes"] == 15
    csv_text = analysis.takeoff_csv(t)
    assert csv_text.splitlines()[0] == "Section,Item,Quantity,Unit,Notes" and "Blocks,DOOR,3,ea" in csv_text


def test_rooms_are_named_by_their_labels(s):
    rows = analysis.rooms(s.doc, MM)
    named = {r["name"]: r for r in rows}
    assert named["OFFICE"]["squareMetres"] == pytest.approx(80) and named["STORE"]["squareMetres"] == pytest.approx(40)
    assert named["OFFICE"]["perimeterMetres"] == pytest.approx(36)


def test_schedule_and_bom(s):
    sched = analysis.schedule(s.doc, "door*")
    assert sched["columns"][:3] == ["Block", "NUM", "WIDTH"] and [r[1] for r in sched["rows"]] == ["D1", "D2", "D3"]
    bom = analysis.schedule(s.doc, None, "bom")
    assert bom["columns"] == ["Item", "Block", "WIDTH", "Qty"]  # NUM is unique per door, so it isn't a BOM column
    qty = {(r[1], r[2]): r[3] for r in bom["rows"]}
    assert qty[("DOOR", "900")] == 2 and qty[("DOOR", "1200")] == 1
    assert analysis.table_csv(bom).startswith("Item,Block,WIDTH,Qty")


def test_warehouse_rows_and_aisles(s):
    w = analysis.warehouse(s.doc, MM, min_aisle_m=2.8)
    assert w["found"] and w["bays"] == 15 and len(w["rows"]) == 3 and w["direction"] == "x"
    widths = [a["widthMetres"] for a in w["aisles"]]
    assert widths == [1.9, 2.9] and w["narrow"] == 1 and "narrower than 2.8 m" in w["message"]


def test_warehouse_on_the_sample_drawing():
    w = analysis.warehouse(sample.build(), MM)
    assert w["found"] and w["bays"] == 120
    # sample: double rows (2 lines 1100 deep with a 200 flue -> one 2.4 m deep row) and 3.2 m aisles
    assert {r["depthMetres"] for r in w["rows"]} == {2.4} and {a["widthMetres"] for a in w["aisles"]} == {3.2}
    assert len(w["rows"]) == 4 and {r["kind"] for r in w["rows"]} == {"double"} and w["narrow"] == 0


def test_no_racking_found(tmp_path):
    doc = ezdxf.new("R2018")
    doc.modelspace().add_line((0, 0), (1, 1))
    assert analysis.warehouse(doc, MM)["found"] is False


def test_explain_mentions_the_facts(s):
    out = analysis.explain(s.doc, s.digest(), MM, {"score": 50, "issues": 4})
    text = out["text"]
    assert "objects on" in text and "millimetres" in text and "OFFICE" in text and "DOOR ×3" in text and "score 50/100" in text


def test_standards_proposal_and_map_layers(s):
    p = standards.propose(s.doc)
    rows = {r["from"]: r for r in p["rows"]}
    assert rows["walls"]["to"] == "A-WALL" and rows["Room names"]["to"] == "A-AREA" and rows["RACKING"]["to"] == "A-EQPM-RACK"
    assert rows["dims"]["to"] == "A-ANNO-DIMS"
    prop = s.stage(p["ops"], [], "standards")
    s.accept(prop.id)
    names = {l.dxf.name for l in s.doc.layers}
    assert {"A-WALL", "A-AREA", "A-EQPM-RACK"} <= names and "walls" not in names and "RACKING" not in names
    assert s.doc.layers.get("A-EQPM-RACK").color == 5
    assert all(e.dxf.layer != "walls" for e in s.doc.modelspace())
    again = standards.propose(s.doc)
    assert again["renames"] == 0 or all(r["status"] != "rename" or r["from"] not in ("walls",) for r in again["rows"])


def test_custom_mapping_parsing():
    assert standards.parse_mapping("old,new\nwalls,A-WALL\n,\nx") == {"walls": "A-WALL"}
    assert standards.parse_mapping('{"a": "B", "c": ""}') == {"a": "B"}
    with pytest.raises(ValueError):
        standards.parse_mapping("nothing useful")


def test_map_layers_merges_into_existing():
    doc = ezdxf.new("R2018")
    for n in ("wall1", "Wall_2", "A-WALL"):
        doc.layers.add(n)
    msp = doc.modelspace()
    for n in ("wall1", "Wall_2", "A-WALL"):
        msp.add_line((0, 0), (1, 0), dxfattribs={"layer": n})
    r = ops.apply_ops(doc, [{"op": "map_layers", "mapping": {"wall1": "A-WALL", "wall_2": "A-WALL", "missing": "X"}}], MM, [], 1)
    assert {e.dxf.layer for e in msp} == {"A-WALL"} and "merged" in r.summaries[0]
    assert not doc.layers.has_entry("wall1") and not doc.layers.has_entry("Wall_2")


# ── HTTP ────────────────────────────────────────────────────────────────────


def test_analysis_endpoints(client):
    sid = client.post("/api/editor/sessions/sample").json()["id"]
    h = client.get(f"/api/editor/sessions/{sid}/health").json()
    ids = {f["id"] for f in h["findings"]}
    assert "unused-layers" in ids and h["fixAll"]
    fix = next(f for f in h["findings"] if f["id"] == "unused-layers")["fix"]
    staged = client.post(f"/api/editor/sessions/{sid}/stage", json={"ops": fix["ops"]}).json()["proposal"]
    assert "XREF-GHOST" in staged["summaries"][0]

    geo = client.get(f"/api/editor/sessions/{sid}/geometry").json()
    handle = next(it["h"] for it in geo["items"] if it["t"] == "INSERT")
    ins = client.post(f"/api/editor/sessions/{sid}/inspect", json={"handles": [handle]}).json()["items"][0]
    assert ins["block"] == "RACK_BAY" and ins["sentence"].startswith("A block reference of block RACK_BAY")

    area = client.post(f"/api/editor/sessions/{sid}/area", json={"bbox": [0, 0, 12000, 36000]}).json()
    assert "OFFICE" in " ".join(area["texts"]) and area["count"] > 0

    t = client.get(f"/api/editor/sessions/{sid}/takeoff").json()
    assert {r["block"]: r["count"] for r in t["blocks"]}["RACK_BAY"] == 120
    csv_resp = client.get(f"/api/editor/sessions/{sid}/takeoff?format=csv")
    assert csv_resp.headers["content-type"].startswith("text/csv") and "RACK_BAY,120" in csv_resp.text

    rooms = client.get(f"/api/editor/sessions/{sid}/rooms").json()
    assert any(r["name"] == "DOCK STAGING" for r in rooms["rooms"])
    assert client.get(f"/api/editor/sessions/{sid}/schedule?mode=bom").json()["total"] == 120
    assert client.get(f"/api/editor/sessions/{sid}/schedule?mode=nope").status_code == 400
    assert "Racking" in client.get(f"/api/editor/sessions/{sid}/explain").json()["text"]
    w = client.get(f"/api/editor/sessions/{sid}/warehouse?min_aisle=3.5").json()
    assert w["narrow"] == 3

    std = client.get(f"/api/editor/sessions/{sid}/standards").json()
    assert std["renames"] >= 1
    custom = client.post(f"/api/editor/sessions/{sid}/standards/custom", json={"mapping": "S-RACK,A-EQPM-RACK"}).json()
    assert custom["proposal"]["stats"]["changed"] == 120
    assert client.post(f"/api/editor/sessions/{sid}/standards/custom", json={"mapping": "rubbish"}).status_code == 422


def test_number_formatting():
    from app.editor.units import fmt_number

    assert MM.show(10000) == "10,000 mm" and MM.show(2000) == "2,000 mm" and MM.show(1234567.25) == "1,234,567.2 mm"
    assert fmt_number(3.5) == "3.5" and fmt_number(0.25) == "0.25" and fmt_number(0.000012) == "0.000012" and fmt_number(-12.5) == "-12.5"


def test_spelling_check_and_fix(tmp_path):
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_text("WHAREHOUSE B - RECIEVING")
    msp.add_mtext("Existant colum to be removed")
    s = EditorStore(tmp_path / "ed").create(doc, "t.dxf")
    f = by_id(s.health())["spelling"]
    assert "wharehouse → warehouse" in f["detail"] and len(f["handles"]) == 2
    prop = s.stage(f["fix"]["ops"], [], "health")
    s.accept(prop.id)
    texts = [e.dxf.text for e in s.doc.modelspace().query("TEXT")] + [e.text for e in s.doc.modelspace().query("MTEXT")]
    assert texts == ["WAREHOUSE B - RECEIVING", "Existing column to be removed"]
    assert "spelling" not in by_id(s.health())
