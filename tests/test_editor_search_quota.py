"""Search across a workspace's drawings, similar drawings, find in a drawing, AI quotas and access keys, local models."""

import json

import pytest

from app.editor import llm
from app.editor.quotas import MeteredModel, Quotas

WS = {"X-Workspace": "workspace-key-abcdef0123456789"}
OTHER = {"X-Workspace": "another-workspace-key-0000000"}


def upload(client, doc, name, headers):
    import io

    buf = io.StringIO()
    doc.write(buf)
    return client.post("/api/editor/sessions", files={"file": (name, buf.getvalue().encode())}, headers=headers).json()


def test_search_and_similar_stay_inside_the_workspace(client):
    from app.editor import sample
    from test_editor_analysis import messy

    wh = upload(client, sample.build(), "warehouse_b.dxf", WS)
    wh2 = sample.build()
    for t in wh2.modelspace().query("TEXT"):
        if t.dxf.text == "REV A":
            t.dxf.text = "REV C"
    upload(client, wh2, "warehouse_b_revC.dxf", WS)
    upload(client, messy(), "offices.dxf", WS)
    upload(client, sample.build(), "theirs.dxf", OTHER)

    hits = client.get("/api/editor/search?q=dock staging", headers=WS).json()["results"]
    names = {h["name"] for h in hits}
    assert names == {"warehouse_b.dxf", "warehouse_b_revC.dxf"}  # never the other workspace's copy
    assert any(m["kind"] == "text" and m["value"] == "DOCK STAGING" for m in hits[0]["matches"])
    assert client.get("/api/editor/search?q=OFFICE", headers=WS).json()["results"][0]["name"] in ("offices.dxf", "warehouse_b.dxf", "warehouse_b_revC.dxf")
    assert client.get("/api/editor/search?q=dock").status_code == 400  # no workspace key

    sim = client.get(f"/api/editor/sessions/{wh['id']}/similar").json()["results"]
    assert sim[0]["name"] == "warehouse_b_revC.dxf" and sim[0]["similarity"] > 0.8
    assert all(r["name"] != "theirs.dxf" for r in sim)
    assert "offices.dxf" not in {r["name"] for r in sim}  # nothing in common, so not "similar" at all


def test_find_in_the_drawing(client):
    sid = client.post("/api/editor/sessions/sample").json()["id"]
    r = client.get(f"/api/editor/sessions/{sid}/find?q=dock").json()
    kinds = {x["kind"] for x in r["results"]}
    labels = [x["label"] for x in r["results"] if x["kind"] == "text"]
    assert "DOCK 1" in labels and "DOCK STAGING" in labels and "layer" in kinds  # layer A-DOCK
    assert all(x["bbox"] for x in r["results"])
    blk = client.get(f"/api/editor/sessions/{sid}/find?q=rack_bay").json()
    assert blk["count"] == 120 and blk["results"][0]["kind"] == "block"
    h = r["results"][0]["handle"]
    assert client.get(f"/api/editor/sessions/{sid}/find?q={h}").json()["results"][0]["kind"] in ("handle", "text")


# ── quotas ──────────────────────────────────────────────────────────────────


class Counting:
    name = "counting"

    def __init__(self):
        self.calls = 0

    def complete(self, messages, max_tokens=None, on_delta=None):
        self.calls += 1
        if on_delta:
            on_delta("content", '{"reply":"ok"}')
        return json.dumps({"reply": "ok", "queries": [], "ops": []})


def test_metered_model_counts_and_stops(monkeypatch):
    monkeypatch.setenv("BACKDATE_AI_DAILY_PER_IP", "2")
    monkeypatch.delenv("BACKDATE_ACCESS_TOKENS", raising=False)
    q = Quotas()
    caller = q.caller({}, "1.2.3.4")
    m = MeteredModel(Counting(), q, caller)
    m.complete([])
    m.complete([], on_delta=lambda k, t: None)
    with pytest.raises(llm.LLMError, match="allowance for your connection is used up"):
        m.complete([])
    assert q.usage(caller) == {"name": "", "daily": 2, "used": 2, "remaining": 0, "keyed": False}
    assert q.usage(q.caller({}, "5.6.7.8"))["used"] == 0  # someone else is unaffected


