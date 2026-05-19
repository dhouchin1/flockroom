"""Unit tests for hivechat.swarm.protocol.

Covers every event type, edge cases, and regression tests for the two
reviewer-flagged bugs:
  - DESIGN_PATCH consumes all subsequent lines (undocumented constraint)
  - Invalid UPDATE_TASK status is silently dropped with no event emitted
"""

from hivechat.swarm.protocol import (
    ClaimTask,
    Comment,
    DesignPatch,
    Final,
    ProposeTask,
    UpdateTask,
    parse,
)

# ── Empty / chatter ────────────────────────────────────────────────────────────


def test_empty_string_returns_empty():
    assert parse("") == []


def test_whitespace_only_returns_empty():
    assert parse("   \n\n  ") == []


def test_pure_chatter_returns_empty():
    assert parse("Just talking here\nNothing protocol-related") == []


def test_lowercase_prefixes_ignored():
    # Spec requires case-sensitive matching at line start
    assert parse("propose_task: Foo\nclaim_task: 1") == []


def test_prefix_not_at_line_start_ignored():
    assert parse("  PROPOSE_TASK: Indented") == []


# ── PROPOSE_TASK ───────────────────────────────────────────────────────────────


def test_propose_task_basic():
    events = parse("PROPOSE_TASK: Add auth middleware")
    assert events == [ProposeTask(title="Add auth middleware")]


def test_propose_task_strips_surrounding_whitespace():
    events = parse("PROPOSE_TASK:   Trimmed title   ")
    assert events == [ProposeTask(title="Trimmed title")]


def test_propose_task_empty_title_ignored():
    assert parse("PROPOSE_TASK:") == []
    assert parse("PROPOSE_TASK:   ") == []


def test_propose_task_title_with_colon():
    events = parse("PROPOSE_TASK: Feature: OAuth2 login")
    assert events == [ProposeTask(title="Feature: OAuth2 login")]


def test_propose_task_chatter_before_and_after():
    msg = "Hello everyone\nPROPOSE_TASK: New task\nGoodbye"
    assert parse(msg) == [ProposeTask(title="New task")]


def test_propose_task_multiple():
    msg = "PROPOSE_TASK: Task A\nPROPOSE_TASK: Task B"
    assert parse(msg) == [ProposeTask(title="Task A"), ProposeTask(title="Task B")]


# ── CLAIM_TASK ─────────────────────────────────────────────────────────────────


def test_claim_task_valid_integer():
    assert parse("CLAIM_TASK: 3") == [ClaimTask(id=3)]


def test_claim_task_strips_whitespace():
    assert parse("CLAIM_TASK:  7 ") == [ClaimTask(id=7)]


def test_claim_task_non_integer_ignored():
    assert parse("CLAIM_TASK: foo") == []


def test_claim_task_empty_ignored():
    assert parse("CLAIM_TASK:") == []


def test_claim_task_float_ignored():
    assert parse("CLAIM_TASK: 1.5") == []


def test_claim_task_negative_integer_accepted():
    # Negative int is a valid int; parser accepts it (callers should reject)
    assert parse("CLAIM_TASK: -1") == [ClaimTask(id=-1)]


# ── UPDATE_TASK ────────────────────────────────────────────────────────────────


def test_update_task_basic():
    assert parse("UPDATE_TASK: 1 done") == [UpdateTask(id=1, status="done", note="")]


def test_update_task_with_note():
    events = parse("UPDATE_TASK: 2 blocked needs more info from design team")
    assert events == [UpdateTask(id=2, status="blocked", note="needs more info from design team")]


def test_update_task_non_integer_id_ignored():
    assert parse("UPDATE_TASK: abc done") == []


def test_update_task_missing_status_ignored():
    assert parse("UPDATE_TASK: 1") == []


def test_update_task_empty_ignored():
    assert parse("UPDATE_TASK:") == []


def test_update_task_invalid_status_silently_dropped():
    # Reviewer-flagged regression: an unrecognized status produces no event and no
    # feedback. Agents that misspell a status get silently ignored.
    assert parse("UPDATE_TASK: 1 nonexistent_status") == []
    assert parse("UPDATE_TASK: 1 DONE") == []  # case-sensitive


