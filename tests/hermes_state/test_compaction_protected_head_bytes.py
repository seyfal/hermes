"""A preserved head and a preserved tail carry canonical bytes, not their model lens."""

from hermes_state import SessionDB


def test_protected_head_does_not_duplicate_after_compaction(tmp_path):
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("s", source="cli")
        for role, content in [
            ("user", "head ask\n"), ("assistant", "head reply\n"),
            ("user", "middle ask"), ("assistant", "middle reply"),
            ("user", "tail ask\n"), ("assistant", "tail reply\n"),
        ]:
            db.append_message("s", role, content)
        live = db.get_messages_as_conversation("s")
        before = [msg["content"] for msg in live]
        compacted = [*live[:2], {"role": "assistant", "content": "summary"}, *live[-2:]]
        db.archive_and_compact("s", compacted, tail_count=2)
        shown = db.get_messages("s", include_compacted=True)
        assert [msg["content"] for msg in live] == before
        for expected in ["head ask\n", "head reply\n", "tail ask\n", "tail reply\n"]:
            assert [msg["content"] for msg in shown].count(expected) == 1
        assert len(shown) == 7
        assert [msg["content"] for msg in db.get_messages("s")][:2] == ["head ask\n", "head reply\n"]
