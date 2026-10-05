import io

import ezdxf
from ezdxf import recover

from app import jobs as jobs_mod
from conftest import build_sample, upload, wait


def test_config_without_oda(client):
    cfg = client.get("/api/config").json()
    assert [t["year"] for t in cfg["targets"]] == [2000, 2004, 2007, 2010, 2013, 2018]
    assert cfg["formats"] == ["DXF"]
    assert cfg["defaultTarget"] == 2010
    assert cfg["maxUploadMB"] == 200


def test_static_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "Backdate" in r.text


def test_dxf_downgrade_reports_skipped(client, sample_dxf):
    r = upload(client, sample_dxf, target=2004)
    assert r.status_code == 201, r.text
    job = wait(client, r.json()["id"])
    assert job["status"] == "done", job
    res = job["result"]
    assert res["outputName"] == "Floorplan Level3_2004.dxf"
    items = {(i["entity"], i["where"]): i for i in res["items"]}
    assert items[("Mesh", "Layer S-TOPO")]["kind"] == "skipped"
    assert "added in 2010" in items[("Mesh", "Layer S-TOPO")]["reason"]
    assert ("Multileader", "Layer A-ANNO") in items
    assert ("Helix", "Layer A-ANNO") in items
    assert ("Helix", "Layer 0, block DOOR") in items
    assert items[("Transparency", "1 object")]["kind"] == "changed"
    assert res["counts"]["skipped"] == 4

    out = client.get(res["downloadUrl"])
    assert out.status_code == 200
    assert "Floorplan%20Level3_2004.dxf" in out.headers["content-disposition"] or "Floorplan Level3_2004.dxf" in out.headers["content-disposition"]
    doc, auditor = recover.read(io.BytesIO(out.content))
    assert doc.dxfversion == "AC1018"
    types = {e.dxftype() for e in doc.modelspace()}
    assert types == {"LINE", "CIRCLE", "INSERT"}
    assert not doc.modelspace().query("LINE")[0].dxf.hasattr("transparency")

    txt = client.get(res["reportUrl"])
    assert txt.status_code == 200
    assert "SKIPPED" in txt.text and "Mesh" in txt.text
    assert "attachment" in txt.headers["content-disposition"]


def test_same_version_has_no_issues(client, tmp_path):
    path = tmp_path / "plain.dxf"
    doc = ezdxf.new("R2010")
    doc.modelspace().add_line((0, 0), (1, 1))
    doc.saveas(path)
    job = wait(client, upload(client, path, target=2010).json()["id"])
    assert job["status"] == "done"
    assert job["result"]["items"] == []


