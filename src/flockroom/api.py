"""FastAPI REST + SSE endpoints for any dashboard or client.

Run with: flockroom serve [--host 127.0.0.1] [--port 8090]

Clients connect to:
  GET  /rooms                      — list active rooms
  GET  /rooms/{code}               — room detail + recent messages
  GET  /rooms/{code}/stream        — SSE typed event stream (for live visualization)
  POST /rooms                      — create room
  POST /rooms/{code}/join          — join room
  GET  /rooms/{code}/messages      — poll messages (also used by stop hook)
  POST /rooms/{code}/messages      — post message
  POST /rooms/{code}/status        — report agent status
  POST /rooms/{code}/tool-call     — log a tool call
  POST /rooms/{code}/progress      — update checkpoint progress
  DELETE /rooms/{code}             — close room + write transcript
"""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import rooms

app = FastAPI(title="flockroom", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── request bodies ────────────────────────────────────────────────────────────


class CreateBody(BaseModel):
    topic: str = ""


class JoinBody(BaseModel):
    name: str
    role: str = "assistant"


class MessageBody(BaseModel):
    author: str
    text: str


class StatusBody(BaseModel):
    agent: str
    status: str
    action: str = ""


class ToolCallBody(BaseModel):
    agent: str
    tool: str
    args_summary: str
    result_summary: str = ""


class ProgressBody(BaseModel):
    agent: str
    step_index: int
    done: bool = True


class CheckpointBody(BaseModel):
    agent: str
    completed_steps: list[str]
    next_step: str
    context_files: list[str] = []
    notes: str = ""


# ── routes ───────────────────────────────────────────────────────────────────


@app.get("/health")
def health():
    return {"ok": True, "service": "flockroom"}


@app.get("/rooms")
def get_rooms():
    return rooms.list_rooms()


@app.post("/rooms", status_code=201)
def create_room(body: CreateBody):
    return rooms.create_room(body.topic)


@app.get("/rooms/{code}")
def get_room(code: str):
    result = rooms.get_room(code)
    if result is None:
        raise HTTPException(404, f"Room '{code}' not found")
    return result


@app.post("/rooms/{code}/join")
def join_room(code: str, body: JoinBody):
    result = rooms.join_room(code, body.name, body.role)
    if result is None:
        raise HTTPException(404, f"Room '{code}' not found or closed")
    return result


@app.get("/rooms/{code}/messages")
def get_messages(code: str, since_id: int = 0):
    return rooms.read_messages(code, since_id)


@app.post("/rooms/{code}/messages", status_code=201)
def post_message(code: str, body: MessageBody):
    result = rooms.post_message(code, body.author, body.text)
    if result is None:
        raise HTTPException(404, f"Room '{code}' not found or closed")
    return result


@app.post("/rooms/{code}/status")
def post_status(code: str, body: StatusBody):
    ok = rooms.report_status(code, body.agent, body.status, body.action)
    if not ok:
        raise HTTPException(404, f"Room '{code}' not found or closed")
    return {"ok": True}


@app.post("/rooms/{code}/tool-call")
def post_tool_call(code: str, body: ToolCallBody):
    ok = rooms.log_tool_call(code, body.agent, body.tool, body.args_summary, body.result_summary)
    if not ok:
        raise HTTPException(404, f"Room '{code}' not found or closed")
    return {"ok": True}


@app.post("/rooms/{code}/progress")
def post_progress(code: str, body: ProgressBody):
    ok = rooms.update_progress(code, body.agent, body.step_index, body.done)
    if not ok:
        raise HTTPException(404, f"Room '{code}' not found or closed")
    return {"ok": True}


@app.post("/rooms/{code}/checkpoint", status_code=201)
def post_checkpoint(code: str, body: CheckpointBody):
    result = rooms.write_checkpoint(
        code,
        body.agent,
        body.completed_steps,
        body.next_step,
        body.context_files or None,
        body.notes,
    )
    if result is None:
        raise HTTPException(404, f"Room '{code}' not found")
    return result


@app.delete("/rooms/{code}")
def delete_room(code: str):
    result = rooms.close_room(code)
    if result is None:
        raise HTTPException(404, f"Room '{code}' not found or closed")
    return result


@app.get("/rooms/{code}/stream")
async def stream_events(code: str, since_id: int = 0):
    """SSE stream of typed room events for real-time dashboard visualization.

    Event types:
      message        — a participant posted a message
      tool_call      — an agent logged a tool invocation
      status_change  — an agent's status changed
      participant_join — a new participant joined
      progress       — a checkpoint step was updated
    """

    async def generate():
        last_id = since_id
        while True:
            new_events = rooms.get_events(code, since_id=last_id)
            for ev in new_events:
                last_id = ev["id"]
                flat = {
                    "type": ev["type"],
                    "id": ev["id"],
                    "agent": ev["agent"],
                    "author": ev["agent"],
                    "name": ev["agent"],
                    "ts": ev["ts"],
                }
                flat.update(ev.get("data", {}))
                yield f"data: {json.dumps(flat)}\n\n"
            if not new_events:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
