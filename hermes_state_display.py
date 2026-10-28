"""Transactional maintenance and snapshot reads of the derived display index."""

from __future__ import annotations

from hermes_state_common import _placeholders
from hermes_state_compaction_display import not_a_later_tool_copy_sql


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


def legacy_projection(db, conn, session_id, active_clause):
    """Keep only fixed-width keys and row addresses while projecting an unindexed snapshot."""
    columns = set(db._message_column_names(conn))
    extra = ", ".join(name if name in columns else f"NULL AS {name}"
                      for name in ("message_uid", "display_order", "display_identity", "_compressed_summary"))
    rows = conn.execute(
        "SELECT id, role, content, timestamp, tool_call_id, tool_calls, tool_name, active, "
        f"display_kind, display_metadata, {extra} FROM messages "
        f"WHERE session_id = ?{active_clause} ORDER BY id", (session_id,))
    representatives, by_uid = {}, {}
    for row in rows:
        if db._is_model_only_row(row):
            continue
        identity = db._fold_summary_carrier(
            row, db._display_identity(db._display_dedupe_key(row)), by_uid)
        order = row["display_order"] if row["display_identity"] == identity else None
        order = row["id"] if order is None else order
        current = representatives.get(identity)
        candidate = (row["active"], row["id"])
        if current is None:
            representatives[identity] = (*candidate, order)
        else:
            representatives[identity] = (*max(candidate, current[:2]), min(current[2], order))
    return [(value[1], value[2], key) for key, value in sorted(
        representatives.items(), key=lambda item: item[1][2])]


def legacy_page(db, conn, session_id, *, active_clause, limit, offset, latest, count_only=False):
    projection = legacy_projection(db, conn, session_id, active_clause)
    if count_only:
        return len(projection)
    page = projection[::-1][offset:][:limit][::-1] if latest else projection[offset:][:limit]
    selected = {}
    for start in range(0, len(page), 900):
        ids = [item[0] for item in page[start:start + 900]]
        selected.update({row["id"]: dict(row) for row in conn.execute(
            f"SELECT * FROM messages WHERE session_id = ? AND id IN ({_placeholders(ids)})", (session_id, *ids))})
    for row_id, order, identity in page:
        selected[row_id].update(display_order=order, display_identity=identity)
    return [selected[row_id] for row_id, _, _ in page]


def display_page(db, conn, session_id, *, limit=None, offset=0, latest=False, count_only=False):
    from hermes_state_identity import user_display_index_current
    from hermes_state_messages import _DISPLAY_INDEX_MISSING_SQL, _DISPLAY_ACTIVE_CLAUSE, DISPLAY_VISIBLE_SQL

    conn.execute("BEGIN")
    try:
        columns = set(db._message_column_names(conn))
        indexed = ({"display_order", "display_identity"} <= columns and user_display_index_current(conn)
                   and conn.execute(_DISPLAY_INDEX_MISSING_SQL, (session_id,)).fetchone() is None)
        if not indexed:
            return legacy_page(db, conn, session_id, active_clause=_DISPLAY_ACTIVE_CLAUSE,
                               limit=limit, offset=offset, latest=latest, count_only=count_only)
        if count_only:
            return conn.execute("SELECT COUNT(*) FROM (SELECT DISTINCT display_order FROM messages "
                                f"WHERE session_id = ?{_DISPLAY_ACTIVE_CLAUSE}{DISPLAY_VISIBLE_SQL}"
                                f"{not_a_later_tool_copy_sql('messages')})",
                                (session_id,)).fetchone()[0]
        return db._display_rows_from_conn(conn, session_id, limit=limit, offset=offset, latest=latest)
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
