"""Agent upgrades: built-in answers and plans, scripts, steps, why, suggestions, streaming and memory."""

import json

import ezdxf
import pytest

from app.editor import agent, sample
from app.editor.session import EditorStore
from test_editor_analysis import messy


class FakeModel:
    name = "fake"

    def __init__(self, *replies, stream_pieces=None):
        self.replies = list(replies)
        self.calls = []
        self.stream_pieces = stream_pieces

    def complete(self, messages, max_tokens=None, on_delta=None):
        self.calls.append(messages)
        r = self.replies.pop(0)
        text = r if isinstance(r, str) else json.dumps(r)
        if on_delta and self.stream_pieces:
            on_delta("reasoning", "let me think about racks")
            for i in range(0, len(text), self.stream_pieces):
                on_delta("content", text[i : i + self.stream_pieces])
        return text


@pytest.fixture
def store(tmp_path):
    return EditorStore(tmp_path / "ed")


@pytest.fixture
def s(store):
    return store.create(messy(), "messy.dxf", fingerprint="messyfp01")


@pytest.fixture
def ws(store):
    return store.create(sample.build(), "sample.dxf", fingerprint="samplefp01")


# ── built-in answers ────────────────────────────────────────────────────────


@pytest.mark.parametrize("q,needle,kind", [
    ("Explain this drawing", "objects on", "explain"),
    ("health check", "Health score", "health"),
    ("what's wrong with it?", "Health score", "health"),
    ("quantity takeoff", "kinds of block", "table"),
    ("rooms", "OFFICE 80 m²", "table"),
    ("door schedule", "3 placements", "table"),
    ("BOM", "parts in", "table"),
    ("check the aisles", "rack bays", "table"),
])
def test_built_in_answers(s, q, needle, kind):
    r = agent.run(s, q, [], None)
    assert r.source == "local" and needle in r.reply and r.data["kind"] == kind, r.reply
    assert r.suggestions


def test_table_answers_offer_csv(s):
    r = agent.run(s, "door schedule", [], None)
    assert r.data["columns"][:3] == ["Block", "NUM", "WIDTH"] and r.data["download"].endswith("format=csv")
    assert "block=*door*" in r.data["download"]


def test_inspect_needs_and_uses_the_selection(s):
    assert "Nothing is selected" in agent.run(s, "what is this?", [], None).reply
    door = s.doc.modelspace().query("INSERT")[0].dxf.handle
    r = agent.run(s, "What is this?", [door], None)
    assert "block DOOR" in r.reply and r.data["items"][0]["attributes"]["NUM"] == "D1"


def test_area_questions(s):
    assert "Box an area first" in agent.run(s, "what's in this area?", [], None).reply
    r = agent.run(s, "what's in this area?", [], None, area=[0, 0, 10000, 8000])
    assert "OFFICE" in r.reply and r.data["summary"]["count"] > 0


def test_area_is_given_to_the_model(s):
    m = FakeModel({"reply": "ok", "queries": [], "ops": []})
    agent.run(s, "make everything here tidier", [], m, area=[0, 0, 10000, 8000])
    system = m.calls[0][0]["content"]
    assert "boxed an AREA" in system and "OFFICE" in system


# ── built-in plans and commands ─────────────────────────────────────────────


def test_clean_up_is_a_multi_step_plan(s):
    r = agent.run(s, "clean up the drawing", [], None)
    p = r.proposal
    assert p and len(p["steps"]) >= 4 and p["why"]
    titles = " ".join(st["title"] for st in p["steps"])
    assert "duplicate" in titles and "unused layer" in titles
    # accept only the first two steps
    s.accept(p["id"], steps=[0, 1])
    assert s.rev == 1 and len(s.log[-1]["summaries"]) >= 2


def test_partial_accept_reapplies_only_chosen_steps(s):
    p = agent.run(s, "clean up the drawing", [], None).proposal
    titles = [st["title"] for st in p["steps"]]
    keep = [i for i, t in enumerate(titles) if "unused layer" in t]
    s.accept(p["id"], steps=keep)
    h = {f["id"] for f in s.health()["findings"]}
    assert "unused-layers" not in h and "duplicates" in h  # duplicates step was left out


def test_standardize_layers_plan(s):
    r = agent.run(s, "standardize layers", [], None)
    assert "walls → A-WALL" in r.reply and r.proposal["steps"][0]["ops"][0]["op"] == "map_layers"


def test_spelling_plan(tmp_path):
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("RECIEVING")
    s = EditorStore(tmp_path / "e").create(doc, "x.dxf")
    r = agent.run(s, "check spelling", [], None)
    assert "recieving → receiving" in r.reply and r.proposal