def test_rejects_wrong_extension(client, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    r = upload(client, p)
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_type"


def test_rejects_garbage_content(client, tmp_path):
    p = tmp_path / "fake.dwg"
    p.write_bytes(b"%PDF-1.7 definitely not a drawing")
    r = upload(client, p)
    assert r.status_code == 422


def test_rejects_too_large(client, tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "MAX_UPLOAD", 1000)
    p = build_sample(tmp_path / "big.dxf")
    r = upload(client, p)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "too_large"


def test_rejects_newer_target(client, tmp_path):
    p = build_sample(tmp_path / "old.dxf", version="R2004")
    r = upload(client, p, target=2010)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "already_older"


def test_dwg_output_needs_oda(client, sample_dxf):
    r = upload(client, sample_dxf, fmt="DWG")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "no_engine"


def test_corrupt_file_fails_job(client, tmp_path):
    p = tmp_path / "broken.dxf"
    p.write_bytes(b"  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1032\n  0\nENDSEC\n  0\nSECTION\n  2\nENTITIES\n  0\nLINE\n 10\nnot-a-number\n")
    r = upload(client, p)
    assert r.status_code == 201
    job = wait(client, r.json()["id"])
    # recover() either repairs the file (and lists that) or the job fails cleanly
    assert job["status"] in ("done", "failed")
    if job["status"] == "failed":
        assert job["error"]["code"] == "corrupt"


def test_detect_endpoint(client, sample_dxf):
    with open(sample_dxf, "rb") as fh:
        r = client.post("/api/detect", files={"file": ("a.dxf", fh.read(65536))})
    assert r.status_code == 200
    assert r.json()["code"] == "AC1032" and r.json()["kind"] == "DXF"


def test_cancel_deletes_job(client, sample_dxf):
    job_id = upload(client, sample_dxf).json()["id"]
    assert client.delete(f"/api/jobs/{job_id}").status_code == 204
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.delete(f"/api/jobs/{job_id}").status_code == 404


def test_sweep_expires_jobs(client, sample_dxf, monkeypatch):
    job_id = upload(client, sample_dxf).json()["id"]
    wait(client, job_id)
    manager = client.app.state.jobs
    job = manager.get(job_id)
    assert job.dir.exists()
    manager.sweep(now=job.finished + jobs_mod.RETENTION_SECONDS + 1)
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert not job.dir.exists()


def test_oda_path_dwg_output(oda_client, sample_dxf):
    cfg = oda_client.get("/api/config").json()
    assert cfg["formats"] == ["DWG", "DXF"] and cfg["engines"]["oda"]
    r = upload(oda_client, sample_dxf, target=2007, fmt="DWG")
    assert r.status_code == 201, r.text
    job = wait(oda_client, r.json()["id"])
    assert job["status"] == "done", job
    res = job["result"]
    assert res["outputName"].endswith("_2007.dwg")
    assert res["engine"] == "ODA File Converter"
    items = {(i["entity"], i["where"]): i for i in res["items"]}
    # MESH was turned into something else by the converter -> "changed"
    assert items[("Mesh", "Layer S-TOPO")]["kind"] == "changed"
    assert "Polyline" in items[("Mesh", "Layer S-TOPO")]["reason"]
    # 2007 has multileaders and helixes, so nothing else is skipped
    assert res["counts"]["skipped"] == 0
    assert any(i["entity"] == "Audit" for i in res["items"])
    assert oda_client.get(res["downloadUrl"]).content.startswith(b"FAKEDWG")


def test_oda_path_dxf_output(oda_client, sample_dxf):
    job = wait(oda_client, upload(oda_client, sample_dxf, target=2000, fmt="DXF").json()["id"])
    assert job["status"] == "done", job
    skipped = {i["entity"] for i in job["result"]["items"] if i["kind"] == "skipped"}
    assert {"Multileader", "Helix"} <= skipped


def test_cancel_kills_running_engine(oda_client, sample_dxf, monkeypatch):
    import time

    monkeypatch.setenv("FAKE_ODA_SLEEP", "30")
    job_id = upload(oda_client, sample_dxf, fmt="DWG").json()["id"]
    manager = oda_client.app.state.jobs
    job = manager.get(job_id)
    deadline = time.time() + 10
    while job.cancel._proc is None and time.time() < deadline:
        time.sleep(0.05)
    proc = job.cancel._proc
    assert proc is not None, "engine never started"
    started = time.time()
    assert oda_client.delete(f"/api/jobs/{job_id}").status_code == 204
    proc.wait(timeout=5)
    assert time.time() - started < 5
    deadline = time.time() + 5
    while job.dir.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert not job.dir.exists()
    assert job.status == "cancelled"


def test_real_dwg_when_available(client):
    """Set BACKDATE_TEST_DWG to a .dwg file (needs LibreDWG or ODA installed)."""
    import os
    import shutil
    from pathlib import Path

    import pytest

    path = os.environ.get("BACKDATE_TEST_DWG")
    if not path or not shutil.which("dwg2dxf"):
        pytest.skip("no sample DWG / DWG reader")
    job = wait(client, upload(client, Path(path), target=2004).json()["id"])
    assert job["status"] == "done", job
    assert job["result"]["outputName"].endswith("_2004.dxf")


def test_cors_allows_static_frontend(client):
    r = client.get("/api/config", headers={"Origin": "https://backdatedwg.vercel.app"})
    assert r.headers["access-control-allow-origin"] in ("*", "https://backdatedwg.vercel.app")
    pre = client.options(
        "/api/jobs/abc",
        headers={"Origin": "https://backdatedwg.vercel.app", "Access-Control-Request-Method": "DELETE"},
    )
    assert pre.status_code == 200
