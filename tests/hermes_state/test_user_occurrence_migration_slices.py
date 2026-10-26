"""The derived user index advances in bounded, restartable slices."""

import time
from types import SimpleNamespace

import hermes_state_schema
from hermes_state import SessionDB


def test_occurrence_migration_resumes_without_rewriting_canonical_rows(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("s", source="cli")
    for i in range(6):
        db.append_messages_batch("s", [{"role": "user", "content": "same", "timestamp": 1,
                                        "message_uid": "carried" if i < 3 else f"new-{i}"}])
    db._execute_write(lambda c: (
        c.execute("UPDATE messages SET display_order = 1, display_identity = X'00'"),
        c.execute("DELETE FROM state_meta WHERE key LIKE 'display_user_occurrence_%'")))
    reader = SessionDB(path, read_only=True)
    try:
        assert reader.display_message_count("s") == 4
    finally:
        reader.close()
    before = db._read_all("SELECT id, content, timestamp, message_uid, active, compacted FROM messages ORDER BY id")
    db.close()
    monkeypatch.setattr(hermes_state_schema, "_MESSAGE_UID_BACKFILL_CHUNK", 2)
    clock = [0]
    repair = hermes_state_schema.repair_user_display_slice

    def expire_after_slice(*args):
        repair(*args)
        clock[0] = 10000000000

    monkeypatch.setattr(hermes_state_schema, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(hermes_state_schema, "repair_user_display_slice", expire_after_slice)
    db = SessionDB(path)
    try:
        assert db.get_meta("display_user_occurrence_cursor") == "2"
        assert db.get_meta("display_user_occurrence_version") is None
        # A partial index must never hide a later occurrence during paging.
        assert db.display_message_count("s") == 4
    finally:
        db.close()
    monkeypatch.undo()
    db = SessionDB(path)
    try:
        assert db.get_meta("display_user_occurrence_version") == "1"
        assert db.get_meta("display_user_occurrence_cursor") is None
        after = db._read_all("SELECT id, content, timestamp, message_uid, active, compacted FROM messages ORDER BY id")
        assert [tuple(row) for row in before] == [tuple(row) for row in after]
        shown = db.get_messages("s", include_compacted=True)
        assert len(shown) == 4
        assert db.display_message_count("s") == 4
    finally:
        db.close()
