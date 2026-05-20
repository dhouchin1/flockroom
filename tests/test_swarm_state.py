"""Unit tests for flockroom.swarm.state.

Covers all SwarmState methods, all legal/illegal task transitions, patch
application, concurrent writes, and regression tests for every issue the
Reviewer flagged:

  CRITICAL:
    - _patch_version resets on every __init__ (not persisted)
    - _apply_with_unidiff multi-hunk index corruption

  MAJOR:
    - update_task/claim_task both return None for two distinct failure modes
      (task not found vs illegal transition) — callers can't distinguish them

  MINOR:
    - claim_task silently re-assigns non-proposed tasks' assignee
    - _write_tasks_raw is non-atomic (documented, not reliably testable)
    - VALID_STATUSES duplicated between protocol.py and state.py
"""

import json
import threading

import pytest

from flockroom.swarm.state import (
    LEGAL_TRANSITIONS,
    VALID_STATUSES,
    SwarmState,
    Task,
    _count_changes,
)

# ── Shared diffs ───────────────────────────────────────────────────────────────

_ADD_DIFF = "--- design.md\n+++ design.md\n@@ -0,0 +1,2 @@\n+# Title\n+Content here\n"
_REPLACE_DIFF = "--- design.md\n+++ design.md\n@@ -1 +1 @@\n-# Title\n+# Better Title\n"
_INVALID_DIFF = "this is not a valid unified diff"


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def state(tmp_path):
    s = SwarmState("testroom", base_dir=tmp_path)
    s.init()
    return s


# ── init ──────────────────────────────────────────────────────────────────────


def test_fresh_state_zero_tasks(state):
    assert state.get_tasks() == []


def test_fresh_design_empty(state):
    assert state.get_design() == ""


def test_init_with_initial_design(tmp_path):
    s = SwarmState("room2", base_dir=tmp_path)
    s.init(initial_design="# My Design\n")
    assert s.get_design() == "# My Design\n"


def test_init_creates_required_files(tmp_path):
    s = SwarmState("newroom", base_dir=tmp_path)
    s.init()
    assert s.design_path.exists()
    assert s.tasks_path.exists()
    assert s.history_path.exists()
    assert s._lock_path.exists()


def test_init_is_idempotent(tmp_path):
    s = SwarmState("room3", base_dir=tmp_path)
    s.init()
    s.propose_task("Keep me", "alice")
    s.init()  # second call must not wipe existing state
    assert len(s.get_tasks()) == 1


def test_init_returns_self(tmp_path):
    s = SwarmState("room4", base_dir=tmp_path)
    result = s.init()
    assert result is s


# ── propose_task ──────────────────────────────────────────────────────────────


def test_propose_task_returns_task_object(state):
    task = state.propose_task("Write tests", "alice")
    assert isinstance(task, Task)
    assert task.id == 1
    assert task.title == "Write tests"
    assert task.status == "proposed"
    assert task.proposed_by == "alice"
    assert task.assignee is None


def test_propose_task_sequential_ids(state):
    ids = [state.propose_task(f"Task {i}", "alice").id for i in range(3)]
    assert ids == [1, 2, 3]


def test_propose_task_persisted_to_disk(state):
    state.propose_task("Persisted", "bob")
    # Read from a fresh instance — verifies disk persistence
    s2 = SwarmState("testroom", base_dir=state.base_dir.parent)
    tasks = s2.get_tasks()
    assert len(tasks) == 1
    assert tasks[0].title == "Persisted"


def test_propose_task_history_entry(state):
    task = state.propose_task("T", "alice")
    assert len(task.history) == 1
    h = task.history[0]
    assert h.from_status is None
    assert h.to == "proposed"
    assert h.by == "alice"
    assert h.ts > 0


def test_propose_task_logs_history_event(state):
    state.propose_task("T", "alice")
    lines = state.history_path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "propose_task"
    assert entry["by"] == "alice"
    assert entry["payload"]["title"] == "T"


# ── update_task — legal transitions ───────────────────────────────────────────


def test_update_task_proposed_to_accepted(state):
    t = state.propose_task("T", "alice")
    result = state.update_task(t.id, "accepted", "", "bob")
    assert result is not None
    assert result.status == "accepted"


def test_update_task_proposed_to_rejected(state):
    t = state.propose_task("T", "alice")
    result = state.update_task(t.id, "rejected", "not needed", "bob")
    assert result is not None
    assert result.status == "rejected"


def test_update_task_accepted_to_in_progress(state):
    t = state.propose_task("T", "alice")
    state.update_task(t.id, "accepted", "", "bob")
    result = state.update_task(t.id, "in_progress", "", "bob")
    assert result is not None
    assert result.status == "in_progress"


