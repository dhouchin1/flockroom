"""Room state management with SQLite persistence.

All writes use WAL mode so multiple processes (MCP servers + HTTP server)
can safely share the same database file.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import string
import time
from pathlib import Path


def _db_path() -> Path:
    override = os.environ.get("HIVECHAT_DB")
    if override:
        return Path(override)
    return Path.home() / ".config" / "hivechat" / "hive.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS rooms (
        code       TEXT PRIMARY KEY,
        topic      TEXT DEFAULT '',
        created_at REAL NOT NULL,
        closed_at  REAL
    );
    CREATE TABLE IF NOT EXISTS participants (
        room_code TEXT NOT NULL,
        name      TEXT NOT NULL,
        role      TEXT NOT NULL DEFAULT 'assistant',
        joined_at REAL NOT NULL,
        PRIMARY KEY (room_code, name)
    );
    CREATE TABLE IF NOT EXISTS messages (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        room_code TEXT NOT NULL,
        author    TEXT NOT NULL,
        role      TEXT NOT NULL DEFAULT 'assistant',
        text      TEXT NOT NULL,
        ts        REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS room_events (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        room_code TEXT NOT NULL,
        type      TEXT NOT NULL,
        agent     TEXT NOT NULL,
        data      TEXT NOT NULL,
        ts        REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_msg_room   ON messages(room_code, id);
    CREATE INDEX IF NOT EXISTS idx_evt_room   ON room_events(room_code, id);
    """)
    conn.commit()


def _make_code() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(9))


def _emit(conn: sqlite3.Connection, code: str, type_: str, agent: str, data: dict) -> None:
    conn.execute(
        "INSERT INTO room_events (room_code, type, agent, data, ts) VALUES (?,?,?,?,?)",
        (code, type_, agent, json.dumps(data), time.time()),
    )


# ── public API ────────────────────────────────────────────────────────────────


def create_room(topic: str = "") -> dict:
    code = _make_code()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO rooms (code, topic, created_at) VALUES (?,?,?)",
            (code, topic, time.time()),
        )
    return {"code": code, "topic": topic, "created_at": time.time()}


def join_room(code: str, name: str, role: str = "assistant") -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM rooms WHERE code=? AND closed_at IS NULL", (code,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "INSERT OR REPLACE INTO participants"
            " (room_code, name, role, joined_at) VALUES (?,?,?,?)",
            (code, name, role, time.time()),
        )
        msgs = conn.execute(
            "SELECT * FROM messages WHERE room_code=? ORDER BY id", (code,)
        ).fetchall()
        parts = conn.execute("SELECT * FROM participants WHERE room_code=?", (code,)).fetchall()
        _emit(conn, code, "participant_join", name, {"role": role})
    return {
        "code": code,
        "topic": row["topic"],
        "history": [dict(m) for m in msgs],
        "participants": [dict(p) for p in parts],
    }


def post_message(code: str, author: str, text: str) -> dict | None:
    with _connect() as conn:
        if not conn.execute(
            "SELECT 1 FROM rooms WHERE code=? AND closed_at IS NULL", (code,)
        ).fetchone():
            return None
        p = conn.execute(
            "SELECT role FROM participants WHERE room_code=? AND name=?", (code, author)
        ).fetchone()
        role = p["role"] if p else "assistant"
        ts = time.time()
        cur = conn.execute(
            "INSERT INTO messages (room_code, author, role, text, ts) VALUES (?,?,?,?,?)",
            (code, author, role, text, ts),
        )
        msg_id = cur.lastrowid
        _emit(conn, code, "message", author, {"id": msg_id, "role": role, "text": text})
    return {"id": msg_id, "author": author, "role": role, "text": text, "ts": ts}


def read_messages(code: str, since_id: int = 0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE room_code=? AND id>? ORDER BY id",
            (code, since_id),
        ).fetchall()
    return [dict(r) for r in rows]


def report_status(code: str, agent: str, status: str, action: str = "") -> bool:
    with _connect() as conn:
        if not conn.execute(
            "SELECT 1 FROM rooms WHERE code=? AND closed_at IS NULL", (code,)
        ).fetchone():
            return False
        _emit(conn, code, "status_change", agent, {"status": status, "action": action})
    return True


def log_tool_call(
    code: str, agent: str, tool: str, args_summary: str, result_summary: str = ""
) -> bool:
    with _connect() as conn:
        if not conn.execute(
            "SELECT 1 FROM rooms WHERE code=? AND closed_at IS NULL", (code,)
        ).fetchone():
            return False
        _emit(
            conn,
            code,
            "tool_call",
            agent,
            {"tool": tool, "args_summary": args_summary, "result_summary": result_summary},
        )
    return True


