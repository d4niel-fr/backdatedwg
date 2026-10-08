"""Recipes, batch jobs, the watched folder, audits, email, webhooks and the command line."""

import io
import json
import os
import time
import zipfile

import ezdxf
import pytest

from app.editor import batch, cli
from app.editor.session import EditorStore
from app.engines import Engines
from test_editor_analysis import messy

FALLBACK = Engines(oda=None, libredwg=None)


def save(doc, path):
    doc.saveas(path)
    return path


@pytest.fixture
def messy_file(tmp_path):
    return save(messy(), tmp_path / "messy.dxf")


# ── recipes ─────────────────────────────────────────────────────────────────


def test_validate_recipe_forms():
    r = batch.validate_recipe('{"name": "Std", "commands": ["purge unused layers"], "output": {"format": "dwg", "target": 2013}}')
    assert r["output"] == {"format": "DWG", "target": 2013} and r["commands"] == ["purge unused layers"]
    r = batch.validate_recipe(["purge unused layers", {"op": "flatten"}])
    assert r["commands"] == ["purge unused layers"] and r["ops"] == [{"op": "flatten"}]


@pytest.mark.parametrize("bad,needle", [
    ("not json", "valid JSON"), ({}, "doesn't do anything"), ({"ops": [{"op": "nope"}]}, "Unknown operation"),
    ({"commands": ["x"], "output": {"format": "PDF"}}, "DXF or DWG"), ({"commands": ["x"], "output": {"target": 1999}}, "target"),
    ({"commands": [""]}, "sentences"), ({"commands": ["x"] * 61}, "at most 60"),
])
def test_recipe_errors(bad, needle):
    with pytest.raises(batch.RecipeError, match=needle):
        batch.validate_recipe(bad)


def test_process_file_applies_and_skips(messy_file, tmp_path):
    recipe = batch.validate_recipe({"name": "Tidy", "commands": ["purge unused layers", "delete duplicates", "purge unused blocks",
                                                                  "rename layer GHOST to X", "make it beautiful"],
                                    "ops": [{"op": "flatten"}]})
    r = batch.process_file(messy_file, recipe, FALLBACK, tmp_path / "w")
    steps = [a["step"] for a in r["applied"]]
    assert steps == ["purge unused layers", "delete duplicates", "purge unused blocks", "ops"]
    assert any("GHOST" in s for s in r["skipped"]) and any("make it beautiful" in e for e in r["errors"])
    assert r["healthAfter"] > r["healthBefore"] and r["output"] == "messy.dxf"
    out = ezdxf.readfile(tmp_path / "w" / "out" / "messy.dxf")
    assert not out.layers.has_entry("UNUSED-1")


def test_process_file_runs_plans_and_converts(messy_file, tmp_path):
    recipe = batch.validate_recipe({"commands": ["clean up the drawing", "standardize layers"], "output": {"format": "DXF", "target": 2004}})
    r = batch.process_file(messy_file, recipe, FALLBACK, tmp_path / "w")
    assert r["status"] == "done" and r["converted"].startswith("AutoCAD 2004")
    out = ezdxf.readfile(tmp_path / "w" / "out" / "messy.dxf")
    assert out.dxfversion == "AC1018" and out.layers.has_entry("A-WALL")


def test_dwg_output_without_oda_falls_back_to_dxf(messy_file, tmp_path):
    r = batch.process_file(messy_file, batch.validate_recipe({"commands": ["purge"], "output": {"format": "DWG", "target": 2010}}), FALLBACK, tmp_path / "w")
    assert r["output"].endswith(".dxf") and any("DWG output needs ODA" in e for e in r["errors"])


def test_dry_run_writes_nothing(messy_file, tmp_path):
    r = batch.process_file(messy_file, batch.validate_recipe({"commands": ["purge"]}), FALLBACK, tmp_path / "w", dry_run=True)
    assert r["changes"] == 1 and "output" not in r and not (tmp_path / "w" / "out").exists()


def test_unreadable_file(tmp_path):
    bad = tmp_path / "bad.dxf"
    bad.write_text("rubbish")
    r = batch.process_file(bad, batch.validate_recipe({"commands": ["purge"]}), FALLBACK, tmp_path / "w")
    assert r["status"] == "failed" and r["errors"]


