#!/usr/bin/env python3
"""Test double for ODA File Converter's command line.

ODAFileConverter <in dir> <out dir> <ACADxxxx> <DWG|DXF> <recurse> <audit> [filter]

Real DWG writing isn't available in the test environment, so a "DWG" written
by this fake is DXF text behind a FAKEDWG marker line. Entities that don't
exist in the target version are removed, and MESH becomes a POLYFACE
POLYLINE, like a real downgrade would do.
"""

import io
import os
import sys
import time
from pathlib import Path

from ezdxf import recover

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.report import newer_than  # noqa: E402
from app.versions import VERSIONS  # noqa: E402

MARK = b"FAKEDWG\n"


def main(argv):
    time.sleep(float(os.environ.get("FAKE_ODA_SLEEP", "0")))
    in_dir, out_dir, version, fmt, _recurse, audit, pattern = argv[1:8]
    src = Path(in_dir) / pattern
    data = src.read_bytes()
    if data.startswith(MARK):
        data = data[len(MARK):]
    elif data[:2] == b"AC":
        print("fake ODA can't read real DWG files", file=sys.stderr)
        return 1
    doc, _ = recover.read(io.BytesIO(data))
    target = next(v for v in VERSIONS if v.oda == version)
    msp_like = [doc.modelspace(), *[b for b in doc.blocks if not b.is_any_layout]]
    for space in msp_like:
        for e in list(space):
            if e.dxftype() == "MESH" and newer_than("MESH", target):
                space.add_polyface(dxfattribs={"layer": e.dxf.layer}).append_faces([[(0, 0, 0), (1, 0, 0), (1, 1, 0)]])
                space.delete_entity(e)
            elif newer_than(e.dxftype(), target):
                space.delete_entity(e)
    doc.dxfversion = target.code
    out = Path(out_dir) / (src.stem + "." + fmt.lower())
    buf = io.StringIO()
    doc.write(buf)
    text = buf.getvalue().encode(doc.output_encoding, errors="replace")
    out.write_bytes(MARK + text if fmt == "DWG" else text)
    if audit == "1":
        (Path(out_dir) / (src.name + ".err")).write_text("Fixed: 1 invalid object reference\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
