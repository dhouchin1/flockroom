"""Message protocol parsing for swarm sessions.

Pure functions — no I/O. Parses text message bodies into typed Event objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProposeTask:
    title: str


@dataclass
class ClaimTask:
    id: int


@dataclass
class UpdateTask:
    id: int
    status: str
    note: str = ""


@dataclass
class DesignPatch:
    diff: str


@dataclass
class Comment:
    text: str


@dataclass
class Final:
    text: str = ""


Event = ProposeTask | ClaimTask | UpdateTask | DesignPatch | Comment | Final

VALID_STATUSES = {"proposed", "accepted", "in_progress", "blocked", "done", "rejected"}


def parse(text: str) -> list[Event]:
    """Parse a message body into zero or more Events."""
    events: list[Event] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("PROPOSE_TASK:"):
            title = line[len("PROPOSE_TASK:"):].strip()
            if title:
                events.append(ProposeTask(title=title))
            i += 1

        elif line.startswith("CLAIM_TASK:"):
            raw = line[len("CLAIM_TASK:"):].strip()
            try:
                events.append(ClaimTask(id=int(raw)))
            except ValueError:
                pass
            i += 1

        elif line.startswith("UPDATE_TASK:"):
            raw = line[len("UPDATE_TASK:"):].strip()
            parts = raw.split(None, 2)
            if len(parts) >= 2:
                try:
                    task_id = int(parts[0])
                    status = parts[1]
                    note = parts[2] if len(parts) > 2 else ""
                    if status in VALID_STATUSES:
                        events.append(UpdateTask(id=task_id, status=status, note=note))
                except ValueError:
                    pass
            i += 1

        elif line.startswith("DESIGN_PATCH:"):
            # Collect all remaining lines as the diff
            diff_lines = []
            i += 1
            while i < len(lines):
                diff_lines.append(lines[i])
                i += 1
            diff = "\n".join(diff_lines)
            if diff:
                events.append(DesignPatch(diff=diff))

        elif line.startswith("COMMENT:"):
            text_body = line[len("COMMENT:"):].strip()
            events.append(Comment(text=text_body))
            i += 1

        elif line.startswith("FINAL:"):
            summary = line[len("FINAL:"):].strip()
            events.append(Final(text=summary))
            i += 1

        else:
            i += 1

    return events
