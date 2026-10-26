"""Transactional maintenance and snapshot reads of the derived display index."""

from __future__ import annotations

from hermes_state_common import _placeholders


def restore_dirty_display_rows(db, conn, session_id):
    """Repair the rows invalidated by an identity UPDATE, including its former peers."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? "
        "AND (display_order IS NULL OR display_identity IS NULL) ORDER BY id",
        (session_id,),
    ).fetchall()
    for row in rows:
        from hermes_state_identity import _display_row_identity
        identity = _display_row_identity(db, conn, row)
        order = row["id"]
        peers = []
        if row["active"] or row["compacted"]:
            peers = conn.execute(
                "SELECT id, display_order FROM messages WHERE session_id = ? "
                "AND display_identity = ? AND (active = 1 OR compacted = 1)",
                (session_id, identity),
            ).fetchall()
            order = min([order] + [peer["display_order"] or peer["id"] for peer in peers])
        conn.execute("UPDATE messages SET display_identity = ?, display_order = ? WHERE id = ?",
                     (identity, order, row["id"]))
        if peers:
            ids = [peer["id"] for peer in peers]
            conn.execute(f"UPDATE messages SET display_order = ? WHERE id IN ({_placeholders(ids)})",
                         (order, *ids))
