"""Legacy semantic slots remain consistent across writable and read-only views."""

import sqlite3

import pytest

from hermes_state import SessionDB


@pytest.mark.parametrize("same_uid", [True, False])
def test_legacy_semantic_split_slots_repair_before_paging(tmp_path, same_uid):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("s", source="cli")
        db.append_message("s", "tool", "same result", timestamp=1, message_uid="first")
        db.append_message("s", "tool", "same result", timestamp=1,
                          message_uid="first" if same_uid else "second")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE messages SET display_order = id WHERE session_id = 's'")
        conn.execute("DELETE FROM state_meta WHERE key LIKE 'display_user_occurrence_%'")
    with SessionDB(path) as db:
        shown = db.get_messages("s", include_compacted=True)
        assert len(shown) == db.display_message_count("s") == 1
        assert db.get_messages("s", include_compacted=True, limit=1, offset=1) == []
