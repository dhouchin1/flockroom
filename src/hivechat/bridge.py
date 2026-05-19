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

    # Build the full prompt with room context injected
    full_prompt = _build_prompt(history, args.prompt, topic)

    backend_label = f"{args.backend}:{args.model or 'default'}"
    r.report_status(args.room, args.name, "thinking", f"Running {backend_label}")

    try:
        if args.backend == "claude":
            output = _run_claude(full_prompt, args.model)
        else:
            model = args.model or "llama3.1:8b"
            output = _run_openai(full_prompt, model, args.base_url, args.api_key)

        r.report_status(args.room, args.name, "posting", "")
        r.post_message(args.room, args.name, output)
        r.report_status(args.room, args.name, "done", "")

    except Exception as exc:
        r.report_status(args.room, args.name, "error", str(exc))
        r.post_message(args.room, args.name, f"[{args.name} error] {exc}")
        sys.exit(1)
