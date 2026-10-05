import pytest

from app.versions import DetectError, detect


def test_dwg_header():
    det = detect(b"AC1032\x00\x00\x00\x00", "x.dwg")
    assert det.kind == "DWG" and det.code == "AC1032"
    assert det.version.label == "AutoCAD 2018–2026"


def test_text_dxf():
    head = b"  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1024\n  9\n$DWGCODEPAGE\n"
    det = detect(head, "x.dxf")
    assert det.kind == "DXF" and det.code == "AC1024" and det.version.year == 2010


def test_text_dxf_crlf():
    head = b"0\r\nSECTION\r\n2\r\nHEADER\r\n9\r\n$ACADVER\r\n1\r\nAC1018\r\n"
    assert detect(head, "x.dxf").code == "AC1018"


def test_r12_dxf_without_acadver():
    assert detect(b"  0\nSECTION\n  2\nENTITIES\n", "old.dxf").code == "AC1009"


def test_binary_dxf():
    head = b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00\x00SECTION\x00\x02\x00HEADER\x00\x09\x00$ACADVER\x00\x01\x00AC1027\x00"
    det = detect(head, "x.dxf")
    assert det.kind == "DXF" and det.binary_dxf and det.code == "AC1027"


def test_garbage():
    with pytest.raises(DetectError):
        detect(b"%PDF-1.7 hello", "x.dwg")