def test_recorded_recipe_replays_on_another_file(tmp_path):
    s = EditorStore(tmp_path / "ed").create(messy(), "messy.dxf")
    for cmd in (["purge_unused_layers"], ["replace_text"]):
        op = {"op": cmd[0]} if cmd[0] != "replace_text" else {"op": "replace_text", "find": "OFFICE", "replace": "OFFICES"}
        s.accept(s.stage([op], [], "local", cmd[0]).id)
    s.undo()
    s.redo()
    recipe = batch.validate_recipe(batch.recipe_from_log("messy.dxf", s.log))
    assert [st["title"] for st in recipe["steps"]] == ["purge_unused_layers", "replace_text"]
    other = save(messy(), tmp_path / "other.dxf")
    r = batch.process_file(other, recipe, FALLBACK, tmp_path / "w")
    assert len(r["applied"]) == 2
    assert any(t.dxf.text == "OFFICES" for t in ezdxf.readfile(tmp_path / "w" / "out" / "other.dxf").modelspace().query("TEXT"))
    with pytest.raises(batch.RecipeError, match="Nothing has been changed"):
        batch.recipe_from_log("x", [])


class Fake:
    name = "fake"

    def complete(self, messages, max_tokens=None, on_delta=None):
        return json.dumps({"reply": "Purging.", "queries": [], "ops": [{"op": "purge_unused_blocks"}]})


def test_ai_instruction_in_a_recipe(messy_file, tmp_path):
    recipe = batch.validate_recipe({"ai_instruction": "get rid of block definitions nobody uses"})
    r = batch.process_file(messy_file, recipe, FALLBACK, tmp_path / "w", model=Fake())
    assert r["applied"][0]["step"].startswith("AI:") and "unused block" in r["applied"][0]["summaries"][0]
    r2 = batch.process_file(messy_file, recipe, FALLBACK, tmp_path / "w2", model=None)
    assert any("isn't connected" in e for e in r2["errors"])


def test_report_text(messy_file, tmp_path):
    recipe = batch.validate_recipe({"name": "R", "commands": ["purge", "rename layer GHOST to X"]})
    text = batch.report_text(recipe, [batch.process_file(messy_file, recipe, FALLBACK, tmp_path / "w")], False)
    assert "Batch report: R" in text and "✓ purge" in text and "– skipped rename layer GHOST" in text


# ── webhooks ────────────────────────────────────────────────────────────────


def fake_dns(monkeypatch, ip):
    monkeypatch.setattr(batch.socket, "getaddrinfo", lambda host, port, proto=0: [(2, 1, 6, "", (ip, port))])


def test_callback_checks(monkeypatch):
    monkeypatch.delenv("BACKDATE_ALLOW_HTTP_CALLBACKS", raising=False)
    with pytest.raises(batch.WebhookError, match="https"):
        batch.check_callback("http://example.com/hook")
    with pytest.raises(batch.WebhookError, match="https"):
        batch.check_callback("ftp://example.com/hook")
    for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "::1"):
        fake_dns(monkeypatch, ip)
        with pytest.raises(batch.WebhookError, match="public address"):
            batch.check_callback("https://sneaky.example/hook")
    fake_dns(monkeypatch, "93.184.216.34")
    assert batch.check_callback("https://hooks.example.com/x") == "https://hooks.example.com/x"


def test_post_callback_never_raises(monkeypatch):
    fake_dns(monkeypatch, "93.184.216.34")
    sent = {}

    class Opener:
        def open(self, req, timeout):
            sent["body"] = json.loads(req.data)
            return io.BytesIO(b"ok")

    monkeypatch.setattr(batch.urllib.request, "build_opener", lambda *a: Opener())
    assert batch.post_callback("https://hooks.example.com/x", {"event": "batch.finished"}) and sent["body"]["event"] == "batch.finished"
    fake_dns(monkeypatch, "127.0.0.1")
    assert batch.post_callback("https://evil.example/x", {}) is False


# ── watched folder, audit, email ────────────────────────────────────────────


def test_watch_once(tmp_path, messy_file):
    folder = tmp_path / "hot"
    (folder / "in").mkdir(parents=True)
    dest = folder / "in" / "plan.dxf"
    dest.write_bytes(messy_file.read_bytes())
    (folder / "in" / "notes.txt").write_text("ignored")
    fresh = folder / "in" / "fresh.dxf"
    fresh.write_bytes(messy_file.read_bytes())
    old = time.time() - 60
    os.utime(dest, (old, old))
    results = batch.watch_once(folder, batch.validate_recipe({"commands": ["purge"]}), FALLBACK)
    assert [r["file"] for r in results] == ["plan.dxf"]  # fresh.dxf may still be copying
    assert (folder / "out" / "plan.dxf").exists() and (folder / "out" / "plan_report.txt").exists() and (folder / "done" / "plan.dxf").exists()
    assert fresh.exists() and (folder / "in" / "notes.txt").exists()