def test_metered_model_without_streaming_support():
    class Plain:
        name = "plain"

        def complete(self, messages, max_tokens=2048):
            return "x"

    q = Quotas()
    assert MeteredModel(Plain(), q, q.caller({}, "9.9.9.9")).complete([], on_delta=lambda k, t: None) == "x"


def test_forwarded_address_only_when_trusted(monkeypatch):
    monkeypatch.delenv("BACKDATE_TRUST_PROXY", raising=False)
    q = Quotas()
    assert q.caller({"x-forwarded-for": "8.8.8.8"}, "10.0.0.1") == q.caller({}, "10.0.0.1")
    monkeypatch.setenv("BACKDATE_TRUST_PROXY", "1")
    q.reload()
    assert q.caller({"x-forwarded-for": "8.8.8.8, 10.0.0.1"}, "10.0.0.1") == q.caller({}, "8.8.8.8")


def test_access_keys_and_required_mode(client, monkeypatch):
    keys = {"acme-key-1234567890": {"name": "Acme", "daily": 1}, "sam-key-1234567890": {"name": "Sam", "daily": 5}}
    monkeypatch.setenv("BACKDATE_ACCESS_TOKENS", json.dumps(keys))
    monkeypatch.setenv("BACKDATE_REQUIRE_TOKEN", "1")
    client.app.state.quotas.reload()
    client.app.state.editor_llm = Counting()
    try:
        assert client.post("/api/editor/sessions/sample").status_code == 401
        assert client.get("/api/editor/config").status_code == 200  # still open
        acme = {"X-Access-Token": "acme-key-1234567890"}
        sid = client.post("/api/editor/sessions/sample", headers=acme).json()["id"]
        u = client.get("/api/editor/usage", headers=acme).json()
        assert u["name"] == "Acme" and u["daily"] == 1 and u["keysEnabled"] and u["keyRequired"]
        assert client.post(f"/api/editor/sessions/{sid}/chat", json={"message": "tell me a story about racks"}, headers=acme).json()["reply"] == "ok"
        over = client.post(f"/api/editor/sessions/{sid}/chat", json={"message": "and another one"}, headers=acme).json()
        assert "allowance for the key “Acme” is used up" in over["reply"]
        # built-in commands keep working after the allowance is gone
        assert client.post(f"/api/editor/sessions/{sid}/chat", json={"message": "list layers"}, headers=acme).json()["source"] == "local"
        # review links stay open without a key
        token = client.post(f"/api/editor/sessions/{sid}/shares", json={"role": "viewer"}, headers=acme).json()["token"]
        assert client.get(f"/api/editor/shares/{token}").status_code == 200
    finally:
        del client.app.state.editor_llm
        monkeypatch.delenv("BACKDATE_ACCESS_TOKENS")
        monkeypatch.delenv("BACKDATE_REQUIRE_TOKEN")
        client.app.state.quotas.reload()


def test_access_tokens_from_a_file(tmp_path, monkeypatch):
    f = tmp_path / "keys.json"
    f.write_text(json.dumps({"long-enough-key-1": {"name": "Team", "daily": 9}, "short": {}}))
    monkeypatch.setenv("BACKDATE_ACCESS_TOKENS", str(f))
    q = Quotas()
    assert list(q.tokens) == ["long-enough-key-1"] and q.caller({"x-access-token": "long-enough-key-1"}, None)["daily"] == 9


# ── local / self-hosted models ──────────────────────────────────────────────


@pytest.mark.parametrize("provider,base", [("ollama", "http://localhost:11434/v1"), ("lmstudio", "http://localhost:1234/v1"), ("vllm", "http://localhost:8001/v1")])
def test_local_presets_need_no_key(monkeypatch, provider, base):
    for k in llm.KEY_NAMES + ("AI_BASE_URL", "AI_MODEL", "AI_REASONING"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AI_PROVIDER", provider)
    m = llm.from_env()
    assert m.provider == provider and m.base_url == base and m.api_key == "local" and m.reasoning == "off"
    assert "reasoning" not in m._body([], 10, False)