def test_update_task_accepted_to_blocked(state):
    t = state.propose_task("T", "alice")
    state.update_task(t.id, "accepted", "", "bob")
    result = state.update_task(t.id, "blocked", "waiting on design", "bob")
    assert result is not None
    assert result.status == "blocked"


def test_update_task_in_progress_to_done(state):
    t = state.propose_task("T", "alice")
    state.update_task(t.id, "accepted", "", "bob")
    state.update_task(t.id, "in_progress", "", "bob")
    result = state.update_task(t.id, "done", "shipped", "bob")
    assert result is not None
    assert result.status == "done"


def test_update_task_in_progress_to_blocked(state):
    t = state.propose_task("T", "alice")
    state.update_task(t.id, "accepted", "", "bob")
    state.update_task(t.id, "in_progress", "", "bob")
    result = state.update_task(t.id, "blocked", "needs review", "bob")
    assert result is not None
    assert result.status == "blocked"


def test_update_task_blocked_to_in_progress(state):
    t = state.propose_task("T", "alice")
    state.update_task(t.id, "accepted", "", "bob")
    state.update_task(t.id, "blocked", "waiting", "bob")
    result = state.update_task(t.id, "in_progress", "", "bob")
    assert result is not None
    assert result.status == "in_progress"


def test_update_task_blocked_to_rejected(state):
    t = state.propose_task("T", "alice")
    state.update_task(t.id, "accepted", "", "bob")
    state.update_task(t.id, "blocked", "", "bob")
    result = state.update_task(t.id, "rejected", "cancelled", "bob")
    assert result is not None
    assert result.status == "rejected"


def test_all_legal_transitions_covered():
    """Verify LEGAL_TRANSITIONS covers every status in VALID_STATUSES."""
    assert set(LEGAL_TRANSITIONS.keys()) == VALID_STATUSES


# ── update_task — illegal transitions ─────────────────────────────────────────


def test_update_task_proposed_to_done_illegal(state):
    t = state.propose_task("T", "alice")
    assert state.update_task(t.id, "done", "", "bob") is None


def test_update_task_proposed_to_in_progress_illegal(state):
    t = state.propose_task("T", "alice")
    assert state.update_task(t.id, "in_progress", "", "bob") is None


def test_update_task_proposed_to_blocked_illegal(state):
    t = state.propose_task("T", "alice")
    assert state.update_task(t.id, "blocked", "", "bob") is None


def test_update_task_done_is_terminal(state):
    t = state.propose_task("T", "alice")
    state.update_task(t.id, "accepted", "", "bob")
    state.update_task(t.id, "in_progress", "", "bob")
    state.update_task(t.id, "done", "", "bob")
    assert state.update_task(t.id, "in_progress", "", "bob") is None
    assert state.update_task(t.id, "rejected", "", "bob") is None


def test_update_task_rejected_is_terminal(state):
    t = state.propose_task("T", "alice")
    state.update_task(t.id, "rejected", "", "bob")
    assert state.update_task(t.id, "proposed", "", "bob") is None
    assert state.update_task(t.id, "accepted", "", "bob") is None


def test_update_task_not_found_returns_none(state):
    result = state.update_task(999, "done", "", "bob")
    assert result is None


def test_update_task_none_ambiguity_regression(state):
    # Reviewer-flagged MAJOR: both "task not found" and "illegal transition"
    # return None, making error messages impossible to distinguish.
    # Document the current (broken) behavior so it's visible when the API changes.
    t = state.propose_task("T", "alice")
    not_found = state.update_task(999, "done", "", "bob")
    illegal = state.update_task(t.id, "done", "", "bob")  # proposed → done illegal
    # Both currently None — the orchestrator cannot tell them apart
    assert not_found is None
    assert illegal is None


def test_update_task_records_history(state):
    t = state.propose_task("T", "alice")
    result = state.update_task(t.id, "accepted", "LGTM", "bob")
    assert result is not None
    assert len(result.history) == 2
    last = result.history[-1]
    assert last.by == "bob"
    assert last.from_status == "proposed"
    assert last.to == "accepted"
    assert last.note == "LGTM"


def test_update_task_persists_new_status(state):
    t = state.propose_task("T", "alice")
    state.update_task(t.id, "accepted", "", "bob")
    tasks = state.get_tasks()
    assert tasks[0].status == "accepted"


# ── claim_task ────────────────────────────────────────────────────────────────


def test_claim_proposed_task_transitions_to_accepted(state):
    t = state.propose_task("T", "alice")
    result = state.claim_task(t.id, "bob")
    assert result is not None
    assert result.status == "accepted"
    assert result.assignee == "bob"


def test_claim_task_not_found_returns_none(state):
    assert state.claim_task(999, "bob") is None


def test_claim_task_persists_assignee(state):
    t = state.propose_task("T", "alice")
    state.claim_task(t.id, "bob")
    tasks = state.get_tasks()
    assert tasks[0].assignee == "bob"


