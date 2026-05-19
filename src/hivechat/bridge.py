"""hivechat bridge agent — run any model as a hive participant.

Supported backends:
  claude   — claude --print [--model <id>] <prompt>  (default)
  openai   — OpenAI-compatible REST API: Ollama, Groq, OpenAI, Gemini, etc.

The agent joins the room, incorporates full room history as context, calls
the model, and posts the result back.  If --wait-for-role is set it polls
the room until an agent with that role has posted before processing — this
is what makes true sequential cascade topologies work.

Typical invocation (spawned by brain-bridge launch-team):
  hivechat agent --room <code> --name "Implementer" --role implementer \\
                 --backend claude --model claude-sonnet-4-6 \\
                 --wait-for-role preprocessor \\
                 --prompt "Implement the solution based on the analysis above."
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from argparse import ArgumentParser

from . import rooms as r

_POLL_INTERVAL = 5  # seconds between wait-for-role polls
_DEFAULT_TIMEOUT = 600  # 10-minute timeout for waiting

# Phrases that mean "the conversation is winding down" beyond the literal
# stop marker. Matched case-insensitive against peer message bodies.
_WIND_DOWN_PHRASES = (
    "final:",
    "**final**",
    "## final",
    "final evaluation",
    "final verdict",
    "final answer",
    "session complete",
    "session is complete",
    "task complete",
    "task is complete",
    "no further action",
    "no further response",
    "nothing more to add",
    "nothing further",
    "we are done",
    "we're done",
)


def _looks_like_winddown(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _WIND_DOWN_PHRASES)


# ── model backends ─────────────────────────────────────────────────────────────


def _run_claude(prompt: str, model: str | None) -> str:
    cmd = ["claude", "--print"]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=_DEFAULT_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"claude exited {result.returncode}")
    return result.stdout.strip()


def _run_openai(prompt: str, model: str, base_url: str, api_key: str) -> str:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


# ── context building ───────────────────────────────────────────────────────────


def _build_prompt(history: list[dict], system_prompt: str, topic: str) -> str:
    parts: list[str] = []
    if topic:
        parts.append(f"Room topic: {topic}\n")
    if history:
        parts.append("=== Room history ===")
        for m in history:
            parts.append(f"[{m['role']}] {m['author']}:\n{m['text']}")
        parts.append("=== End of history ===\n")
    parts.append(system_prompt)
    return "\n\n".join(parts)


# ── main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    ap = ArgumentParser(
        prog="hivechat agent",
        description="Run a model as a hive room participant",
    )
    ap.add_argument("--room", required=True, help="9-character room code")
    ap.add_argument("--name", required=True, help="Display name shown in the room")
    ap.add_argument("--role", default="assistant", help="Role label (orchestrator, coder, etc.)")
    ap.add_argument(
        "--backend",
        choices=["claude", "openai"],
        default="claude",
        help="'claude' = claude --print CLI; 'openai' = OpenAI-compatible REST API",
    )
    ap.add_argument(
        "--model",
        default=None,
        help="Model ID (e.g. claude-haiku-4-5, llama3.1:8b, gpt-4o-mini)",
    )
    ap.add_argument(
        "--base-url",
        default="http://localhost:11434",
        help="Base URL for openai backend (default: Ollama on localhost)",
    )
    ap.add_argument(
        "--api-key",
        default="ollama",
        help="API key for openai backend (use 'ollama' for local Ollama)",
    )
    ap.add_argument(
        "--wait-for-role",
        default=None,
        metavar="ROLE",
        help="Poll the room until an agent with this role has posted, then process",
    )
    ap.add_argument(
        "--wait-timeout",
        type=int,
        default=_DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help="Max seconds to wait for --wait-for-role (default: 600)",
    )
    ap.add_argument(
        "--loop",
        action="store_true",
        help="After the first run, keep polling for peer messages and re-run the "
             "model on each new message (reactive swarm mode). Exits when --loop-max "
             "iterations is hit or the room is closed.",
    )
    ap.add_argument(
        "--loop-poll",
        type=int,
        default=_POLL_INTERVAL,
        metavar="SECONDS",
        help="Seconds between peer-message polls in --loop mode (default: 5)",
    )
    ap.add_argument(
        "--loop-max",
        type=int,
        default=5,
        metavar="N",
        help="Hard cap on iterations in --loop mode to bound cost (default: 5)",
    )
    ap.add_argument(
        "--loop-idle-exit",
        type=int,
        default=180,
        metavar="SECONDS",
        help="Exit if --loop sees no peer messages for this many seconds (default: 180)",
    )
    ap.add_argument(
        "--loop-stop-marker",
        default="FINAL:",
        metavar="STRING",
        help="If a peer message contains this marker, exit the loop. Set to '' to disable. "
             "Default 'FINAL:' — works with the Swarm preset's moderator prompt.",
    )
    ap.add_argument(
        "--loop-self-cooldown",
        type=int,
        default=20,
        metavar="SECONDS",
        help="Minimum gap between our own posts (default: 20). Prevents immediate echo loops "
             "where we react to a peer who was reacting to us.",
    )
    ap.add_argument(
        "--loop-quiet",
        type=int,
        default=8,
        metavar="SECONDS",
        help="Wait for peer messages to stop arriving for this many seconds before "
             "generating a response (default: 8). Lets parallel peer posts batch into a "
             "single reaction.",
    )
    ap.add_argument(
        "--loop-room-cap",
        type=int,
        default=15,
        metavar="N",
        help="Total room message cap. If the room has more messages than this, exit "
             "regardless of peer activity (default: 15).",
    )
    ap.add_argument("--prompt", required=True, help="System prompt / task description")

    # When invoked via `hivechat agent ...`, sys.argv is
    #   ['hivechat', 'agent', '--room', ...]
    # so strip the `agent` subcommand token before parsing.
    argv = sys.argv[1:]
    if argv and argv[0] == "agent":
        argv = argv[1:]
    args = ap.parse_args(argv)

    # Join the room and get current history
    joined = r.join_room(args.room, args.name, args.role)
    if joined is None:
        print(f"Error: room '{args.room}' not found or closed", file=sys.stderr)
        sys.exit(1)

    history: list[dict] = joined.get("history", [])
    topic: str = joined.get("topic", "")
    last_id: int = max((m["id"] for m in history), default=0)

    # Optionally wait until a specific upstream role has posted
    if args.wait_for_role:
        r.report_status(args.room, args.name, "idle", f"Waiting for {args.wait_for_role}")
        deadline = time.monotonic() + args.wait_timeout
        trigger_found = any(m["role"] == args.wait_for_role for m in history)

        while not trigger_found:
            if time.monotonic() > deadline:
                r.post_message(
                    args.room,
                    args.name,
                    f"Timeout waiting for upstream {args.wait_for_role!r} stage.",
                )
                sys.exit(1)
            time.sleep(_POLL_INTERVAL)
            new_msgs = r.read_messages(args.room, since_id=last_id)
            if new_msgs:
                history.extend(new_msgs)
                last_id = new_msgs[-1]["id"]
            trigger_found = any(m["role"] == args.wait_for_role for m in history)

    backend_label = f"{args.backend}:{args.model or 'default'}"

    def _generate_and_post(hist: list[dict]) -> None:
        full_prompt = _build_prompt(hist, args.prompt, topic)
        r.report_status(args.room, args.name, "thinking", f"Running {backend_label}")
        if args.backend == "claude":
            output = _run_claude(full_prompt, args.model)
        else:
            model = args.model or "llama3.1:8b"
            output = _run_openai(full_prompt, model, args.base_url, args.api_key)
        r.report_status(args.room, args.name, "posting", "")
        r.post_message(args.room, args.name, output)
        r.report_status(args.room, args.name, "done", "")

    try:
        _generate_and_post(history)
        # Re-fetch our own posted message so the loop sees it in history
        history = r.read_messages(args.room, since_id=0)
        last_id = max((m["id"] for m in history), default=last_id)

    except Exception as exc:
        r.report_status(args.room, args.name, "error", str(exc))
        r.post_message(args.room, args.name, f"[{args.name} error] {exc}")
        sys.exit(1)

    if not args.loop:
        return

    # ── reactive swarm loop ───────────────────────────────────────────────────
    # The naive "respond on every peer message" approach turns into an echo
    # cascade because two peers responding to each other in the same window
    # both trigger us, and our reply triggers them again. Defenses:
    #
    #  - self_cooldown: minimum delay since OUR last post.
    #  - quiet:         peer activity has to settle for N seconds first
    #                   (so parallel peer posts batch into one reaction).
    #  - stop_marker:   literal token (default "FINAL:") in any peer message.
    #  - winddown:      heuristic match on common wrap-up phrases.
    #  - room_cap:      absolute message-count cap on the room.
    #  - own_winddown:  if WE just posted wrap-up language, stop ourselves.
    #
    r.report_status(args.room, args.name, "idle", "Watching for peer messages")
    iterations = 0
    last_peer_ts = time.monotonic()
    last_own_post = time.monotonic()  # we just posted above
    last_peer_msg_time = time.monotonic()

    while iterations < args.loop_max:
        time.sleep(args.loop_poll)

        # Check whether the room is still open / message cap hit
        room = r.get_room(args.room)
        if room is None or room.get("closed_at"):
            r.report_status(args.room, args.name, "done", "Room closed")
            return
        if room.get("message_count", 0) >= args.loop_room_cap:
            r.report_status(args.room, args.name, "done",
                            f"Room cap reached ({args.loop_room_cap})")
            return

        new_msgs = r.read_messages(args.room, since_id=last_id)
        if new_msgs:
            history.extend(new_msgs)
            last_id = new_msgs[-1]["id"]
            last_peer_msg_time = time.monotonic()
        peer_msgs = [m for m in new_msgs if m["author"] != args.name]

        if not peer_msgs:
            if time.monotonic() - last_peer_ts > args.loop_idle_exit:
                r.report_status(args.room, args.name, "done", "Idle exit")
                return
            continue

        # Stop conditions — literal marker OR semantic wind-down language.
        if args.loop_stop_marker and any(
            args.loop_stop_marker in m["text"] for m in peer_msgs
        ):
            r.report_status(args.room, args.name, "done", "Stop marker seen")
            return
        if any(_looks_like_winddown(m["text"]) for m in peer_msgs):
            r.report_status(args.room, args.name, "done", "Wind-down phrase seen")
            return

        # Self-cooldown: don't fire if we posted very recently. Just absorb the
        # peer messages into history and wait until our cooldown clears.
        since_own = time.monotonic() - last_own_post
        if since_own < args.loop_self_cooldown:
            continue

        # Quiet-peer: wait until peer activity has settled. If a new peer
        # message arrived less than `loop_quiet` seconds ago, keep waiting —
        # this lets two peers responding in parallel batch into one reaction.
        since_last_peer = time.monotonic() - last_peer_msg_time
        if since_last_peer < args.loop_quiet:
            continue

        last_peer_ts = time.monotonic()
        iterations += 1
        try:
            _generate_and_post(history)
            last_own_post = time.monotonic()
            history = r.read_messages(args.room, since_id=0)
            last_id = max((m["id"] for m in history), default=last_id)

            # If WE just produced wrap-up language, exit so we don't generate
            # again on the next peer "ack" cascade.
            own_last = history[-1]["text"] if history else ""
            if _looks_like_winddown(own_last):
                r.report_status(args.room, args.name, "done", "Own wind-down")
                return

        except Exception as exc:
            r.report_status(args.room, args.name, "error", str(exc))
            r.post_message(args.room, args.name, f"[{args.name} error] {exc}")
            return
