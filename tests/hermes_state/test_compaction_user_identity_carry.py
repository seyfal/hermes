"""Compaction must recover an unaddressed carried ask, not mint a new occurrence."""

from datetime import UTC, datetime

import pytest

from agent.context_compressor import (
    ContextCompressor,
    SUMMARY_PREFIX,
    user_originated_turn_view,
)
from hermes_state import SessionDB


def _carrier(message):
    compressor = ContextCompressor(model="test/model", quiet_mode=True)
    compressor._merge_summary_into_tail_row(message, SUMMARY_PREFIX + "\nold task", "user", True)
    return message


@pytest.mark.parametrize("addressed", [False, True])
@pytest.mark.parametrize("mode", ["watermark", "coverage", "rotation"])
def test_compressor_carrier_keeps_the_original_occurrence(tmp_path, addressed, mode):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("s", source="desktop")
        original = db.append_message("s", "user", "REAL ASK", timestamp=0)
        held = db.get_messages_as_conversation("s")
        uid = held[0]["message_uid"]
        if not addressed:
            # Legacy/engine-owned histories can retain the clock without a durable UID.
            held[0].pop("message_uid")
        carrier = _carrier(held[0])
        # The persistence API also accepts datetime clocks, including epoch zero.
        if mode == "rotation":
            carrier["timestamp"] = datetime(1970, 1, 1, tzinfo=UTC)
        before = carrier["content"]
        if mode == "rotation":
            assert db.try_acquire_compression_lock("s", "test")
            db.publish_compression_child(parent_session_id="s", child_session_id="c", source="desktop",
                messages=[carrier], compression_lock_holder="test")
            sid = "c"
        else:
            db.archive_and_compact("s", [carrier], covered_ids=[original] if mode == "coverage" else None)
            sid = "s"
        shown = db.get_messages(sid, include_compacted=True, include_ancestors=True)
        assert len(shown) == 1
        assert shown[0]["message_uid"] == uid
        assert user_originated_turn_view(shown[0])["content"] == "REAL ASK"
        assert carrier["content"] == before
    finally:
        db.close()


@pytest.mark.parametrize("case", ["distinct", "ambiguous", "other-session", "clock", "authority"])
def test_compaction_never_guesses_a_carried_user_occurrence(tmp_path, case):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("s", source="desktop")
        db.create_session("other", source="desktop")
        db.append_message("other" if case == "other-session" else "s", "user", "REAL ASK",
                          timestamp=123, message_uid="original")
        if case == "ambiguous":
            db.append_message("s", "user", "REAL ASK", timestamp=123, message_uid="second")
        carrier = _carrier({"role": "user", "content": "REAL ASK", "timestamp": 123})
        if case == "distinct":
            carrier["message_uid"] = "new-ask"
        if case == "clock":
            carrier["timestamp"] = 124
        if case == "authority":
            carrier["display_kind"] = "steer"
        db.archive_and_compact("s", [carrier])
        assert carrier["message_uid"] != "original"
        if case == "distinct":
            assert carrier["message_uid"] == "new-ask"
        expected = 1 if case == "other-session" else 3 if case == "ambiguous" else 2
        assert len(db.get_messages("s", include_compacted=True)) == expected
    finally:
        db.close()
