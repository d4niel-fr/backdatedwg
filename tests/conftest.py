import sys
import time
from pathlib import Path

import ezdxf
import pytest
from ezdxf.math import Vec2
from ezdxf.render import mleader
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import main  # noqa: E402


def build_sample(path: Path, version: str = "R2018") -> Path:
    """A drawing with entities that older versions can't hold."""
    doc = ezdxf.new(version, setup=True)
    for name in ("A-WALL", "S-TOPO", "A-ANNO"):
        doc.layers.add(name)
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "A-WALL", "transparency": 0x02000080})
    msp.add_circle((5, 5), 2, dxfattribs={"layer": "A-WALL"})
    mesh = msp.add_mesh(dxfattribs={"layer": "S-TOPO"})
    with mesh.edit_data() as data:
        data.vertices = [(0, 0, 0), (1, 0, 0), (1, 1, 0)]
        data.faces = [(0, 1, 2)]
    ml = msp.add_multileader_mtext("Standard", dxfattribs={"layer": "A-ANNO"})
    ml.set_content("Note")
    ml.add_leader_line(mleader.ConnectionSide.left, [Vec2(5, 5)])
    ml.build(Vec2(10, 10))
    msp.add_helix(5, 1, 3, dxfattribs={"layer": "A-ANNO"})
    block = doc.blocks.new("DOOR")
    block.add_line((0, 0), (1, 0))
    block.add_helix(1, 1, 1)
    msp.add_blockref("DOOR", (20, 0))
    doc.saveas(path)
    return path


@pytest.fixture
def sample_dxf(tmp_path):
    return build_sample(tmp_path / "Floorplan Level3.dxf")


def make_client(monkeypatch, tmp_path, engine: str):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "jobs")
    if engine == "fake-oda":
        monkeypatch.setenv("BACKDATE_ENGINE", "oda")
        monkeypatch.setenv("ODA_CONVERTER_PATH", str(ROOT / "tests" / "fake_oda.py"))
        monkeypatch.setenv("DISPLAY", ":fake")  # skip xvfb-run for the fake
    else:
        monkeypatch.setenv("BACKDATE_ENGINE", "fallback")
    return TestClient(main.app)


@pytest.fixture
def client(monkeypatch, tmp_path):
    with make_client(monkeypatch, tmp_path, "fallback") as c:
        yield c


@pytest.fixture
def oda_client(monkeypatch, tmp_path):
    with make_client(monkeypatch, tmp_path, "fake-oda") as c:
        yield c


def wait(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def upload(client, path: Path, target=2010, fmt="DXF", name=None):
    with open(path, "rb") as fh:
        return client.post(
            "/api/jobs",
            files={"file": (name or path.name, fh)},
            data={"target": str(target), "format": fmt},
        )
