"""Identity carry for re-inserted transcript rows: a compaction or rotation hands its carried tail back as
message dicts, and these keep each copy the same logical message as the durable original it repeats (same
display identity, same text), so display reads fold the generations and nothing the user wrote is edited."""

from __future__ import annotations

import json
import logging
from typing import Any

from hermes_cli.timefmt import coerce_epoch
from hermes_state_messages import _parse_tool_calls, _scrub_surrogates

logger = logging.getLogger("hermes_state")  # caplog tests pin the origin module's name


class SessionCarryMixin:
    """Carry the durable original's identity onto re-inserted carried rows (mixed into ``SessionDB``)."""

    def _carry_parent_timestamps(self, conn, parent_session_id: str, messages: list[dict[str, Any]]) -> None:
        """Adopt the durable parent row's timestamp onto carried handoff rows so the re-inserted child row keeps
        the original's display identity (see _display_dedupe_key). Idempotent; never overwrites a timestamp the
        dict already carries; rows with no match keep their caller-supplied/now_ts value.

        Content identity = _display_dedupe_key's content key minus the timestamp (role, encoded content,
        tool_call_id, encoded tool_calls, tool_name). Donors are the parent's ACTIVE rows, consumed first-match-wins in id
        order so two identical-content turns cannot both grab the same parent row. Best-effort: on any error
        the insert falls back to today's behavior (publish-time stamp) and never breaks publish.
        """
        from hermes_state_identity import carry_unaddressed_user_uids

        carry_unaddressed_user_uids(self, conn, parent_session_id, messages)
        try:
            candidates = [i for i, msg in enumerate(messages)
                if isinstance(msg, dict) and coerce_epoch(msg.get("timestamp"), field="message timestamp") is None]
            if not candidates:
                return
            donors: dict[tuple[Any, ...], list[Any]] = {}
            for row in conn.execute(
                    "SELECT id, role, content, timestamp, tool_call_id, tool_calls, tool_name FROM messages "
                    "WHERE session_id = ? AND active = 1 ORDER BY id", (parent_session_id,)).fetchall():
                donors.setdefault((row["role"], row["content"], row["tool_call_id"], row["tool_calls"],
                    row["tool_name"]), []).append(row["timestamp"])
            for i in candidates:
                msg = messages[i]
                tool_calls = _parse_tool_calls(msg.get("tool_calls"))
                key = (msg.get("role", "unknown"), self._encode_content(msg.get("content")),
                    msg.get("tool_call_id"), json.dumps(tool_calls) if tool_calls else None,
                    _scrub_surrogates(msg.get("tool_name")))
                queue = donors.get(key)
                if queue:
                    msg["timestamp"] = queue.pop(0)
        except Exception:
            logger.debug("Could not carry parent transcript timestamps", exc_info=True)
            return  # Best-effort: a failed carry falls back to the publish-time stamp, never breaks publish.

    def _carried_message(self, conn, source_session_id, msg):
        """Restore only safe whitespace at serialization, using a row in the proven source segment."""
        from agent.message_metadata import DB_ROW_SNAPSHOT, message_uid_or_none
        from agent.transcript_repair import transcript_row_snapshot
        from hermes_state_messages import sanitize_context

        uid = message_uid_or_none(msg)
        if not uid or msg.get("role") not in ("user", "assistant") or not isinstance(msg.get("content"), str):
            return msg
        row_id = msg.get("_row_id")
        snapshot = msg.get(DB_ROW_SNAPSHOT)
        if isinstance(row_id, int):
            row = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND id = ? AND message_uid = ?",
                (source_session_id, row_id, uid),
            ).fetchone()
        elif isinstance(snapshot, str) or msg.get("timestamp") is not None:
            row = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND message_uid = ? AND role = ? "
                "AND timestamp IS ? ORDER BY id DESC LIMIT 1",
                (source_session_id, uid, msg["role"], msg.get("timestamp")),
            ).fetchone()
        else:
            return msg
        if row is None or row["role"] != msg["role"]:
            return msg
        if isinstance(snapshot, str) and transcript_row_snapshot(row) != snapshot:
            return msg
        stored = self._decode_content(row["content"])
        # Sanitized model text must never resurrect removed instructions or control bytes.
        if (isinstance(stored, str) and stored != msg["content"]
                and sanitize_context(stored) == stored and stored.strip() == msg["content"]):
            return {**msg, "content": stored}
        return msg
