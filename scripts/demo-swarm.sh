#!/usr/bin/env bash
# Drives a scripted 3-agent swarm against a running flockroom server.
# Used to record the README GIF — deterministic, snappy, ~10 seconds.
#
# Usage:
#   1.  flockroom serve              (in another terminal)
#   2.  open web/viewer.html
#   3.  start your screen recorder, framed on the viewer window
#   4.  ./scripts/demo-swarm.sh     and paste the printed room code into the viewer
#   5.  press ENTER to fire the swarm
#
# Requires: curl, jq.

set -euo pipefail

HOST="${FLOCK_HOST:-127.0.0.1}"
PORT="${FLOCK_PORT:-8099}"
BASE="http://${HOST}:${PORT}"

post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2" >/dev/null; }

# ── create room ──────────────────────────────────────────────────────────────
CODE=$(curl -s -X POST "$BASE/rooms" \
  -H 'Content-Type: application/json' \
  -d '{"topic":"Pick a 1-sentence tagline for flockroom"}' | jq -r .code)

echo
echo "  room code:  $CODE"
echo "  paste into the viewer, frame your recording, then press ENTER"
read -r

# ── join cast ────────────────────────────────────────────────────────────────
post "/rooms/$CODE/join" '{"name":"proposer","role":"proposer"}'
post "/rooms/$CODE/join" '{"name":"critic","role":"critic"}'
post "/rooms/$CODE/join" '{"name":"moderator","role":"moderator"}'

post "/rooms/$CODE/status" '{"agent":"proposer","status":"idle"}'
post "/rooms/$CODE/status" '{"agent":"critic","status":"idle"}'
post "/rooms/$CODE/status" '{"agent":"moderator","status":"idle"}'
sleep 1.4

# ── beat 1: proposer offers ──────────────────────────────────────────────────
post "/rooms/$CODE/status" '{"agent":"proposer","status":"thinking","action":"drafting tagline"}'
sleep 1.1
post "/rooms/$CODE/status" '{"agent":"proposer","status":"posting"}'
sleep 0.25
post "/rooms/$CODE/messages" '{"author":"proposer","text":"How about: \"A shared room any MCP agent can join.\""}'
post "/rooms/$CODE/status" '{"agent":"proposer","status":"idle"}'
sleep 1.4

# ── beat 2: critic pushes back (the load-bearing moment) ─────────────────────
post "/rooms/$CODE/status" '{"agent":"critic","status":"thinking","action":"evaluating"}'
sleep 1.1
post "/rooms/$CODE/status" '{"agent":"critic","status":"posting"}'
sleep 0.25
post "/rooms/$CODE/messages" '{"author":"critic","text":"Boring. Lead with the verb. Make it concrete."}'
post "/rooms/$CODE/status" '{"agent":"critic","status":"idle"}'
sleep 1.7

# ── beat 3: proposer revises ─────────────────────────────────────────────────
post "/rooms/$CODE/status" '{"agent":"proposer","status":"thinking","action":"revising"}'
sleep 1.1
post "/rooms/$CODE/status" '{"agent":"proposer","status":"posting"}'
sleep 0.25
post "/rooms/$CODE/messages" '{"author":"proposer","text":"\"Three Claudes walk into a room.\" Then explain underneath."}'
post "/rooms/$CODE/status" '{"agent":"proposer","status":"idle"}'
sleep 1.4

# ── beat 4: moderator calls it ───────────────────────────────────────────────
post "/rooms/$CODE/status" '{"agent":"moderator","status":"posting"}'
sleep 0.25
post "/rooms/$CODE/messages" '{"author":"moderator","text":"Ship it. Closing the room."}'
post "/rooms/$CODE/status" '{"agent":"moderator","status":"done"}'
sleep 1.8

echo
echo "  swarm complete — stop your recording"
echo "  close the room with:  curl -X DELETE $BASE/rooms/$CODE"
