"""Durable on-disk state for a swarm session.

Directory layout per room:
  <base_dir>/<room_code>/
    design.md       — canonical design document
    tasks.json      — task list
    history.jsonl   — append-only event log
    .lock           — flock sidecar
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def _default_base() -> Path:
    fallback = Path.home() / ".config" / "hivechat" / "swarm"
    return Path(os.environ.get("HIVECHAT_SWARM_DIR", fallback))


# ── Task model ────────────────────────────────────────────────────────────────

VALID_STATUSES = {"proposed", "accepted", "in_progress", "blocked", "done", "rejected"}

LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"accepted", "rejected"},
    "accepted": {"in_progress", "blocked", "rejected"},
    "in_progress": {"blocked", "done"},
    "blocked": {"in_progress", "rejected"},
    "done": set(),
    "rejected": set(),
}


@dataclass
class TaskHistoryEntry:
    ts: float
    by: str
    from_status: str | None
    to: str
    note: str = ""

    def to_dict(self) -> dict:
        d: dict = {"ts": self.ts, "by": self.by, "from": self.from_status, "to": self.to}
        if self.note:
            d["note"] = self.note
        return d

    @classmethod
    def from_dict(cls, d: dict) -> TaskHistoryEntry:
        return cls(
            ts=d["ts"],
            by=d["by"],
            from_status=d.get("from"),
            to=d["to"],
            note=d.get("note", ""),
        )


@dataclass
class Task:
    id: int
    title: str
    status: str
    proposed_by: str
    assignee: str | None
    history: list[TaskHistoryEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "proposed_by": self.proposed_by,
            "assignee": self.assignee,
            "history": [h.to_dict() for h in self.history],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        return cls(
            id=d["id"],
            title=d["title"],
            status=d["status"],
            proposed_by=d["proposed_by"],
            assignee=d.get("assignee"),
            history=[TaskHistoryEntry.from_dict(h) for h in d.get("history", [])],
        )


# ── SwarmState ────────────────────────────────────────────────────────────────


class SwarmState:
    def __init__(self, room_code: str, base_dir: Path | None = None) -> None:
        self.room_code = room_code
        self.base_dir = (base_dir or _default_base()) / room_code
        self.design_path = self.base_dir / "design.md"
        self.tasks_path = self.base_dir / "tasks.json"
        self.history_path = self.base_dir / "history.jsonl"
        self._lock_path = self.base_dir / ".lock"
        self._patch_version = 0  # incremented on every successful apply_patch

    # ── lifecycle ──────────────────────────────────────────────────────────

    def init(self, initial_design: str = "") -> SwarmState:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        if not self.design_path.exists():
            self.design_path.write_text(initial_design)
        if not self.tasks_path.exists():
            self._write_tasks_raw({"next_id": 1, "tasks": []})
        if not self.history_path.exists():
            self.history_path.touch()
        self._lock_path.touch()
        return self

    # ── locking ────────────────────────────────────────────────────────────

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with open(self._lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    # ── design ─────────────────────────────────────────────────────────────

    def get_design(self) -> str:
        return self.design_path.read_text()

    def apply_patch(self, diff: str, author: str) -> tuple[bool, str]:
        with self._locked():
            current = self.design_path.read_text()
            new_text, ok, reason = _try_apply(current, diff)
            if not ok:
                return False, reason
            self.design_path.write_text(new_text)
            self._patch_version += 1
            lines_changed = _count_changes(diff)
            self.log_event(
                "design_patch",
                author,
                {
                    "applied": True,
                    "lines_changed": lines_changed,
                    "version": self._patch_version,
                },
            )
            return True, ""

    # ── tasks ──────────────────────────────────────────────────────────────

    def get_tasks(self) -> list[Task]:
        data = self._read_tasks_raw()
        return [Task.from_dict(t) for t in data["tasks"]]

    def propose_task(self, title: str, author: str) -> Task:
        with self._locked():
            data = self._read_tasks_raw()
            task_id = data["next_id"]
            data["next_id"] += 1
            now = time.time()
            task = Task(
                id=task_id,
                title=title,
                status="proposed",
                proposed_by=author,
                assignee=None,
                history=[TaskHistoryEntry(ts=now, by=author, from_status=None, to="proposed")],
            )
            data["tasks"].append(task.to_dict())
            self._write_tasks_raw(data)
            self.log_event("propose_task", author, {"id": task_id, "title": title})
            return task

    def update_task(self, id: int, status: str, note: str, author: str) -> Task | None:
        with self._locked():
            data = self._read_tasks_raw()
            for td in data["tasks"]:
                if td["id"] == id:
                    task = Task.from_dict(td)
                    allowed = LEGAL_TRANSITIONS.get(task.status, set())
                    if status not in allowed:
                        return None
                    now = time.time()
                    task.history.append(
                        TaskHistoryEntry(
                            ts=now, by=author, from_status=task.status, to=status, note=note
                        )
                    )
                    task.status = status
                    # Update the dict in-place
                    td.update(task.to_dict())
                    self._write_tasks_raw(data)
                    self.log_event(
                        "update_task", author, {"id": id, "status": status, "note": note}
                    )
                    return task
            return None

    def claim_task(self, id: int, author: str) -> Task | None:
        with self._locked():
            data = self._read_tasks_raw()
            for td in data["tasks"]:
                if td["id"] == id:
                    task = Task.from_dict(td)
                    # Transition proposed→accepted and assign
                    if task.status == "proposed":
                        now = time.time()
                        task.history.append(
                            TaskHistoryEntry(
                                ts=now, by=author, from_status="proposed", to="accepted"
                            )
                        )
                        task.status = "accepted"
                    task.assignee = author
                    td.update(task.to_dict())
                    self._write_tasks_raw(data)
                    self.log_event("claim_task", author, {"id": id, "assignee": author})
                    return task
            return None

    # ── history ────────────────────────────────────────────────────────────

    def log_event(self, event_type: str, author: str, payload: dict) -> None:
        entry = {"ts": time.time(), "type": event_type, "by": author, "payload": payload}
        with open(self.history_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    # ── summary ────────────────────────────────────────────────────────────

    def render_summary(self) -> str:
        tasks = self.get_tasks()
        now_str = datetime.now().strftime("%H:%M:%S")

        open_tasks = [t for t in tasks if t.status not in ("done", "rejected")]
        done_tasks = [t for t in tasks if t.status == "done"]

        lines = [f"**📊 Hive state @ {now_str}**", ""]

        if tasks:
            lines.append(f"**Tasks** ({len(open_tasks)} open / {len(done_tasks)} done)")
            for t in tasks:
                checkbox = "x" if t.status in ("done", "rejected") else " "
                assignee = f" ({t.assignee})" if t.assignee else ""
                lines.append(f"- [{checkbox}] #{t.id} *{t.status}* — {t.title}{assignee}")
        else:
            lines.append("**Tasks** — none yet")

        lines.append("")
        lines.append(f"**Design** — `design.md` v{self._patch_version}")
        lines.append("")
        if open_tasks:
            lines.append(
                f"_Open tasks remaining: {len(open_tasks)}. Send `FINAL:` to close the room._"
            )
        else:
            lines.append("_All tasks complete. Send `FINAL:` to close the room._")

        return "\n".join(lines)

    # ── internal helpers ───────────────────────────────────────────────────

    def _read_tasks_raw(self) -> dict:
        return json.loads(self.tasks_path.read_text())

    def _write_tasks_raw(self, data: dict) -> None:
        self.tasks_path.write_text(json.dumps(data, indent=2))


# ── Patch application helpers ─────────────────────────────────────────────────


def _try_apply(current: str, diff: str) -> tuple[str, bool, str]:
    """Try to apply a unified diff to `current`. Returns (new_text, ok, reason)."""
    try:
        import unidiff  # noqa: F401  # availability probe — caller uses the helper below

        return _apply_with_unidiff(current, diff)
    except ImportError:
        pass
    return _apply_with_patch_cli(current, diff)


def _apply_with_unidiff(current: str, diff: str) -> tuple[str, bool, str]:
    import unidiff  # type: ignore[import]

    try:
        patch_set = unidiff.PatchSet(diff)
    except Exception as e:
        return current, False, f"parse error: {e}"

    lines = current.splitlines(keepends=True)
    for patched_file in patch_set:
        for hunk in patched_file:
            src_start = hunk.source_start - 1  # 0-indexed
            src_len = hunk.source_length
            new_lines = []
            for line in hunk:
                if line.line_type in (unidiff.LINE_TYPE_ADDED, unidiff.LINE_TYPE_CONTEXT):
                    new_lines.append(line.value)
            lines[src_start : src_start + src_len] = new_lines

    return "".join(lines), True, ""


def _apply_with_patch_cli(current: str, diff: str) -> tuple[str, bool, str]:
    """Shell out to the `patch` CLI."""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "design.md"
        patch_file = Path(td) / "changes.patch"
        target.write_text(current)
        patch_file.write_text(diff)
        result = subprocess.run(
            ["patch", "-p0", "--batch", "-F0", "--silent", str(target)],
            input=diff,
            capture_output=True,
            text=True,
            cwd=td,
        )
        if result.returncode != 0:
            return current, False, result.stderr.strip() or "patch failed"
        return target.read_text(), True, ""


def _count_changes(diff: str) -> int:
    lines = diff.splitlines()
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return added + removed
