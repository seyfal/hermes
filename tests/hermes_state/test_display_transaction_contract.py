"""Transactional display repair contracts."""

import pytest

from hermes_state import SessionDB


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