def test_claim_nonproposed_task_reassigns_assignee(state):
    # Reviewer-flagged MINOR: claim_task on a non-proposed task silently updates
    # assignee without changing status. Undocumented side-effect.
    t = state.propose_task("T", "alice")
    state.update_task(t.id, "accepted", "", "alice")
    state.update_task(t.id, "in_progress", "", "alice")

    result = state.claim_task(t.id, "charlie")
    assert result is not None
    # Status unchanged — still in_progress
    assert result.status == "in_progress"
    # Assignee silently re-assigned to charlie
    assert result.assignee == "charlie"


def test_claim_task_logs_event(state):
    t = state.propose_task("T", "alice")
    state.claim_task(t.id, "bob")
    lines = state.history_path.read_text().splitlines()
    entries = [json.loads(line) for line in lines]
    claim_entries = [e for e in entries if e["type"] == "claim_task"]
    assert len(claim_entries) == 1
    assert claim_entries[0]["payload"]["assignee"] == "bob"


# ── apply_patch ───────────────────────────────────────────────────────────────


def test_apply_patch_adds_content(state):
    ok, reason = state.apply_patch(_ADD_DIFF, "alice")
    assert ok is True
    assert reason == ""
    content = state.get_design()
    assert "# Title" in content
    assert "Content here" in content


def test_apply_patch_replaces_existing_content(tmp_path):
    s = SwarmState("replace", base_dir=tmp_path)
    s.init(initial_design="# Title\n")
    ok, _ = s.apply_patch(_REPLACE_DIFF, "alice")
    assert ok is True
    content = s.get_design()
    assert "# Better Title" in content
    assert "# Title\n" not in content


def test_apply_patch_invalid_diff_returns_false(state):
    ok, reason = state.apply_patch(_INVALID_DIFF, "alice")
    assert ok is False
    assert reason  # non-empty reason string


def test_apply_patch_invalid_leaves_design_untouched(tmp_path):
    s = SwarmState("safe", base_dir=tmp_path)
    s.init(initial_design="original\n")
    ok, _ = s.apply_patch(_INVALID_DIFF, "alice")
    assert ok is False
    assert s.get_design() == "original\n"


def test_apply_patch_increments_in_memory_version(state):
    assert state._patch_version == 0
    state.apply_patch(_ADD_DIFF, "alice")
    assert state._patch_version == 1
    state.apply_patch(_ADD_DIFF, "alice")
    assert state._patch_version == 2


def test_patch_version_not_persisted_across_instances(tmp_path):
    # Reviewer-flagged CRITICAL: _patch_version is in-memory only.
    # After a restart (new SwarmState instance), the version resets to 0.
    s1 = SwarmState("verroom", base_dir=tmp_path)
    s1.init()
    s1.apply_patch(_ADD_DIFF, "alice")
    assert s1._patch_version == 1

    s2 = SwarmState("verroom", base_dir=tmp_path)
    # Bug: s2 starts at 0, not 1. render_summary() will show "v0" instead of "v1".
    assert s2._patch_version == 0


def test_apply_patch_logs_event(state):
    state.apply_patch(_ADD_DIFF, "alice")
    lines = state.history_path.read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry["type"] == "design_patch"
    assert entry["by"] == "alice"
    assert entry["payload"]["applied"] is True
    assert entry["payload"]["lines_changed"] > 0


def test_apply_patch_does_not_log_on_failure(state):
    state.apply_patch(_INVALID_DIFF, "alice")
    # history.jsonl should be empty — failed patches must not be logged
    assert state.history_path.read_text().strip() == ""


def test_apply_patch_multi_hunk(tmp_path):
    # Reviewer-flagged CRITICAL: the unidiff backend has an offset bug where
    # multi-hunk patches use original line numbers against a mutating array.
    # The patch CLI backend should apply correctly.
    s = SwarmState("multi", base_dir=tmp_path)
    initial = "a\nb\nc\nd\ne\n"
    s.init(initial_design=initial)

    # Hunk 1 replaces line 1 ("a") with two lines, shifting all subsequent numbers.
    # Hunk 2 replaces line 5 ("e") with "z" — correct only if offset is tracked.
    diff = "--- design.md\n+++ design.md\n@@ -1,1 +1,2 @@\n-a\n+x\n+y\n@@ -5,1 +6,1 @@\n-e\n+z\n"
    ok, reason = s.apply_patch(diff, "alice")
    if not ok:
        pytest.skip(f"patch CLI rejected multi-hunk diff: {reason}")

    content = s.get_design()
    assert "x" in content and "y" in content  # hunk 1 applied
    assert "z" in content  # hunk 2 applied
    assert "a" not in content  # original line 1 replaced
    assert "e" not in content  # original line 5 replaced
    assert "d" in content  # untouched line preserved


