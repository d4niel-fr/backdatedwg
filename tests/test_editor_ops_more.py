"""The cleanup, text, attribute, numbering, markup and xref operations."""

import ezdxf
import pytest

from app.editor import ops
from app.editor.units import Units

MM = Units(4, "millimetres", "mm", 0.001)


def run(doc, op_list, selection=()):
    return ops.apply_ops(doc, op_list, MM, list(selection), 250)


def new():
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    return doc, doc.modelspace()


def test_explode_block_reference_and_polyline():
    doc, msp = new()
    blk = doc.blocks.new("BAY")
    blk.add_line((0, 0), (10, 0))
    blk.add_circle((5, 5), 1)
    ins = msp.add_blockref("BAY", (100, 0))
    pl = msp.add_lwpolyline([(0, 0), (10, 0), (10, 10)])
    h1, h2 = ins.dxf.handle, pl.dxf.handle
    r = run(doc, [{"op": "explode", "selector": {"type": ["block", "polyline"]}}])
    assert r.touched.deleted == {h1, h2} and len(r.touched.created) == 4
    assert sorted(e.dxftype() for e in msp) == ["CIRCLE", "LINE", "LINE", "LINE"]
    assert min(e.dxf.start.x for e in msp.query("LINE")) == 0 and max(e.dxf.start.x for e in msp.query("LINE")) == 100
    with pytest.raises(ops.OpError, match="nothing selected can be exploded"):
        run(doc, [{"op": "explode", "selector": {"type": "circle"}}])


def test_flatten_fixes_subtle_3d():
    doc, msp = new()
    line = msp.add_line((0, 0, 5), (10, 0, -3))
    msp.add_circle((0, 0, 2), 1)
    msp.add_text("T", dxfattribs={"insert": (0, 0, 7)})
    msp.add_line((0, 0), (1, 1))
    r = run(doc, [{"op": "flatten"}])
    assert len(r.touched.changed) == 3
    assert line.dxf.start.z == 0 and line.dxf.end.z == 0
    with pytest.raises(ops.OpError, match="already at elevation 0"):
        run(doc, [{"op": "flatten"}])


def test_purge_unused_blocks_including_nested():
    doc, msp = new()
    inner = doc.blocks.new("INNER")
    inner.add_line((0, 0), (1, 0))
    outer = doc.blocks.new("OUTER")
    outer.add_blockref("INNER", (0, 0))
    used = doc.blocks.new("USED")
    used.add_circle((0, 0), 1)
    msp.add_blockref("USED", (0, 0))
    r = run(doc, [{"op": "purge_unused_blocks"}])
    names = {b.name for b in doc.blocks}
    assert "USED" in names and "OUTER" not in names and "INNER" not in names
    assert "2 unused blocks" in r.summaries[0]
    with pytest.raises(ops.OpError, match="no unused blocks"):
        run(doc, [{"op": "purge_unused_blocks"}])


def test_delete_duplicates():
    doc, msp = new()
    msp.add_line((0, 0), (10, 0))
    msp.add_line((10, 0), (0, 0))  # reversed copy
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "OTHER"})  # different layer: kept by default
    msp.add_circle((5, 5), 2)
    msp.add_circle((5, 5), 2)
    msp.add_circle((5, 5), 3)
    r = run(doc, [{"op": "delete_duplicates"}])
    assert len(r.touched.deleted) == 2 and len(msp) == 4
    r = run(doc, [{"op": "delete_duplicates", "ignore_layer": True}])
    assert len(r.touched.deleted) == 1
    with pytest.raises(ops.OpError, match="no duplicates"):
        run(doc, [{"op": "delete_duplicates"}])


def test_set_units_label_and_convert():
    doc, msp = new()
    line = msp.add_line((0, 0), (2, 0))
    doc.header["$INSUNITS"] = 4
    run(doc, [{"op": "set_units", "units": "m"}])
    assert doc.header["$INSUNITS"] == 6 and line.dxf.end.x == 2
    run(doc, [{"op": "set_units", "units": "mm", "convert": True}])
    assert doc.header["$INSUNITS"] == 4 and line.dxf.end.x == pytest.approx(2000)
    with pytest.raises(ops.OpError, match="already labelled"):
        run(doc, [{"op": "set_units", "units": "mm"}])
    with pytest.raises(ops.OpError, match="unknown unit"):
        run(doc, [{"op": "set_units", "units": "furlongs"}])
    doc.header["$INSUNITS"] = 0
    with pytest.raises(ops.OpError, match="no units recorded"):
        run(doc, [{"op": "set_units", "units": "m", "convert": True}])