@pytest.mark.parametrize("cmd,op", [
    ("purge unused blocks", "purge_unused_blocks"),
    ("purge", "purge_unused_layers"),
    ("delete duplicates", "delete_duplicates"),
    ("overkill", "delete_duplicates"),
    ("flatten the drawing", "flatten"),
    ("set units to m", "set_units"),
    ("convert to m", "set_units"),
    ("replace shx fonts with arial", "replace_fonts"),
    ("standardize text heights", "normalize_text_heights"),
    ("revision cloud around layer walls rev B", "revision_cloud"),
    ("renumber labels starting with D", "renumber"),
    ("explode layer door_tags", "explode"),
    ("rename block DOOR to A-DOOR", "rename_block"),
    ("copy layer walls 5 m north", "copy"),
    ("rotate layer walls 90 degrees clockwise", "rotate"),
])
def test_new_built_in_commands_parse(cmd, op):
    parsed = agent.local_ops(cmd)
    assert parsed and parsed[0][0]["op"] == op, cmd


def test_rotate_clockwise_is_negative():
    assert agent.local_ops("rotate the selection 90 degrees clockwise")[0][0]["angle"] == -90


def test_script_of_lines_becomes_steps(s):
    r = agent.run(s, "purge unused layers\ndelete duplicates\nflatten", [], None)
    p = r.proposal
    assert [st["title"] for st in p["steps"]] == ["purge unused layers", "delete duplicates", "flatten"]
    assert "Here is your 3-step script" in r.reply


def test_script_of_json_ops(s):
    r = agent.run(s, json.dumps([{"op": "purge_unused_layers"}, {"op": "purge_unused_blocks"}]), [], None)
    assert r.proposal and len(r.proposal["summaries"]) == 2
    bad = agent.run(s, json.dumps([{"op": "explode_everything"}]), [], None)
    assert bad.error and "unknown op" in bad.reply


def test_unparseable_script_falls_through(s):
    assert agent.script_steps("purge unused layers\nmake it pretty") is None
    assert agent.script_steps("one line only") is None


# ── model: steps, why, streaming ────────────────────────────────────────────


def test_model_can_propose_named_steps_with_a_why(s):
    m = FakeModel({"reply": "Two parts.", "why": "Separate steps so you can choose.", "queries": [], "ops": [],
                   "steps": [{"title": "Remove clutter", "ops": [{"op": "delete_duplicates"}]},
                             {"title": "Tidy layers", "ops": [{"op": "purge_unused_layers"}]}]})
    r = agent.run(s, "remove clutter and tidy layers", [], m)
    p = r.proposal
    assert [st["title"] for st in p["steps"]] == ["Remove clutter", "Tidy layers"] and p["why"] == "Separate steps so you can choose."
    assert "steps" in m.calls[0][0]["content"] and '"why"' in m.calls[0][0]["content"]


def test_model_queries_new_kinds(s):
    m = FakeModel({"reply": "x", "queries": [{"q": "health"}, {"q": "rooms"}, {"q": "takeoff"}, {"q": "warehouse"}], "ops": []},
                  {"reply": "The office is 80 m².", "queries": [], "ops": []})
    r = agent.run(s, "how big is the office and is anything wrong?", [], m)
    results = m.calls[1][-1]["content"]
    assert '"score"' in results and "OFFICE" in results and '"blocks"' in results and '"bays"' in results
    assert r.reply == "The office is 80 m²."


def test_partial_reply_extraction():
    assert agent.partial_reply('{"reply": "Hel') == "Hel"
    assert agent.partial_reply('{"reply":"a\\nb \\"c\\" \\u00e9') == 'a\nb "c" é'
    assert agent.partial_reply('{"reply":"done","ops":[]}') == "done"
    assert agent.partial_reply('{"queries":[') is None
    assert agent.partial_reply('{"reply":"half \\u00') == "half "


def test_streaming_emits_status_and_reply_pieces(s):
    m = FakeModel({"reply": "Here is a long answer about the racks.", "queries": [], "ops": []}, stream_pieces=7)
    events = []
    r = agent.run(s, "tell me something about the racks", [], m, emit=lambda k, d: events.append((k, d)))
    kinds = [k for k, _ in events]
    assert "status" in kinds and "reply" in kinds
    replies = [d for k, d in events if k == "reply"]
    assert replies[-1] == "Here is a long answer about the racks." and len(replies) > 2
    assert any("Thinking" in d for k, d in events if k == "status")
    assert r.reply == "Here is a long answer about the racks."


# ── suggestions ─────────────────────────────────────────────────────────────


def test_suggestions_follow_the_drawing(s):
    sug = agent.suggestions(s, [])
    assert "Purge unused layers" in sug and len(sug) <= 5
    assert agent.suggestions(s, ["X"])[0] == "What is this?"


# ── memory ──────────────────────────────────────────────────────────────────