def test_audit_folder(tmp_path, messy_file):
    d = tmp_path / "drawings"
    (d / "sub").mkdir(parents=True)
    (d / "a.dxf").write_bytes(messy_file.read_bytes())
    clean = ezdxf.new("R2018")
    clean.header["$INSUNITS"] = 6
    clean.layers.add("A-WALL")
    clean.modelspace().add_line((0, 0), (5, 0), dxfattribs={"layer": "A-WALL"})
    clean.saveas(d / "sub" / "b.dxf")
    (d / "broken.dxf").write_text("nope")
    rows, text, csv_text = batch.audit_folder(d, FALLBACK)
    by = {r["file"]: r for r in rows}
    assert by[os.path.join("sub", "b.dxf")]["score"] == 100 and by["a.dxf"]["score"] < 100 and by["broken.dxf"]["score"] is None
    assert "3 drawing(s)" in text and csv_text.startswith("File,Score,Issues")
    assert not (d / ".audit").exists()


def test_send_email(monkeypatch):
    sent = {}

    class SMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = u

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(batch.smtplib, "SMTP", SMTP)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        batch.send_email("a@b.c", "s", "b")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    batch.send_email("cad@example.com", "Audit", "body", {"audit.csv": b"File,Score\n"})
    assert sent["host"] == ("smtp.example.com", 587) and sent["tls"] and sent["login"] == "bot@example.com"
    assert sent["msg"]["To"] == "cad@example.com" and any(p.get_filename() == "audit.csv" for p in sent["msg"].iter_attachments())


# ── command line ────────────────────────────────────────────────────────────


@pytest.fixture
def no_engines(monkeypatch):
    monkeypatch.setattr(cli.Engines, "discover", classmethod(lambda cls: FALLBACK))