def test_update_task_all_valid_statuses():
    valid = ["proposed", "accepted", "in_progress", "blocked", "done", "rejected"]
    for status in valid:
        events = parse(f"UPDATE_TASK: 1 {status}")
        assert events == [UpdateTask(id=1, status=status)], f"failed for status={status!r}"


# ── DESIGN_PATCH ───────────────────────────────────────────────────────────────


_SIMPLE_DIFF = "--- design.md\n+++ design.md\n@@ -0,0 +1,2 @@\n+# Title\n+Content\n"


def test_design_patch_basic():
    msg = f"DESIGN_PATCH:\n{_SIMPLE_DIFF}"
    events = parse(msg)
    assert len(events) == 1
    assert isinstance(events[0], DesignPatch)
    assert "+# Title" in events[0].diff


def test_design_patch_empty_body_ignored():
    # DESIGN_PATCH: with nothing after it should not emit an event
    assert parse("DESIGN_PATCH:") == []
    assert parse("DESIGN_PATCH:\n") == []


def test_design_patch_consumes_all_subsequent_lines():
    # Reviewer-flagged regression: once DESIGN_PATCH: is encountered, every
    # remaining line is treated as diff content, swallowing any later directives.
    msg = f"DESIGN_PATCH:\n{_SIMPLE_DIFF}\nPROPOSE_TASK: This line is swallowed into the diff"
    events = parse(msg)
    assert len(events) == 1
    assert isinstance(events[0], DesignPatch)
    assert "PROPOSE_TASK: This line is swallowed into the diff" in events[0].diff


def test_design_patch_after_other_directives():
    # Directives BEFORE DESIGN_PATCH are parsed normally
    msg = "PROPOSE_TASK: Before\nDESIGN_PATCH:\n+line\nPROPOSE_TASK: After (eaten)"
    events = parse(msg)
    assert len(events) == 2
    assert isinstance(events[0], ProposeTask)
    assert events[0].title == "Before"
    assert isinstance(events[1], DesignPatch)
    assert "PROPOSE_TASK: After (eaten)" in events[1].diff


# ── COMMENT ────────────────────────────────────────────────────────────────────


def test_comment_basic():
    assert parse("COMMENT: This looks good") == [Comment(text="This looks good")]


def test_comment_empty_body():
    assert parse("COMMENT:") == [Comment(text="")]


def test_comment_stripped():
    assert parse("COMMENT:  note  ") == [Comment(text="note")]


# ── FINAL ──────────────────────────────────────────────────────────────────────


def test_final_no_summary():
    assert parse("FINAL:") == [Final(text="")]


def test_final_with_summary():
    assert parse("FINAL: Design complete") == [Final(text="Design complete")]


def test_final_stripped():
    assert parse("FINAL:   done  ") == [Final(text="done")]


# ── Multi-event messages ───────────────────────────────────────────────────────


def test_two_events_in_order():
    msg = "PROPOSE_TASK: Foo\nCLAIM_TASK: 1"
    assert parse(msg) == [ProposeTask(title="Foo"), ClaimTask(id=1)]


def test_all_event_types_before_design_patch():
    msg = (
        "PROPOSE_TASK: do something\n"
        "CLAIM_TASK: 1\n"
        "UPDATE_TASK: 2 done finished\n"
        "COMMENT: looks good\n"
        "FINAL: session over"
    )
    events = parse(msg)
    assert len(events) == 5
    assert isinstance(events[0], ProposeTask)
    assert isinstance(events[1], ClaimTask)
    assert isinstance(events[2], UpdateTask)
    assert isinstance(events[3], Comment)
    assert isinstance(events[4], Final)


def test_chatter_interspersed():
    msg = (
        "Hey team, I'm starting work.\n"
        "PROPOSE_TASK: Write API client\n"
        "Will start right away.\n"
        "CLAIM_TASK: 1\n"
        "OK I'm on it."
    )
    events = parse(msg)
    assert events == [ProposeTask(title="Write API client"), ClaimTask(id=1)]


def test_update_task_with_chatter_and_valid_status():
    msg = "Some context\nUPDATE_TASK: 3 in_progress working on it now\nDone."
    events = parse(msg)
    assert events == [UpdateTask(id=3, status="in_progress", note="working on it now")]
