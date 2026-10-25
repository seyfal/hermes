"""Peer session messaging (tools/peer_messaging_tool.py) — invariant tests.

1. End-to-end mailbox contract: a message sent to a known session via the registry
   handler is delivered by ``deliver_peer_messages`` as one transcript row with peer
   provenance declared, and the inbox file is consumed once the row is stored.
2. Trust/scope contract: sending to an unknown session id fails without creating a
   mailbox, and a session with no inbox drains nothing.
"""

import json

import pytest


@pytest.fixture
def peer_env(tmp_path, monkeypatch):
    """Temp HERMES_HOME with a real SessionDB containing one live target session."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    db.create_session("target-session-1", source="cli")
    db.close()
    return home


class _StubAgent:
    """A recipient whose transcript stores every row it is handed (``stored``)."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.stored = []

    def store(self, row):
        self.stored.append(row)
        return True


def _dispatch(name, args, session_id):
    from tools.registry import registry
    return json.loads(registry.dispatch(name, args, session_id=session_id))


def test_peer_send_delivers_one_attributed_row_and_consumes_inbox(peer_env):
    import tools.peer_messaging_tool as pm

    result = _dispatch(
        "peer_send",
        {"target_session_id": "target-session-1", "message": "heads up: schema changed"},
        session_id="sender-session",
    )
    assert result.get("success") is True

    inbox = pm._inbox_dir("target-session-1")
    assert [p for p in inbox.iterdir() if p.suffix == ".json"], "message file must be queued"

    agent = _StubAgent("target-session-1")
    assert pm.deliver_peer_messages(agent, agent.store) is True
    delivered = "\n".join(row["content"] for row in agent.stored)
    # Provenance: attributed to the sender session, not the user.
    assert "sender-session" in delivered
    assert "heads up: schema changed" in delivered
    assert "user authority" in delivered  # explicit non-user provenance framing
    # Consumed: nothing left to deliver.
    assert not list(inbox.glob("*.json"))
    assert pm.deliver_peer_messages(_StubAgent("target-session-1"), agent.store) is False


def test_unknown_target_rejected_and_no_inbox_is_a_noop(peer_env):
    import tools.peer_messaging_tool as pm

    result = _dispatch(
        "peer_send",
        {"target_session_id": "no-such-session", "message": "hello"},
        session_id="sender-session",
    )
    assert result.get("success") is False or "error" in result
    assert not pm._inbox_dir("no-such-session").exists(), "failed send must not create a mailbox"

    # A session nobody messaged delivers nothing.
    agent = _StubAgent("never-messaged")
    assert pm.deliver_peer_messages(agent, agent.store) is False
    assert agent.stored == []


def test_message_stays_queued_until_its_row_is_stored(peer_env):
    """A transcript with no slot for the row yet (or a store that failed) must leave the message
    in the mailbox: it is the only durable copy."""
    import tools.peer_messaging_tool as pm

    _dispatch("peer_send", {"target_session_id": "target-session-1", "message": "not yet"},
              session_id="sender-session")
    offered = []
    assert pm.deliver_peer_messages(_StubAgent("target-session-1"), lambda row: offered.append(row) or False) is False
    assert offered and len(list(pm._inbox_dir("target-session-1").glob("*.json"))) == 1


@pytest.mark.platforms("posix")
def test_queued_messages_are_private_to_the_user(peer_env):
    """Only the owning user may list or read queued messages: a 0700 inbox holding 0600 files, whatever
    the process umask or the mode of the Hermes home."""
    import os
    import stat

    import tools.peer_messaging_tool as pm

    old_umask = os.umask(0o022)
    try:
        result = _dispatch(
            "peer_send",
            {"target_session_id": "target-session-1", "message": "private note"},
            session_id="sender-session",
        )
    finally:
        os.umask(old_umask)
    assert result.get("success") is True
    inbox = pm._inbox_dir("target-session-1")
    assert stat.S_IMODE(inbox.stat().st_mode) == 0o700
    assert [stat.S_IMODE(p.stat().st_mode) for p in inbox.iterdir()] == [0o600]


def test_ended_session_is_refused_without_creating_a_mailbox(peer_env):
    """An ended session reads no mailbox (discovery already hides it); a send must say so, not report
    a message queued that nobody may ever read."""
    import tools.peer_messaging_tool as pm
    from hermes_state import SessionDB

    db = SessionDB(db_path=peer_env / "state.db")
    db.end_session("target-session-1", "user_exit")
    db.close()
    result = _dispatch(
        "peer_send",
        {"target_session_id": "target-session-1", "message": "anyone there?"},
        session_id="sender-session",
    )
    assert result.get("success") is not True
    assert "peer_sessions" in json.dumps(result)
    assert not pm._inbox_dir("target-session-1").exists()