def test_text_style_height_and_normalising():
    doc, msp = new()
    t1 = msp.add_text("A", height=2.5)
    t2 = msp.add_text("B", height=2.6)
    t3 = msp.add_text("C", height=5)
    m = msp.add_mtext("D", dxfattribs={"char_height": 4.9})
    with pytest.raises(ops.OpError, match="no text style"):
        run(doc, [{"op": "set_text_style", "style": "ROMANS"}])
    run(doc, [{"op": "set_text_style", "style": "NOTES", "font": "arial.ttf", "selector": {"type": "text"}}])
    assert doc.styles.get("NOTES").dxf.font == "arial.ttf" and t1.dxf.style == "NOTES" and m.dxf.style == "NOTES"
    r = run(doc, [{"op": "normalize_text_heights", "heights": [2.5, 5]}])
    assert t2.dxf.height == 2.5 and m.dxf.char_height == 5 and len(r.touched.changed) == 2
    run(doc, [{"op": "set_text_height", "selector": {"text": "C"}, "height": "7mm"}])
    assert t3.dxf.height == 7


def test_replace_shx_fonts():
    doc, msp = new()
    doc.styles.add("OLD", font="romans.shx")
    t = msp.add_text("x", dxfattribs={"style": "OLD"})
    r = run(doc, [{"op": "replace_fonts", "font": "arial.ttf"}])
    assert doc.styles.get("OLD").dxf.font == "arial.ttf" and t.dxf.handle in r.touched.changed
    with pytest.raises(ops.OpError, match="no text style"):
        run(doc, [{"op": "replace_fonts", "font": "arial.ttf"}])


def test_fill_title_block_and_placeholders():
    doc, msp = new()
    tb = doc.blocks.new("TITLE")
    tb.add_attdef("PROJECT", (0, 0))
    tb.add_attdef("DATE", (0, -5))
    ins = msp.add_blockref("TITLE", (0, 0))
    ins.add_auto_attribs({"PROJECT": "", "DATE": ""})
    note = msp.add_text("Client: {{ client }}")
    r = run(doc, [{"op": "fill_attributes", "values": {"project": "Warehouse B", "DATE": "2026-10-08", "CLIENT": "Acme"}}])
    vals = {a.dxf.tag: a.dxf.text for a in ins.attribs}
    assert vals == {"PROJECT": "Warehouse B", "DATE": "2026-10-08"} and note.dxf.text == "Client: Acme"
    assert len(r.touched.changed) == 2
    with pytest.raises(ops.OpError, match="no block attribute"):
        run(doc, [{"op": "fill_attributes", "values": {"NOPE": "x"}}])


def test_renumber_in_reading_order():
    doc, msp = new()
    # two rows of circuit labels, drawn out of order
    for label, x, y in (("C-9", 20, 100), ("C-2", 0, 100), ("C-7", 10, 100), ("C-1", 0, 50), ("C-5", 10, 50), ("X", 0, 0)):
        msp.add_text(label, height=2).set_placement((x, y))
    run(doc, [{"op": "renumber", "match": r"^C-\d+$", "start": 1}])
    got = {(round(t.dxf.insert.x), round(t.dxf.insert.y)): t.dxf.text for t in msp.query("TEXT")}
    assert got[(0, 100)] == "C-1" and got[(10, 100)] == "C-2" and got[(20, 100)] == "C-3"
    assert got[(0, 50)] == "C-4" and got[(10, 50)] == "C-5" and got[(0, 0)] == "X"
    run(doc, [{"op": "renumber", "match": r"^C-", "prefix": "P", "pad": 2, "order": "top-bottom", "start": 1}])
    got = {(round(t.dxf.insert.x), round(t.dxf.insert.y)): t.dxf.text for t in msp.query("TEXT")}
    assert got[(0, 100)] == "P01" and got[(0, 50)] == "P02" and got[(10, 100)] == "P03"
    with pytest.raises(ops.OpError, match="order"):
        run(doc, [{"op": "renumber", "order": "diagonal"}])


