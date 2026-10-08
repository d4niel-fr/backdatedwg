"""Review links (comments, approval), live editing links, and presence events."""

import asyncio
import json
import threading
import time

import pytest

from app.editor import share


def open_sample(client):
    return client.post("/api/editor/sessions/sample").json()["id"]


# ── review links ────────────────────────────────────────────────────────────


def test_viewer_link_freezes_the_revision(client):
    sid = open_sample(client)
    r = client.post(f"/api/editor/sessions/{sid}/shares", json={"role": "viewer", "label": "Client"})
    assert r.status_code == 201
    link = r.json()
    assert link["link"] == f"editor.html?share={link['token']}" and link["rev"] == 0 and link["allowDownload"]
    geo_before = client.get(f"/api/editor/shares/{link['token']}/geometry").json()
    # the owner keeps editing; the link still shows revision 0
    p = client.post(f"/api/editor/sessions/{sid}/chat", json={"message": "move layer S-RACK 5 m up"}).json()["proposal"]
    client.post(f"/api/editor/sessions/{sid}/proposals/{p['id']}/accept")
    assert client.get(f"/api/editor/shares/{link['token']}/geometry").json() == geo_before
    meta = client.get(f"/api/editor/shares/{link['token']}").json()
    assert meta["role"] == "viewer" and meta["label"] == "Client" and "session" not in meta
    dxf = client.get(f"/api/editor/shares/{link['token']}/drawing.dxf")
    assert dxf.status_code == 200 and "_rev0.dxf" in dxf.headers["content-disposition"]
    assert client.get(f"/api/editor/shares/{link['token']}/export.pdf").content.startswith(b"%PDF")


def test_comments_and_owner_replies(client):
    sid = open_sample(client)
    token = client.post(f"/api/editor/sessions/{sid}/shares", json={"role": "viewer"}).json()["token"]
    c = client.post(f"/api/editor/shares/{token}/comments", json={"author": "Sam <script>", "text": "Move the dock?", "x": 1000, "y": 2000}).json()
    assert c["author"] == "Sam script" and c["x"] == 1000 and not c["owner"]
    reply = client.post(f"/api/editor/sessions/{sid}/shares/{token}/comments", json={"text": "Will do", "reply_to": c["id"]}).json()
    assert reply["owner"] and reply["author"] == "Owner" and reply["replyTo"] == c["id"]
    all_c = client.get(f"/api/editor/sessions/{sid}/comments").json()["comments"]
    assert [x["text"] for x in all_c] == ["Move the dock?", "Will do"] and all_c[0]["token"] == token
    client.post(f"/api/editor/sessions/{sid}/shares/{token}/comments/{c['id']}/resolve", json={"resolved": True})
    assert client.get(f"/api/editor/shares/{token}/comments").json()["comments"][0]["resolved"] is True
    listed = client.get(f"/api/editor/sessions/{sid}/shares").json()["shares"][0]
    assert listed["comments"] == 2 and listed["open"] == 1
    assert client.post(f"/api/editor/shares/{token}/comments", json={"text": "x", "reply_to": "nope"}).status_code == 404
    assert client.post(f"/api/editor/shares/{token}/comments", json={"text": ""}).status_code == 422


def test_approval_only_for_approvers(client):
    sid = open_sample(client)
    viewer = client.post(f"/api/editor/sessions/{sid}/shares", json={"role": "viewer"}).json()["token"]
    approver = client.post(f"/api/editor/sessions/{sid}/shares", json={"role": "approver", "label": "Acme"}).json()["token"]
    assert client.post(f"/api/editor/shares/{viewer}/decision", json={"decision": "approved"}).status_code == 403
    assert client.post(f"/api/editor/shares/{approver}/decision", json={"decision": "maybe"}).status_code == 400
    d = client.post(f"/api/editor/shares/{approver}/decision", json={"decision": "changes_requested", "author": "Jo", "note": "Wider aisles"}).json()
    assert d["decision"] == "changes_requested" and d["rev"] == 0
    d2 = client.post(f"/api/editor/shares/{approver}/decision", json={"decision": "approved", "author": "Jo"}).json()
    meta = client.get(f"/api/editor/shares/{approver}").json()
    assert meta["decision"]["decision"] == "approved" and len(meta["decisions"]) == 2 and d2["author"] == "Jo"
    listed = {r["token"]: r for r in client.get(f"/api/editor/sessions/{sid}/shares").json()["shares"]}
    assert listed[approver]["decision"]["decision"] == "approved"


def test_downloads_can_be_disabled_and_links_revoked(client):
    sid = open_sample(client)
    token = client.post(f"/api/editor/sessions/{sid}/shares", json={"role": "viewer", "allow_download": False}).json()["token"]
    assert client.get(f"/api/editor/shares/{token}/drawing.dxf").status_code == 403
    other = open_sample(client)
    assert client.delete(f"/api/editor/sessions/{other}/shares/{token}").status_code == 404  # not this drawing's link
    assert client.delete(f"/api/editor/sessions/{sid}/shares/{token}").status_code == 204
    assert client.get(f"/api/editor/shares/{token}").status_code == 404


