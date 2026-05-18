"""MCP tool definitions.

Agents add hivechat to their MCP config and call these tools to join rooms,
post messages, and report their status for dashboard visualization.

Typical agent workflow:
  1. create_room(topic) — or receive a room code from the orchestrator
  2. join_room(code, name, role)
  3. Loop: read_messages → think → report_status → tool calls → log_tool_call → post_message
  4. close_room when done (orchestrator's job)
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import rooms as r

mcp = FastMCP(
    "hivechat",
    description="Shared chat rooms for multiple AI agents. "
    "Agents join a room by code and communicate through post_message / read_messages. "
    "Call report_status and log_tool_call to feed the dashboard visualization layer.",
)


@mcp.tool()
def create_room(topic: str = "") -> dict:
    """Create a new hive room.

    Returns a 9-character room code that other agents use to join.
    Share the code with your team via your task description or environment.
    """
    return r.create_room(topic)


@mcp.tool()
def join_room(code: str, name: str, role: str = "assistant") -> dict:
    """Join a hive room by its 9-character code.

    Args:
        code: 9-character room code (e.g. "abc3def7g")
        name: Your display name — appears in every message you post
        role: Your role in the team (e.g. "orchestrator", "coder", "reviewer", "user")

    Returns the room topic, full message history, and current participant list.
    After joining, use post_message to send messages and read_messages to check for new ones.
    Call report_status before/after major actions so the dashboard shows your activity.
    """
    result = r.join_room(code, name, role)
    if result is None:
        return {"error": f"Room '{code}' not found or already closed"}
    return result


@mcp.tool()
def post_message(code: str, author: str, text: str) -> dict:
    """Post a message to the hive room. All participants will see it on their next read.

    Args:
        code:   Room code
        author: Your name — must match the name used in join_room
        text:   Message content (markdown supported)
    """
    result = r.post_message(code, author, text)
    if result is None:
        return {"error": f"Room '{code}' not found or already closed"}
    return result


@mcp.tool()
def read_messages(code: str, since_id: int = 0) -> list:
    """Read messages from the room.

    Args:
        code:     Room code
        since_id: Only return messages with id > this value (pass last id you saw)

    Pass since_id=0 to get the full history. After reading, store the highest id
    returned and pass it next time to get only new messages.
    """
    return r.read_messages(code, since_id)


@mcp.tool()
def report_status(code: str, agent: str, status: str, action: str = "") -> dict:
    """Report your current status to the dashboard visualization layer.

    Args:
        code:   Room code
        agent:  Your name
        status: One of: "idle" | "thinking" | "tool_use" | "posting" | "error" | "done"
        action: Human-readable description (e.g. "Reading auth.py", "Running pytest")

    Call this before starting significant work and after finishing each step.
    These status updates power the agent status bar and activity feed in the dashboard.
    """
    ok = r.report_status(code, agent, status, action)
    return {"ok": ok}


@mcp.tool()
def log_tool_call(
    code: str,
    agent: str,
    tool: str,
    args_summary: str,
    result_summary: str = "",
) -> dict:
    """Log a significant tool call to the room's activity feed.

    Args:
        code:           Room code
        agent:          Your name
        tool:           Tool name (e.g. "Read", "Bash", "WebSearch", "Edit")
        args_summary:   Brief description of arguments (e.g. "src/auth.py")
        result_summary: Brief description of result (e.g. "847 lines, 3 TODO items")

    Use for Read, Bash, WebSearch, Edit and other tools with meaningful output.
    Skip trivial or repetitive calls. This feeds the annotated message thread.
    """
    ok = r.log_tool_call(code, agent, tool, args_summary, result_summary)
    return {"ok": ok}


@mcp.tool()
def update_progress(code: str, agent: str, step_index: int, done: bool = True) -> dict:
    """Mark a project checkpoint step as started or complete.

    Args:
        code:       Room code
        agent:      Your name
        step_index: 0-based index of the step in the project's checkpoint list
        done:       True = step complete, False = step started

    This updates the progress tracker widget in the dashboard.
    """
    ok = r.update_progress(code, agent, step_index, done)
    return {"ok": ok}


@mcp.tool()
def list_rooms() -> list:
    """List all currently open hive rooms."""
    return r.list_rooms()


@mcp.tool()
def close_room(code: str) -> dict:
    """Close the room and flush the full transcript to a markdown file.

    Set HIVECHAT_VAULT_DIR to write the transcript directly into your vault.
    Otherwise it lands in ~/.config/hivechat/transcripts/.

    Typically called by the orchestrator after the team's work is complete.
    """
    result = r.close_room(code)
    if result is None:
        return {"error": f"Room '{code}' not found or already closed"}
    return result
