"""Unit tests for the rooms module."""

from hivechat import rooms


def test_create_room():
    result = rooms.create_room("test topic")
    assert len(result["code"]) == 9
    assert result["topic"] == "test topic"


def test_create_room_generates_unique_codes():
    codes = {rooms.create_room()["code"] for _ in range(20)}
    assert len(codes) == 20


def test_join_room():
    code = rooms.create_room("collab")["code"]
    result = rooms.join_room(code, "alice", "orchestrator")
    assert result is not None
    assert result["code"] == code
    assert result["history"] == []
    assert any(p["name"] == "alice" for p in result["participants"])


def test_join_nonexistent_room():
    assert rooms.join_room("notaroom1", "alice") is None


def test_post_and_read_messages():
    code = rooms.create_room()["code"]
    rooms.join_room(code, "alice", "orchestrator")
    rooms.join_room(code, "bob", "coder")

    msg = rooms.post_message(code, "alice", "Hello team!")
    assert msg is not None
    assert msg["author"] == "alice"
    assert msg["role"] == "orchestrator"
    assert msg["text"] == "Hello team!"

    msgs = rooms.read_messages(code)
    assert len(msgs) == 1
    assert msgs[0]["text"] == "Hello team!"


def test_read_messages_since_id():
    code = rooms.create_room()["code"]
    rooms.join_room(code, "alice", "orchestrator")

    m1 = rooms.post_message(code, "alice", "first")
    rooms.post_message(code, "alice", "second")
    rooms.post_message(code, "alice", "third")

    result = rooms.read_messages(code, since_id=m1["id"])
    assert len(result) == 2
    assert result[0]["text"] == "second"
    assert result[1]["text"] == "third"


def test_post_to_closed_room():
    code = rooms.create_room()["code"]
    rooms.join_room(code, "alice")
    rooms.close_room(code)
    assert rooms.post_message(code, "alice", "late message") is None


def test_report_status_emits_event():
    code = rooms.create_room()["code"]
    rooms.join_room(code, "coder", "coder")
    ok = rooms.report_status(code, "coder", "thinking", "Planning the approach")
    assert ok is True
    events = rooms.get_events(code)
    status_events = [e for e in events if e["type"] == "status_change"]
    assert len(status_events) == 1
    assert status_events[0]["data"]["status"] == "thinking"


def test_log_tool_call_emits_event():
    code = rooms.create_room()["code"]
    rooms.join_room(code, "coder", "coder")
    rooms.log_tool_call(code, "coder", "Read", "auth.py", "847 lines")
    events = rooms.get_events(code)
    tool_events = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0]["data"]["tool"] == "Read"
    assert tool_events[0]["data"]["args"] == "auth.py"


def test_update_progress():
    code = rooms.create_room()["code"]
    rooms.join_room(code, "coder")
    rooms.update_progress(code, "coder", 2, done=True)
    events = rooms.get_events(code)
    progress_events = [e for e in events if e["type"] == "progress"]
    assert len(progress_events) == 1
    assert progress_events[0]["data"]["step"] == 2
    assert progress_events[0]["data"]["done"] is True


def test_get_events_since_id():
    code = rooms.create_room()["code"]
    rooms.join_room(code, "alice", "orchestrator")
    rooms.post_message(code, "alice", "msg1")
    rooms.post_message(code, "alice", "msg2")

    all_events = rooms.get_events(code)
    first_id = all_events[0]["id"]
    later = rooms.get_events(code, since_id=first_id)
    assert len(later) == len(all_events) - 1


def test_list_rooms():
    code1 = rooms.create_room("room one")["code"]
    code2 = rooms.create_room("room two")["code"]
    listing = rooms.list_rooms()
    codes = [r["code"] for r in listing]
    assert code1 in codes
    assert code2 in codes


def test_close_room_writes_transcript(tmp_path):
    code = rooms.create_room("test session")["code"]
    rooms.join_room(code, "alice", "orchestrator")
    rooms.post_message(code, "alice", "Let's begin")
    result = rooms.close_room(code)
    assert result is not None
    transcript_path = result["transcript_path"]
    content = open(transcript_path).read()
    assert "Let's begin" in content
    assert "hive-session" in content


def test_closed_room_not_in_listing():
    code = rooms.create_room()["code"]
    rooms.close_room(code)
    listing = rooms.list_rooms()
    assert all(r["code"] != code for r in listing)


def test_write_checkpoint(tmp_path):
    code = rooms.create_room("checkpoint test")["code"]
    rooms.join_room(code, "coder", "coder")
    result = rooms.write_checkpoint(
        code,
        agent="coder",
        completed_steps=["Analyzed auth.py", "Wrote validation logic"],
        next_step="Write tests for validate_token()",
        context_files=["src/auth.py", "tests/test_auth.py"],
        notes="Token expiry edge case needs attention",
    )
    assert result is not None
    assert result["next_step"] == "Write tests for validate_token()"
    content = open(result["path"]).read()
    assert "agent-checkpoint" in content
    assert "Analyzed auth.py" in content
    assert "Write tests for validate_token()" in content
    assert "src/auth.py" in content

    events = rooms.get_events(code)
    checkpoint_events = [e for e in events if e["type"] == "checkpoint"]
    assert len(checkpoint_events) == 1
    assert checkpoint_events[0]["data"]["next"] == "Write tests for validate_token()"