def test_memory_across_sessions(store):
    first = store.create(messy(), "messy.dxf", fingerprint="memfp001")
    assert first.previous is None
    p = first.stage([{"op": "purge_unused_layers"}], [], "local", "purge unused layers")
    first.accept(p.id)
    first.remember("user", "is the office big enough?")
    first.remember("assistant", "It is 80 m².")
    again = store.create(messy(), "messy.dxf", fingerprint="memfp001")
    prev = again.previous
    assert prev["visits"] == 1 and prev["changes"][-1]["prompt"] == "purge unused layers"
    assert prev["chat"][-1]["user"] == "is the office big enough?"
    m = FakeModel({"reply": "ok", "queries": [], "ops": []})
    agent.run(again, "what did we do last time?", [], m)
    assert "EARLIER SESSIONS ON THIS DRAWING" in m.calls[0][0]["content"] and "purge unused layers" in m.calls[0][0]["content"]


def test_edited_copy_shares_memory(store):
    from app.editor.memory import file_fingerprint

    s1 = store.create(messy(), "messy.dxf", fingerprint="origfp01")
    p = s1.stage([{"op": "purge_unused_layers"}], [], "local", "purge")
    s1.accept(p.id)
    path = s1.write_current()
    reopened = store.create(ezdxf.readfile(path), "messy_edited.dxf", fingerprint=file_fingerprint(path))
    assert reopened.previous and reopened.previous["changes"][-1]["prompt"] == "purge"


def test_memory_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKDATE_EDITOR_MEMORY", "off")
    st = EditorStore(tmp_path / "ed")
    st.create(messy(), "a.dxf", fingerprint="offfp001")
    assert st.create(messy(), "a.dxf", fingerprint="offfp001").previous is None


# ── HTTP ────────────────────────────────────────────────────────────────────


def parse_sse(text):
    events = []
    for block in text.split("\n\n"):
        kind = data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if kind:
            events.append((kind, data))
    return events


def test_chat_stream_endpoint(client):
    sid = client.post("/api/editor/sessions/sample").json()["id"]
    client.app.state.editor_llm = FakeModel({"reply": "Streaming works fine.", "queries": [], "ops": []}, stream_pieces=5)
    try:
        r = client.post(f"/api/editor/sessions/{sid}/chat/stream", json={"message": "say something nice about this layout"})
    finally:
        del client.app.state.editor_llm
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    kinds = [k for k, _ in events]
    assert kinds[-1] == "result" and "reply" in kinds and "status" in kinds
    assert events[-1][1]["reply"] == "Streaming works fine." and events[-1][1]["source"] == "model"


def test_chat_stream_local_and_data(client):
    sid = client.post("/api/editor/sessions/sample").json()["id"]
    events = parse_sse(client.post(f"/api/editor/sessions/{sid}/chat/stream", json={"message": "door schedule"}).text)
    assert events[-1][0] == "result"
    r = client.post(f"/api/editor/sessions/{sid}/chat", json={"message": "check the aisles"}).json()
    assert r["data"]["kind"] == "table" and r["suggestions"]


def test_accept_chosen_steps_over_http(client):
    sid = client.post("/api/editor/sessions/sample").json()["id"]
    p = client.post(f"/api/editor/sessions/{sid}/chat", json={"message": "purge unused layers\nreplace \"REV A\" with \"REV B\""}).json()["proposal"]
    assert len(p["steps"]) == 2
    r = client.post(f"/api/editor/sessions/{sid}/proposals/{p['id']}/accept", json={"steps": [1]}).json()
    assert r["proposal"]["summaries"] == ["Replace “REV A” with “REV B” in 1 text item (1 occurrence)"]
    layers = {l["name"] for l in r["summary"]["digest"]["layers"]}
    assert "XREF-GHOST" in layers  # the purge step was left out
    assert r["suggestions"]


def test_memory_endpoints(client):
    sid = client.post("/api/editor/sessions/sample").json()["id"]
    p = client.post(f"/api/editor/sessions/{sid}/chat", json={"message": "purge unused layers"}).json()["proposal"]
    client.post(f"/api/editor/sessions/{sid}/proposals/{p['id']}/accept")
    sid2 = client.post("/api/editor/sessions/sample").json()
    assert sid2["memory"]["visits"] >= 1 and sid2["memory"]["changes"]
    mem = client.get(f"/api/editor/sessions/{sid2['id']}/memory").json()
    assert mem["remembered"] and mem["changes"][-1]["prompt"] == "purge unused layers"
    assert client.delete(f"/api/editor/sessions/{sid2['id']}/memory").status_code == 204
    assert client.get(f"/api/editor/sessions/{sid2['id']}/memory").json() == {"remembered": False}


def test_empty_plan_is_refused(s):
    s.accept(s.stage([{"op": "purge_unused_layers"}], [], "x").id)
    with pytest.raises(Exception, match="nothing to change"):
        s.stage([{"op": "purge_unused_layers", "optional": True}], [], "x")