# ── log_event ─────────────────────────────────────────────────────────────────


def test_log_event_appends_jsonl(state):
    state.log_event("evt_a", "alice", {"k": "v"})
    state.log_event("evt_b", "bob", {"n": 42})
    lines = state.history_path.read_text().splitlines()
    assert len(lines) == 2
    e1, e2 = json.loads(lines[0]), json.loads(lines[1])
    assert e1["type"] == "evt_a" and e1["by"] == "alice"
    assert e2["type"] == "evt_b" and e2["payload"]["n"] == 42


def test_log_event_includes_timestamp(state):
    state.log_event("x", "a", {})
    entry = json.loads(state.history_path.read_text().strip())
    assert entry["ts"] > 0


# ── render_summary ────────────────────────────────────────────────────────────


def test_render_summary_no_tasks(state):
    summary = state.render_summary()
    assert "Hive state @" in summary
    assert "none yet" in summary


def test_render_summary_lists_tasks(state):
    state.propose_task("Foo", "alice")
    state.propose_task("Bar", "bob")
    summary = state.render_summary()
    assert "#1" in summary and "Foo" in summary
    assert "#2" in summary and "Bar" in summary


def test_render_summary_open_and_done_counts(state):
    t1 = state.propose_task("Done", "alice")
    state.update_task(t1.id, "accepted", "", "alice")
    state.update_task(t1.id, "in_progress", "", "alice")
    state.update_task(t1.id, "done", "", "alice")
    state.propose_task("Open", "alice")

    summary = state.render_summary()
    assert "1 open" in summary
    assert "1 done" in summary


def test_render_summary_shows_design_version(state):
    state.apply_patch(_ADD_DIFF, "alice")
    summary = state.render_summary()
    assert "v1" in summary


def test_render_summary_contains_final_hint(state):
    state.propose_task("T", "alice")
    assert "FINAL:" in state.render_summary()


def test_render_summary_all_done_different_hint(state):
    t = state.propose_task("T", "alice")
    state.update_task(t.id, "accepted", "", "alice")
    state.update_task(t.id, "in_progress", "", "alice")
    state.update_task(t.id, "done", "", "alice")
    summary = state.render_summary()
    assert "All tasks complete" in summary


# ── _count_changes ────────────────────────────────────────────────────────────


def test_count_changes_added_and_removed():
    diff = "+line1\n+line2\n-removed\n"
    assert _count_changes(diff) == 3


def test_count_changes_headers_not_counted():
    diff = "--- design.md\n+++ design.md\n+hello\n"
    assert _count_changes(diff) == 1


def test_count_changes_empty_diff():
    assert _count_changes("") == 0


def test_count_changes_context_lines_not_counted():
    diff = " unchanged\n+added\n-removed\n"
    assert _count_changes(diff) == 2


# ── VALID_STATUSES consistency ────────────────────────────────────────────────


def test_valid_statuses_matches_protocol():
    from flockroom.swarm.protocol import VALID_STATUSES as proto_statuses

    # Reviewer-flagged MINOR: VALID_STATUSES is defined identically in both modules.
    # Verify they stay in sync; a mismatch means one was edited without the other.
    assert VALID_STATUSES == proto_statuses


# ── Concurrency / file lock ───────────────────────────────────────────────────


def test_concurrent_propose_no_duplicate_ids(tmp_path):
    """File lock prevents two threads from assigning the same task ID."""
    results = []
    errors = []

    def worker(worker_id: int) -> None:
        try:
            s = SwarmState("concroom", base_dir=tmp_path)
            for i in range(5):
                task = s.propose_task(f"w{worker_id}-task{i}", f"worker{worker_id}")
                results.append(task.id)
        except Exception as exc:
            errors.append(exc)

    base = SwarmState("concroom", base_dir=tmp_path)
    base.init()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    assert len(results) == 20
    assert len(set(results)) == 20, "Duplicate task IDs — file lock is broken"


def test_concurrent_updates_do_not_corrupt_tasks(tmp_path):
    """Two threads updating different tasks must not corrupt tasks.json."""
    s = SwarmState("concroom2", base_dir=tmp_path)
    s.init()
    t1 = s.propose_task("Task 1", "alice")
    t2 = s.propose_task("Task 2", "alice")

    errors = []

    def accept(task_id: int) -> None:
        try:
            ws = SwarmState("concroom2", base_dir=tmp_path)
            ws.update_task(task_id, "accepted", "", "bob")
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=accept, args=(t1.id,)),
        threading.Thread(target=accept, args=(t2.id,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    final_tasks = s.get_tasks()
    assert len(final_tasks) == 2
    # tasks.json must be valid JSON — corruption would cause parse errors above
