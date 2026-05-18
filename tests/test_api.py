"""Integration tests for the HTTP API."""

from fastapi.testclient import TestClient

from hivechat.api import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_create_and_list_room():
    r = client.post("/rooms", json={"topic": "api test"})
    assert r.status_code == 201
    code = r.json()["code"]
    assert len(code) == 9

    listing = client.get("/rooms").json()
    assert any(room["code"] == code for room in listing)


def test_join_room():
    code = client.post("/rooms", json={}).json()["code"]
    r = client.post(f"/rooms/{code}/join", json={"name": "alice", "role": "orchestrator"})
    assert r.status_code == 200
    data = r.json()
    assert data["code"] == code
    assert any(p["name"] == "alice" for p in data["participants"])


def test_join_nonexistent_room():
    r = client.post("/rooms/notexist1/join", json={"name": "alice"})
    assert r.status_code == 404


def test_post_and_get_messages():
    code = client.post("/rooms", json={}).json()["code"]
    client.post(f"/rooms/{code}/join", json={"name": "alice", "role": "coder"})

    r = client.post(f"/rooms/{code}/messages", json={"author": "alice", "text": "Hello!"})
    assert r.status_code == 201
    msg = r.json()
    assert msg["author"] == "alice"
    assert msg["role"] == "coder"

    msgs = client.get(f"/rooms/{code}/messages").json()
    assert len(msgs) == 1
    assert msgs[0]["text"] == "Hello!"


def test_messages_since_id():
    code = client.post("/rooms", json={}).json()["code"]
    client.post(f"/rooms/{code}/join", json={"name": "bot"})
    m1 = client.post(f"/rooms/{code}/messages", json={"author": "bot", "text": "a"}).json()
    client.post(f"/rooms/{code}/messages", json={"author": "bot", "text": "b"})

    result = client.get(f"/rooms/{code}/messages?since_id={m1['id']}").json()
    assert len(result) == 1
    assert result[0]["text"] == "b"


def test_status_endpoint():
    code = client.post("/rooms", json={}).json()["code"]
    client.post(f"/rooms/{code}/join", json={"name": "coder"})
    r = client.post(
        f"/rooms/{code}/status",
        json={"agent": "coder", "status": "thinking", "action": "Analyzing code"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_tool_call_endpoint():
    code = client.post("/rooms", json={}).json()["code"]
    client.post(f"/rooms/{code}/join", json={"name": "coder"})
    r = client.post(
        f"/rooms/{code}/tool-call",
        json={
            "agent": "coder",
            "tool": "Read",
            "args_summary": "auth.py",
            "result_summary": "847 lines",
        },
    )
    assert r.status_code == 200


def test_progress_endpoint():
    code = client.post("/rooms", json={}).json()["code"]
    client.post(f"/rooms/{code}/join", json={"name": "coder"})
    r = client.post(
        f"/rooms/{code}/progress",
        json={"agent": "coder", "step_index": 1, "done": True},
    )
    assert r.status_code == 200


def test_close_room():
    code = client.post("/rooms", json={"topic": "closing test"}).json()["code"]
    client.post(f"/rooms/{code}/join", json={"name": "alice"})
    client.post(f"/rooms/{code}/messages", json={"author": "alice", "text": "done"})

    r = client.delete(f"/rooms/{code}")
    assert r.status_code == 200
    assert "transcript_path" in r.json()

    # Room should no longer appear in listing
    listing = client.get("/rooms").json()
    assert all(room["code"] != code for room in listing)


def test_get_room_detail():
    code = client.post("/rooms", json={"topic": "detail test"}).json()["code"]
    client.post(f"/rooms/{code}/join", json={"name": "alice", "role": "orchestrator"})
    client.post(f"/rooms/{code}/messages", json={"author": "alice", "text": "hi"})

    r = client.get(f"/rooms/{code}")
    assert r.status_code == 200
    data = r.json()
    assert data["topic"] == "detail test"
    assert len(data["recent_messages"]) == 1
    assert len(data["participants"]) >= 1