def test_renumber_block_attribute():
    doc, msp = new()
    door = doc.blocks.new("DOOR")
    door.add_attdef("NUM", (0, 0))
    for x in (30, 10, 20):
        msp.add_blockref("DOOR", (x, 0)).add_auto_attribs({"NUM": "D?"})
    run(doc, [{"op": "renumber", "tag": "num", "prefix": "D"}])
    nums = [i.get_attrib_text("NUM") for i in sorted(msp.query("INSERT"), key=lambda i: i.dxf.insert.x)]
    assert nums == ["D1", "D2", "D3"]


def test_revision_cloud_and_tag():
    doc, msp = new()
    c = msp.add_circle((50, 50), 10)
    r = run(doc, [{"op": "revision_cloud", "selector": {"handles": [c.dxf.handle]}, "rev": "B"}])
    clouds = [e for e in msp.query("LWPOLYLINE") if e.dxf.layer == "REV-CLOUD"]
    assert clouds and clouds[0].closed and any(p[4] != 0 for p in clouds[0].get_points("xyseb"))
    assert any(t.dxf.text == "B" for t in msp.query("TEXT")) and len(r.touched.created) == 3
    assert doc.layers.get("REV-CLOUD").color == 1
    run(doc, [{"op": "revision_cloud", "bbox": [0, 0, "1m", "1m"]}])


def test_add_table():
    doc, msp = new()
    r = run(doc, [{"op": "add_table", "x": 0, "y": 0, "rows": [["Door", "Qty"], ["D1", 4], ["D2", None]], "title": "Door schedule", "text_height": 2.5}])
    texts = [t.dxf.text for t in msp.query("TEXT")]
    assert "Door schedule" in texts and "D1" in texts and "4" in texts
    assert len(msp.query("LINE")) == 4 + 3  # 3 rows -> 4 horizontal, 2 cols -> 3 vertical
    assert len(r.touched.created) == len(msp)
    with pytest.raises(ops.OpError, match="col_widths"):
        run(doc, [{"op": "add_table", "x": 0, "y": 0, "rows": [["a", "b"]], "col_widths": [1]}])


def test_rename_block():
    doc, msp = new()
    doc.blocks.new("old_door").add_line((0, 0), (1, 0))
    doc.blocks.new("WALL_SET").add_blockref("old_door", (5, 0))  # a reference inside another block
    i = msp.add_blockref("old_door", (0, 0))
    run(doc, [{"op": "rename_block", "old": "old_door", "new": "A-DOOR-SGL"}])
    assert i.dxf.name == "A-DOOR-SGL" and "A-DOOR-SGL" in doc.blocks
    assert doc.blocks.get("WALL_SET").query("INSERT")[0].dxf.name == "A-DOOR-SGL"
    auditor = doc.audit()
    assert not auditor.has_errors
    with pytest.raises(ops.OpError, match="no block named"):
        run(doc, [{"op": "rename_block", "old": "nope", "new": "x"}])


def test_xref_repath_and_detach():
    doc, msp = new()
    doc.add_xref_def("C:/old/site.dwg", "SITE")
    msp.add_blockref("SITE", (0, 0))
    run(doc, [{"op": "set_xref_path", "block": "SITE", "path": "../site/site_v2.dwg"}])
    assert doc.blocks.get("SITE").block.dxf.xref_path == "../site/site_v2.dwg"
    r = run(doc, [{"op": "detach_xref", "block": "SITE"}])
    assert "SITE" not in doc.blocks and not msp.query("INSERT") and len(r.touched.deleted) == 1
    doc.blocks.new("PLAIN")
    with pytest.raises(ops.OpError, match="isn't an external reference"):
        run(doc, [{"op": "set_xref_path", "block": "PLAIN", "path": "x.dwg"}])


def test_every_op_is_documented_for_the_model():
    text = ops.vocabulary()
    for name in ops.OPS:
        assert f"{name}(" in text