def update_progress(code: str, agent: str, step_index: int, done: bool) -> bool:
    with _connect() as conn:
        if not conn.execute(
            "SELECT 1 FROM rooms WHERE code=? AND closed_at IS NULL", (code,)
        ).fetchone():
            return False
        _emit(conn, code, "progress", agent, {"step_index": step_index, "done": done})
    return True


def list_rooms() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("""
            SELECT r.code, r.topic, r.created_at,
                   COUNT(DISTINCT p.name)  AS participant_count,
                   COUNT(DISTINCT m.id)    AS message_count
            FROM   rooms r
            LEFT JOIN participants p ON p.room_code = r.code
            LEFT JOIN messages     m ON m.room_code = r.code
            WHERE  r.closed_at IS NULL
            GROUP BY r.code
            ORDER BY r.created_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_room(code: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM rooms WHERE code=?", (code,)).fetchone()
        if not row:
            return None
        parts = conn.execute("SELECT * FROM participants WHERE room_code=?", (code,)).fetchall()
        msgs = conn.execute(
            "SELECT * FROM messages WHERE room_code=? ORDER BY id DESC LIMIT 50", (code,)
        ).fetchall()
    return {
        **dict(row),
        "participants": [dict(p) for p in parts],
        "recent_messages": [dict(m) for m in reversed(msgs)],
    }


def get_events(code: str, since_id: int = 0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM room_events WHERE room_code=? AND id>? ORDER BY id",
            (code, since_id),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d["data"])
        result.append(d)
    return result


def write_checkpoint(
    code: str,
    agent: str,
    completed_steps: list[str],
    next_step: str,
    context_files: list[str] | None = None,
    notes: str = "",
) -> dict | None:
    """Write a structured checkpoint for pause/resume support."""
    import datetime

    with _connect() as conn:
        row = conn.execute("SELECT * FROM rooms WHERE code=?", (code,)).fetchone()
        if not row:
            return None
        _emit(
            conn,
            code,
            "checkpoint",
            agent,
            {
                "completed": completed_steps,
                "next": next_step,
                "files": context_files or [],
            },
        )

    vault_dir = os.environ.get("HIVECHAT_VAULT_DIR")
    if vault_dir:
        out_dir = Path(vault_dir)
    else:
        out_dir = _db_path().parent / "checkpoints"
    out_dir.mkdir(parents=True, exist_ok=True)

    date = datetime.datetime.now().strftime("%Y-%m-%d")
    path = out_dir / f"{date}-checkpoint-{code}-{agent.lower().replace(' ', '-')}.md"

    lines = [
        "---",
        "type: agent-checkpoint",
        f"room_code: {code}",
        f"topic: {row['topic']}",
        f"agent_role: {agent}",
        f"date: {date}",
        "completed_steps:",
    ]
    for s in completed_steps:
        lines.append(f'  - "{s}"')
    lines.append(f'next_step: "{next_step}"')
    if context_files:
        lines.append("context_files:")
        for f in context_files:
            lines.append(f'  - "{f}"')
    lines.extend(["---", ""])
    if notes:
        lines.extend([notes, ""])

    path.write_text("\n".join(lines))
    return {"path": str(path), "code": code, "agent": agent, "next_step": next_step}


def close_room(code: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM rooms WHERE code=? AND closed_at IS NULL", (code,)
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE rooms SET closed_at=? WHERE code=?", (time.time(), code))
        msgs = conn.execute(
            "SELECT * FROM messages WHERE room_code=? ORDER BY id", (code,)
        ).fetchall()
    path = _write_transcript(code, dict(row), [dict(m) for m in msgs])
    return {"code": code, "transcript_path": str(path)}


def _write_transcript(code: str, room: dict, messages: list[dict]) -> Path:
    import datetime

    vault_dir = os.environ.get("HIVECHAT_VAULT_DIR")
    if vault_dir:
        out_dir = Path(vault_dir)
    else:
        # Default: local transcripts/ next to the DB
        out_dir = _db_path().parent / "transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)

    date = datetime.datetime.now().strftime("%Y-%m-%d")
    path = out_dir / f"{date}-hive-{code}.md"

    lines = [
        "---",
        "type: hive-session",
        f"room_code: {code}",
        f"topic: {room.get('topic', '')}",
        f"date: {date}",
        "tags: [hive, multi-agent]",
        "---",
        "",
        f"# Hive: {room.get('topic') or code}",
        "",
    ]
    for m in messages:
        dt = datetime.datetime.fromtimestamp(m["ts"]).strftime("%H:%M:%S")
        lines.append(f"**{m['author']}** `{m['role']}` · {dt}")
        lines.append("")
        lines.append(m["text"])
        lines.append("")

    path.write_text("\n".join(lines))
    return path
