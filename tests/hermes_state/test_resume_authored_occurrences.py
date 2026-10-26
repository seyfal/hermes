"""Resume must not erase authored occurrence identity with a later text heuristic."""

import pytest

from hermes_state import SessionDB


@pytest.mark.parametrize("uid", ["first", "second"])
@pytest.mark.parametrize("display", [False, True])
def test_lineage_resume_keeps_identical_asks_with_distinct_uids(tmp_path, uid, display):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", source="cli")
    db.append_messages_batch("root", [
        {"role": "user", "content": "again", "timestamp": 10, "message_uid": "first"},
        {"role": "assistant", "content": "answer", "timestamp": 11},
    ])
    db.end_session("root", "compression")
    db.create_session("child", source="cli", parent_session_id="root")
    db.append_messages_batch("child", [
        {"role": "user", "content": "again", "timestamp": 10, "message_uid": uid},
    ])
    try:
        replay = db.get_messages_as_conversation("child", include_ancestors=True, include_compacted=display,
                                                 repair_alternation=False)
        expected = ["first", "second"] if uid == "second" else ["first"]
        assert [m["message_uid"] for m in replay if m["role"] == "user"] == expected
    finally:
        db.close()
