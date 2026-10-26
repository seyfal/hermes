"""Display snapshots remain coherent across old writers and compaction generations."""

import copy

import pytest

from hermes_state import SessionDB
from hermes_state_timeline import get_session_messages_around, get_session_timeline


def test_legacy_writer_between_probe_and_page_keeps_every_row(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", source="desktop")
    db.append_message("s", "user", "first")
    db.append_message("s", "assistant", "reply")
    ensure = db._ensure_display_order

    def old_writer(sid):
        ready = ensure(sid)
        db._execute_write(lambda conn: conn.execute(
            "UPDATE messages SET content = content || ' expanded' WHERE session_id = ?", (sid,)))
        return ready

    monkeypatch.setattr(db, "_ensure_display_order", old_writer)
    try:
        assert [m["role"] for m in db.get_messages("s", include_compacted=True)] == ["user", "assistant"]
        assert db.display_message_count("s") == 2
    finally:
        db.close()


def test_adoption_only_does_not_refold_transcript(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", source="cli")
    row = db.append_message("s", "assistant", "settled")
    def unexpected(*args):
        pytest.fail("adoption must not rescan transcript payloads")
    monkeypatch.setattr(db, "_reconcile_display_orders", unexpected)
    try:
        assert db.append_messages_batch("s", [{"role": "assistant", "content": "settled", "_row_id": row}]) == 0
    finally:
        db.close()


@pytest.mark.parametrize("read_only", [False, True])
def test_legacy_same_semantic_slots_agree_before_pagination(tmp_path, read_only):
    # Legacy rows can retain separate slots for one carried reply.
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("s", source="desktop")
    db.append_message("s", "user", "question", timestamp=1)
    old = db.append_message("s", "assistant", "same reply", timestamp=2, message_uid="reply")
    db._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET active = 0, compacted = 1 WHERE id = ?", (old,)))
    new = db.append_message("s", "assistant", "same reply", timestamp=2, message_uid="reply")
    db._execute_write(lambda conn: (
        conn.execute("UPDATE messages SET display_order = id, display_identity = zeroblob(32) WHERE id = ?", (new,)),
        conn.execute("DELETE FROM state_meta WHERE key = 'display_user_occurrence_version'")))
    db.close()
    db = SessionDB(path, read_only=read_only)
    try:
        whole = db.get_messages("s", include_compacted=True)
        assert [m["id"] for m in whole] == [old - 1, new]
        pages = [m for i in range(3) for m in db.get_messages("s", include_compacted=True, limit=1, offset=i)]
        assert pages == whole
        assert db.display_message_count("s") == len(whole)
        assert db.get_messages("s", include_compacted=True, latest=True, limit=1) == whole[-1:]
        timeline = get_session_timeline(db, "s")
        around = get_session_messages_around(db, "s", timeline["entries"][0]["row_id"])
        assert around["messages"] == whole
    finally:
        db.close()


@pytest.mark.parametrize("rotation", [False, True])
@pytest.mark.parametrize("covered", [False, True])
def test_carried_bytes_do_not_change_model_view(tmp_path, rotation, covered):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", source="cli")
    db.create_session("unrelated", source="cli")
    db.append_message("unrelated", "assistant", "answer\n\n", message_uid="reply", timestamp=0)
    db.append_message("s", "user", "ask\n", timestamp=0)
    db.append_message("s", "assistant", "answer\n", message_uid="reply", timestamp=0)
    live = db.get_messages_as_conversation("s")
    content = copy.deepcopy([m["content"] for m in live])
    try:
        if rotation:
            db.publish_compression_child(parent_session_id="s", child_session_id="child", messages=live,
                                         source="cli", require_compression_lease=False)
            target = "child"
        else:
            kwargs = {"covered_ids": [m["id"] for m in db.get_messages("s")]} if covered else {}
            db.archive_and_compact("s", live, **kwargs)
            target = "s"
        assert [m["content"] for m in live] == content
        assert [m["content"] for m in db.get_messages(target)] == ["ask\n", "answer\n"]
        assert [m["timestamp"] for m in db.get_messages(target)] == [0, 0]
        assert len(db.get_messages(target, include_compacted=True, include_ancestors=True)) == 2
    finally:
        db.close()