def test_bad_and_expired_links(client, monkeypatch):
    assert client.get("/api/editor/shares/../../etc").status_code == 404
    assert client.get("/api/editor/shares/short").status_code == 404
    sid = open_sample(client)
    assert client.post(f"/api/editor/sessions/{sid}/shares", json={"role": "boss"}).status_code == 400
    token = client.post(f"/api/editor/sessions/{sid}/shares", json={"role": "viewer", "days": 1}).json()["token"]
    real = time.time
    monkeypatch.setattr(share.time, "time", lambda: real() + 3 * 86400)
    assert client.get(f"/api/editor/shares/{token}").status_code == 410


def test_review_links_outlive_the_session(client):
    sid = open_sample(client)
    token = client.post(f"/api/editor/sessions/{sid}/shares", json={"role": "approver"}).json()["token"]
    client.delete(f"/api/editor/sessions/{sid}")
    assert client.get(f"/api/editor/shares/{token}/geometry").status_code == 200
    assert client.post(f"/api/editor/shares/{token}/decision", json={"decision": "approved"}).status_code == 200


def test_editor_link_joins_the_live_session(client):
    sid = open_sample(client)
    link = client.post(f"/api/editor/sessions/{sid}/shares", json={"role": "editor"}).json()
    assert link["link"] == f"editor.html?join={link['token']}"
    assert client.get(f"/api/editor/join/{link['token']}").json() == {"sessionId": sid, "name": "Warehouse B (sample).dxf"}
    assert client.get("/api/editor/join/unknown-token-1234567").status_code == 404
    client.delete(f"/api/editor/sessions/{sid}")
    assert client.get(f"/api/editor/join/{link['token']}").status_code == 404


# ── live events ─────────────────────────────────────────────────────────────


def test_event_bus_fan_out():
    async def scenario():
        bus = share.EventBus()
        a = bus.join("aaaa", {"name": "A"})
        b = bus.join("bbbb", {"name": "B"})
        assert (await a.get())[0] == "presence"  # B joined
        bus.publish("cursor", {"x": 1}, exclude="aaaa")
        assert await b.get() == ("cursor", {"x": 1})
        assert a.empty()
        # publishing from another thread (as sync endpoints do) is safe
        t = threading.Thread(target=lambda: bus.publish("changed", {"rev": 2}))
        t.start()
        t.join()
        assert await asyncio.wait_for(a.get(), 1) == ("changed", {"rev": 2})
        bus.leave("bbbb")
        assert (await a.get())[1] == {"client": "bbbb", "left": True}
        assert [p["client"] for p in bus.people()] == ["aaaa"]

    asyncio.run(scenario())


def test_listener_limit():
    async def scenario():
        bus = share.EventBus()
        for i in range(share.MAX_LISTENERS):
            bus.join(f"client{i:03d}", {})
        with pytest.raises(share.ShareError):
            bus.join("one-too-many", {})

    asyncio.run(scenario())


def test_stream_yields_hello_and_events():
    async def scenario():
        bus = share.EventBus()
        gen = share.stream(bus, "watcher1", {"name": "W"}, {"rev": 3})
        hello = await gen.__anext__()
        assert hello.startswith("event: hello") and '"rev":3' in hello
        bus.publish("changed", {"what": "accepted"})
        nxt = await gen.__anext__()
        assert nxt.startswith("event: changed") and "accepted" in nxt
        await gen.aclose()
        assert bus.people() == []

    asyncio.run(scenario())


def test_mutations_are_published(client):
    sid = open_sample(client)
    s = client.app.state.editor.get(sid)
    seen = []
    s.bus.publish = lambda kind, data, exclude=None: seen.append((kind, data, exclude))
    p = client.post(f"/api/editor/sessions/{sid}/chat", json={"message": "purge unused layers"}, headers={"X-Client-Id": "tab-1", "X-Client-Name": "Dan"}).json()["proposal"]
    client.post(f"/api/editor/sessions/{sid}/proposals/{p['id']}/accept", headers={"X-Client-Id": "tab-1"})
    client.post(f"/api/editor/sessions/{sid}/undo")
    client.post(f"/api/editor/sessions/{sid}/presence", json={"client": "tab-2", "name": "Ann", "x": 5, "y": 6})
    kinds = [k for k, _d, _e in seen]
    assert kinds == ["chat", "changed", "changed", "cursor"]
    assert seen[0][1]["by"] == "Dan" and seen[0][2] == "tab-1" and seen[1][1]["what"] == "accepted" and seen[2][1]["what"] == "undo"
    assert seen[3][1]["name"] == "Ann" and seen[3][2] == "tab-2"


def test_comment_notifies_the_owner(client):
    sid = open_sample(client)
    s = client.app.state.editor.get(sid)
    seen = []
    s.bus.publish = lambda kind, data, exclude=None: seen.append((kind, data))
    token = client.post(f"/api/editor/sessions/{sid}/shares", json={"role": "approver", "label": "Acme"}).json()["token"]
    client.post(f"/api/editor/shares/{token}/comments", json={"text": "Looks good"})
    client.post(f"/api/editor/shares/{token}/decision", json={"decision": "approved"})
    assert [k for k, _ in seen] == ["comment", "decision"] and seen[0][1]["label"] == "Acme"


def test_people_endpoint(client):
    sid = open_sample(client)
    assert client.get(f"/api/editor/sessions/{sid}/people").json() == {"people": []}