def test_cli(no_engines, messy_file, tmp_path, capsys):
    assert cli.main(["health", str(messy_file)]) == 1  # below 80
    assert "health" in capsys.readouterr().out
    assert cli.main(["explain", str(messy_file)]) == 0 and "objects on" in capsys.readouterr().out
    assert cli.main(["takeoff", str(messy_file), "--csv", str(tmp_path / "q.csv")]) == 0 and (tmp_path / "q.csv").read_text().startswith("Section")
    capsys.readouterr()
    assert cli.main(["rooms", str(messy_file)]) == 0 and "OFFICE" in capsys.readouterr().out
    assert cli.main(["aisles", str(messy_file)]) == 1 and "narrower" in capsys.readouterr().out
    assert cli.main(["apply", str(messy_file), "--command", "purge unused layers", "--command", "delete duplicates", "--out", str(tmp_path / "o")]) == 0
    assert (tmp_path / "o" / "messy.dxf").exists() and "✓ purge unused layers" in (tmp_path / "o" / "report.txt").read_text()
    capsys.readouterr()
    # --out ending in .dxf names the output file itself
    assert cli.main(["apply", str(messy_file), "--command", "purge unused layers", "--target", "2010", "--out", str(tmp_path / "named" / "clean.dxf")]) == 0
    assert ezdxf.readfile(tmp_path / "named" / "clean.dxf").acad_release == "R2010" and "wrote" in capsys.readouterr().out
    folder = tmp_path / "many"
    folder.mkdir()
    for n in ("x.dxf", "y.dxf"):
        (folder / n).write_bytes(messy_file.read_bytes())
    recipe = tmp_path / "r.json"
    recipe.write_text(json.dumps({"name": "R", "commands": ["purge"]}))
    assert cli.main(["batch", str(folder), "--recipe", str(recipe), "--out", str(tmp_path / "bo"), "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out and not (tmp_path / "bo" / "x.dxf").exists()
    assert cli.main(["audit", str(folder), "--csv", str(tmp_path / "a.csv")]) == 0 and "2 drawing(s)" in capsys.readouterr().out
    other = save(messy(), tmp_path / "other.dxf")
    assert cli.main(["compare", str(messy_file), str(other)]) == 0 and "Drawing comparison" in capsys.readouterr().out
    assert cli.main(["pdf", str(messy_file), "--out", str(tmp_path / "p.pdf"), "--paper", "A1"]) == 0
    assert (tmp_path / "p.pdf").read_bytes().startswith(b"%PDF") and "A1" in capsys.readouterr().out
    assert cli.main(["apply", str(messy_file), "--out", str(tmp_path / "z")]) == 2  # no recipe or command
    folder2 = tmp_path / "hot"
    (folder2 / "in").mkdir(parents=True)
    f = folder2 / "in" / "w.dxf"
    f.write_bytes(messy_file.read_bytes())
    os.utime(f, (time.time() - 60, time.time() - 60))
    assert cli.main(["watch", str(folder2), "--recipe", str(recipe), "--once"]) == 0 and (folder2 / "out" / "w.dxf").exists()


# ── HTTP ────────────────────────────────────────────────────────────────────


def wait_batch(client, jid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/editor/batch/{jid}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.1)
    raise AssertionError("batch did not finish")


def test_batch_over_http(client, messy_file):
    data = messy_file.read_bytes()
    recipe = json.dumps({"name": "Office", "commands": ["purge unused layers", "delete duplicates"]})
    r = client.post("/api/editor/batch", files=[("files", ("a.dxf", data)), ("files", ("b.dxf", data))], data={"recipe": recipe})
    assert r.status_code == 201, r.text
    job = wait_batch(client, r.json()["id"])
    assert job["status"] == "done" and job["done"] == 2 and all(x["status"] == "done" for x in job["results"])
    z = zipfile.ZipFile(io.BytesIO(client.get(job["downloadUrl"]).content))
    assert {"output/a.dxf", "output/b.dxf", "report.txt", "recipe.json"} <= set(z.namelist())
    assert "Batch report: Office" in client.get(job["reportUrl"]).text

    dry = wait_batch(client, client.post("/api/editor/batch", files=[("files", ("a.dxf", data))], data={"recipe": recipe, "dry_run": "true"}).json()["id"])
    assert dry["downloadUrl"] is None and dry["results"][0]["changes"] == 2


def test_batch_request_errors(client, messy_file):
    data = messy_file.read_bytes()
    assert client.post("/api/editor/batch", files=[("files", ("a.dxf", data))], data={"recipe": "{}"}).status_code == 422
    assert client.post("/api/editor/batch", files=[("files", ("a.txt", b"x"))], data={"recipe": '["purge"]'}).status_code == 415
    r = client.post("/api/editor/batch", files=[("files", ("a.dxf", data))], data={"recipe": '["purge"]', "callback_url": "http://localhost/x"})
    assert r.status_code == 422 and "https" in r.json()["error"]["message"]
    r = client.post("/api/editor/batch", files=[("files", ("a.dxf", data))], data={"recipe": json.dumps({"commands": ["purge"], "output": {"format": "DWG"}})})
    assert r.status_code == 400 and r.json()["error"]["code"] == "no_engine"
    assert client.get("/api/editor/batch/nope").status_code == 404


def test_recipe_endpoints(client):
    sid = client.post("/api/editor/sessions/sample").json()["id"]
    assert client.get(f"/api/editor/sessions/{sid}/recipe").status_code == 409
    p = client.post(f"/api/editor/sessions/{sid}/chat", json={"message": "purge unused layers"}).json()["proposal"]
    client.post(f"/api/editor/sessions/{sid}/proposals/{p['id']}/accept")
    rec = client.get(f"/api/editor/sessions/{sid}/recipe").json()
    assert rec["steps"][0]["title"] == "purge unused layers"
    assert client.post("/api/editor/recipes/validate", json={"recipe": rec}).json()["recipe"]["steps"]
    assert client.post("/api/editor/recipes/validate", json={"recipe": {"ops": [{"op": "zap"}]}}).status_code == 422
    sid2 = client.post("/api/editor/sessions/sample").json()["id"]
    applied = client.post(f"/api/editor/sessions/{sid2}/recipe/apply", json={"recipe": {"commands": ["purge unused layers", "standardize layers"]}}).json()
    assert [st["title"] for st in applied["proposal"]["steps"]][0] == "purge unused layers"
    assert client.post(f"/api/editor/sessions/{sid2}/recipe/apply", json={"recipe": {"commands": ["dance"]}}).status_code == 422
    # a recipe that finds nothing to do is an answer, not an error
    p = applied["proposal"]
    client.post(f"/api/editor/sessions/{sid2}/proposals/{p['id']}/accept", json={"steps": [0]})
    again = client.post(f"/api/editor/sessions/{sid2}/recipe/apply", json={"recipe": {"commands": ["purge unused layers"]}})
    assert again.status_code == 200 and again.json()["proposal"] is None and "no unused layers" in again.json()["message"]
