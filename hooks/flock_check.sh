#!/usr/bin/env bash
# Claude Code Stop hook — checks for new flock room messages each turn boundary.
#
# If FLOCK_ROOM is set and there are unread messages, injects them as context
# and returns {"decision":"block"} to force another agent turn.
#
# Install in ~/.claude/settings.json:
#   {
#     "hooks": {
#       "Stop": [{"hooks": [{"type": "command", "command": "/path/to/flock_check.sh"}]}]
#     }
#   }
#
# Required env:  FLOCK_ROOM=<9-char room code>
# Optional env:  FLOCK_PORT=8099   FLOCK_HOST=127.0.0.1

[ -z "${FLOCK_ROOM:-}" ] && exit 0

HOST="${FLOCK_HOST:-127.0.0.1}"
PORT="${FLOCK_PORT:-8099}"

# Last-seen ID is persisted in a state file (env vars don't survive between hook calls)
STATE_FILE="${HOME}/.config/flockroom/last_id_${FLOCK_ROOM}"
LAST=$(cat "$STATE_FILE" 2>/dev/null || echo "0")

python3 - <<PYEOF
import json, os, sys, urllib.request, urllib.error

host  = "${HOST}"
port  = "${PORT}"
room  = "${FLOCK_ROOM}"
last  = int("${LAST}" or 0)
state = "${STATE_FILE}"

try:
    url = f"http://{host}:{port}/rooms/{room}/messages?since_id={last}"
    with urllib.request.urlopen(url, timeout=3) as resp:
        msgs = json.loads(resp.read())
except Exception:
    sys.exit(0)

if not msgs:
    sys.exit(0)

# Persist the new high-water mark
os.makedirs(os.path.dirname(state), exist_ok=True)
with open(state, "w") as f:
    f.write(str(msgs[-1]["id"]))

lines = [f"[{m['author']} ({m['role']})]: {m['text']}" for m in msgs]
reason = f"New messages in flock room {room}:\n" + "\n".join(lines)
print(json.dumps({"decision": "block", "reason": reason}))
PYEOF
