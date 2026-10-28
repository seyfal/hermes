"""Replay preserves durable clocks independently of the model-facing content."""

import copy

import pytest

from gateway.run import _build_gateway_agent_history


@pytest.mark.parametrize("timestamp", [0, 1700000000.0])
def test_every_replay_role_keeps_timestamp_and_coverage(timestamp):
    rows = [
        {"role": "user", "content": "ask"},
        {"role": "assistant", "content": "checking", "tool_calls": [
            {"id": "call-a", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "content": "result", "tool_call_id": "call-a"},
        {"role": "assistant", "content": "answer"},
    ]
    for i, row in enumerate(rows):
        row.update(timestamp=timestamp, message_uid=f"row-{i}", _db_persisted=True, _row_id=i + 1)
    original = copy.deepcopy(rows)
    replay, _ = _build_gateway_agent_history(rows)
    assert [row.get("timestamp") for row in replay] == [timestamp] * len(rows)
    assert [row["message_uid"] for row in replay] == [row["message_uid"] for row in rows]
    assert all(row["_db_persisted"] for row in replay)
    assert rows == original
