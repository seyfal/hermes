"""Peer messages through real recipient turns: a real ``AIAgent`` with a real ``SessionDB`` in a temp
``HERMES_HOME``, a scripted model, and ``peer_send`` through the tool registry. Contracts: a peer message
is never lost and never becomes the user's next turn."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from model_tools import handle_function_call
from tools.registry import registry

SENDER, RECIPIENT = "sender-session", "recipient-session"
PEER_BODY = "PEER_BODY: the schema migration is merged"
# A recipient tool during which the peer sends: the message arrives while the recipient is busy.
SEND_DURING_TOOL = "peer_turn_probe_send"
_PROBE_SCHEMA = {"name": SEND_DURING_TOOL, "description": "test probe",
                 "parameters": {"type": "object", "properties": {}, "required": []}}
registry.register(name=SEND_DURING_TOOL, toolset="utility", schema=_PROBE_SCHEMA,
                  handler=lambda args, **kw: _send() or "probe ok", override=True)
QUIET_TOOL = "peer_turn_probe_quiet"
_QUIET_SCHEMA = {**_PROBE_SCHEMA, "name": QUIET_TOOL}
registry.register(name=QUIET_TOOL, toolset="utility", schema=_QUIET_SCHEMA,
                  handler=lambda args, **kw: "quiet ok", override=True)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    db.create_session(SENDER, source="cli")
    db.create_session(RECIPIENT, source="cli")
    yield SimpleNamespace(path=home, db=db)
    db.close()


def _send(body: str = PEER_BODY) -> None:
    result = json.loads(handle_function_call(
        "peer_send", {"target_session_id": RECIPIENT, "message": body}, session_id=SENDER))
    assert result.get("success") is True, result


def _queued() -> list:
    import tools.peer_messaging_tool as pm
    inbox = pm._inbox_dir(RECIPIENT)
    return sorted(inbox.glob("*.json")) if inbox.is_dir() else []


def _stored_text(db: SessionDB) -> str:
    return "\n".join(str(row.get("content")) for row in db.get_messages_as_conversation(RECIPIENT))


def _recipient(db: SessionDB, replies: list, payloads: list, on_request=None):
    """A recipient agent whose model returns ``replies`` in order (text, or a probe tool name for a
    call of that tool) and records every request; ``on_request()`` runs as each request is made."""
    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[
            {"type": "function", "function": schema} for schema in (_PROBE_SCHEMA, _QUIET_SCHEMA)]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            session_db=db, session_id=RECIPIENT,
        )
    agent._cached_system_prompt = "You are helpful."
    return _script(agent, replies, payloads, on_request)


def _script(agent, replies: list, payloads: list, on_request=None):
    """Route ``agent``'s model calls to ``replies`` (see ``_recipient``), recording each request."""
    from tests.agent.test_run_agent import _mock_response

    agent.client = MagicMock()
    agent._disable_streaming = True
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False

    def model_call(api_kwargs):
        payloads.append([dict(m) for m in api_kwargs["messages"]])
        if on_request is not None:
            on_request()
        reply = replies.pop(0)
        if reply in (SEND_DURING_TOOL, QUIET_TOOL):
            call = SimpleNamespace(id=f"call_{len(payloads)}", type="function",
                                   function=SimpleNamespace(name=reply, arguments="{}"))
            return _mock_response(content=None, finish_reason="tool_calls", tool_calls=[call])
        return _mock_response(content=reply, finish_reason="stop")

    agent._interruptible_api_call = model_call
    return agent


def _sent_text(payloads: list) -> str:
    return "\n".join(str(m.get("content")) for call in payloads for m in call)


def test_peer_message_survives_a_turn_without_tool_calls(home):
    """A turn whose model answers without a tool call must neither drop a queued peer message nor hand it
    back as ``pending_steer``: every surface replays that as a new turn the USER typed (one-shot
    ``chat -q`` just exits and loses it)."""
    _send()
    payloads: list = []
    agent = _recipient(home.db, ["first answer"], payloads)

    result = agent.run_conversation("hello")

    assert PEER_BODY not in str(result.get("pending_steer") or "")
    assert PEER_BODY in _sent_text(payloads) or _queued()


def test_message_is_stored_before_it_leaves_the_mailbox(home):
    """A busy recipient gets the message with its next request. At every request the message is in the
    session database or still in the mailbox, so a receiver that dies at any point loses nothing."""
    durable = []
    payloads: list = []
    agent = _recipient(home.db, [SEND_DURING_TOOL, "done"], payloads,
                       on_request=lambda: durable.append(PEER_BODY in _stored_text(home.db) or bool(_queued())))
    agent.run_conversation("run the probe")

    assert PEER_BODY in _sent_text(payloads[1:])
    assert durable[1:] == [True]  # the peer sends during the tool, after the first request
    assert not _queued()


def test_row_that_cannot_be_stored_is_not_sent_and_stays_queued(home):
    """If the session database refuses the row, the request goes without it and the mailbox keeps the
    message: sending it anyway would deliver it twice once a later request stores it."""
    payloads: list = []
    agent = _recipient(home.db, [SEND_DURING_TOOL, "done"], payloads)
    flush = agent._flush_messages_to_session_db
    agent._flush_messages_to_session_db = lambda messages, *a, **kw: (
        False if PEER_BODY in str(messages[-1].get("content")) else flush(messages, *a, **kw))
    agent.run_conversation("run the probe")

    assert PEER_BODY not in _sent_text(payloads)
    assert len(_queued()) == 1
