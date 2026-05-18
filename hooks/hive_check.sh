#!/usr/bin/env bash
# Claude Code Stop hook — checks for new hive room messages each turn boundary.
#
# If HIVE_ROOM is set and there are unread messages, injects them as context
# and returns {"decision":"block"} to force another agent turn.
#
# Install in ~/.claude/settings.json:
#   {
#     "hooks": {
#       "Stop": [{"hooks": [{"type": "command", "command": "/path/to/hive_check.sh"}]}]
#     }
#   }
#
# Required env:  HIVE_ROOM=<9-char room code>
# Optional env:  HIVE_PORT=8090   HIVE_HOST=127.0.0.1

[ -z "${HIVE_ROOM:-}" ] && exit 0

HOST="${HIVE_HOST:-127.0.0.1}"
PORT="${HIVE_PORT:-8090}"

# Last-seen ID is persisted in a state file (env vars don't survive between hook calls)
STATE_FILE="${HOME}/.config/hivechat/last_id_${HIVE_ROOM}"
LAST=$(cat "$STATE_FILE" 2>/dev/null || echo "0")

python3 - <<PYEOF
import json, os, sys, urllib.request, urllib.error

host  = "${HOST}"
port  = "${PORT}"
room  = "${HIVE_ROOM}"
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
reason = f"New messages in hive room {room}:\n" + "\n".join(lines)
print(json.dumps({"decision": "block", "reason": reason}))
PYEOF
