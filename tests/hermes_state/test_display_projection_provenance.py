"""Producer provenance and legacy projection controls."""

import pytest

from hermes_state import SessionDB
from hermes_state_timeline import get_session_messages_around, get_session_timeline


@pytest.mark.parametrize("read_only", [False, True])
def test_image_generations_keep_authored_occurrences_and_tool_history(tmp_path, read_only):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("s", source="desktop")
    rows = [
        {"role": "user", "content": "look", "message_uid": "ask", "timestamp": 1},
        {"role": "assistant", "content": "checking", "timestamp": 2},
        {"role": "tool", "content": "full result", "message_uid": "tool", "timestamp": 3},
    ]
    db.append_messages_batch("s", rows)
    db._execute_write(lambda c: c.execute("UPDATE messages SET active = 0, compacted = 1"))
    db.append_messages_batch("s", [
        {"role": "user", "content": [{"type": "text", "text": "look"},
                                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}],
         "message_uid": "ask", "timestamp": 1},
        {"role": "assistant", "content": "checking", "timestamp": 2},
        {"role": "tool", "content": "stub", "message_uid": "tool", "timestamp": 3},
        {"role": "user", "content": "look", "message_uid": "fresh", "timestamp": 1},
    ])
    db._execute_write(lambda c: (
        c.execute("UPDATE messages SET display_order = NULL, display_identity = NULL"),
        c.execute("DELETE FROM state_meta WHERE key = 'display_user_occurrence_version'")))
    db.close()
    db = SessionDB(path, read_only=read_only)
    try:
        shown = db.get_messages("s", include_compacted=True)
        assert [m["message_uid"] for m in shown if m["role"] == "user"] == ["ask", "fresh"]
        assert [m["content"] for m in shown if m["role"] == "tool"] == ["full result", "stub"]
        assert db.display_message_count("s") == len(shown)
        assert [m for i in range(len(shown)) for m in db.get_messages(
            "s", include_compacted=True, limit=1, offset=i)] == shown
        prompts = get_session_timeline(db, "s")["entries"]
        assert len(prompts) == 2
        assert get_session_messages_around(db, "s", prompts[0]["row_id"])["messages"] == shown
        exported = db.export_session("s", include_compacted=True)["messages"]
        assert [m["content"] for m in exported] == [m["content"] for m in shown]
        assert all(len(m["display_identity"]) == 64 for m in shown)
    finally:
        db.close()


@pytest.mark.parametrize("rotation", [False, True])
def test_carry_never_restores_sanitized_context(tmp_path, rotation):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", source="cli")
    db.append_message("s", "user", "ask")
    db.append_message("s", "assistant", "answer<memory-context>private instructions</memory-context>\n")
    live = db.get_messages_as_conversation("s", include_row_ids=True)
    assert live[-1]["content"] == "answer"
    try:
        if rotation:
            db.publish_compression_child(parent_session_id="s", child_session_id="child", source="cli",
                                         messages=live, require_compression_lease=False)
            target = "child"
        else:
            db.archive_and_compact("s", live, tail_count=2)
            target = "s"
        assert db.get_messages(target)[-1]["content"] == "answer"
        assert live[-1]["content"] == "answer"
    finally:
        db.close()
