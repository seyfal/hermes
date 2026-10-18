"""Peer session messaging — let one Hermes session message another on the same machine.

Inspired by Muse Code's inter-session messaging (out-of-beta release, Aug 31 2026):
"Your sessions can now deliver messages to each other... Messages travel over a Unix
socket between processes owned by you, so nothing crosses the network. The agent does
the sending: it discovers peers and writes the message itself through two built-in
tools." Hermes adapts the design to filesystem mailboxes under ``$HERMES_HOME`` (works
on Windows too, atomic via ``os.replace``) and delivers received messages as a
standalone user-role row after the recipient's tool results (the slot a mid-turn
/steer takes) or after the question that starts its next turn
(``agent/turn_iteration_prep.py``), framed as a peer's message
(``agent.prompt_builder.peer_message_row``), never as the user's steer.

Trust boundary: the mailbox directory lives inside ``$HERMES_HOME`` (0700 on POSIX),
so only processes running as the same user can write messages — the same trust domain
as the SQLite session store itself. Nothing crosses the network. The delivered row
identifies the sending peer and states that it carries no user authority.

The ``peer_messaging`` toolset opts into discovery and sending. Receiving does not
require that toolset: an enabled sender can create another session's inbox, which
the recipient reads before each model request. Idle sessions are not started
automatically. A message file is deleted only once its row is stored in the
recipient's session database, so a receiver crash leaves the message either stored
or still queued.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_INBOX_DIRNAME = "peer_messages"  # $HERMES_HOME/runtime/peer_messages/<session_id>/
_MAX_MESSAGE_CHARS = 8_000
# Cap unread messages per inbox so an abandoned session's mailbox can't grow unbounded.
_MAX_INBOX_FILES = 50
# Default liveness window for peer discovery.
_DEFAULT_ACTIVE_WITHIN_MINUTES = 60

_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_session_dir_component(session_id: str) -> str:
    """Session id as a single safe path component (ids are locally generated, but a
    corrupted/hostile DB row must never traverse out of the inbox root)."""
    return _SESSION_ID_RE.sub("_", str(session_id or "").strip())[:128]


def _inbox_root() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "runtime" / _INBOX_DIRNAME


def _inbox_dir(session_id: str, *, hermes_home: Optional[Path] = None) -> Path:
    root = _inbox_root() if hermes_home is None else hermes_home / "runtime" / _INBOX_DIRNAME
    return root / _safe_session_dir_component(session_id)


def check_peer_messaging_requirements() -> bool:
    """Requires the SQLite state database (peer discovery reads it)."""
    return (_inbox_root().parent.parent / "state.db").is_file()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def peer_send(target_session_id: str, message: str, *,
              from_session_id: Optional[str] = None) -> str:
    """Write one message file into the target session's inbox (atomic rename)."""
    target = str(target_session_id or "").strip()
    body = str(message or "").strip()
    if not target:
        return tool_error("target_session_id is required")
    if not body:
        return tool_error("message is required")
    if len(body) > _MAX_MESSAGE_CHARS:
        return tool_error(f"message too long ({len(body)} chars; max {_MAX_MESSAGE_CHARS})")
    sender = str(from_session_id or "").strip()
    if sender and target == sender:
        return tool_error("target_session_id is this session — peer_send messages OTHER sessions")

    row = _lookup_session(target)
    if row is None:
        return tool_error(
            f"No session '{target}' found. Use peer_sessions to list the user's "
            "other live sessions and copy an exact session id.")

    inbox = _inbox_dir(target)
    try:
        inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            pending = sum(1 for p in inbox.iterdir() if p.suffix == ".json")
        except OSError:
            pending = 0
        if pending >= _MAX_INBOX_FILES:
            return tool_error(
                f"Target inbox is full ({pending} undelivered messages) — the session "
                "is likely not running. It will drain them if it resumes.")
        payload = {
            "from_session_id": sender or None,
            "message": body,
            "sent_at": time.time(),
        }
        # Sortable name (delivery order) + atomic publish so the reader never sees a
        # partial file. os.replace is atomic on POSIX and Windows.
        name = f"{time.time_ns():020d}_{uuid.uuid4().hex[:8]}"
        tmp = inbox / f".{name}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, inbox / f"{name}.json")
    except OSError as e:
        return tool_error(f"Failed to write peer message: {e}")

    return json.dumps({
        "success": True,
        "target_session_id": target,
        "target_title": row.get("title") or None,
        "note": ("Message queued. The target reads it before its next model request: mid-turn "
                 "if it is running, else when its next turn starts (idle sessions are not "
                 "started). Queued does not mean read or completed."),
    })


def _lookup_session(session_id: str) -> Optional[dict[str, Any]]:
    """The target's session row, or None. Never raises."""
    try:
        from hermes_state import SessionDB
        db = SessionDB(db_path=_inbox_root().parent.parent / "state.db", read_only=True)
        try:
            row = db.get_session(session_id)
        finally:
            db.close()
        return dict(row) if row else None
    except Exception:
        logger.debug("peer_send: session lookup failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def peer_sessions(active_within_minutes: int = _DEFAULT_ACTIVE_WITHIN_MINUTES, *,
                  current_session_id: Optional[str] = None) -> str:
    """List the user's other recently active sessions (this session excluded)."""
    try:
        minutes = max(1, min(int(active_within_minutes or _DEFAULT_ACTIVE_WITHIN_MINUTES), 24 * 60))
    except (TypeError, ValueError):
        minutes = _DEFAULT_ACTIVE_WITHIN_MINUTES
    try:
        from hermes_state import SessionDB
        db = SessionDB(db_path=_inbox_root().parent.parent / "state.db", read_only=True)
        try:
            rows = db.list_sessions_rich(limit=100, order_by_last_active=True)
        finally:
            db.close()
    except Exception as e:
        logger.error("peer_sessions: session list failed: %s", e, exc_info=True)
        return tool_error(f"Could not read the session store: {e}", success=False)

    peers = _filter_peer_rows(rows, current_session_id=current_session_id, active_within_minutes=minutes)
    return json.dumps({
        "success": True,
        "active_within_minutes": minutes,
        "count": len(peers),
        "peers": peers,
        "note": ("Message a peer with peer_send(target_session_id=..., message=...). "
                 "Delivery lands mid-turn when the peer is running a turn, otherwise "
                 "at the start of its next turn."),
    }, ensure_ascii=False)


def _filter_peer_rows(rows: list[dict[str, Any]], *, current_session_id: Optional[str],
                      active_within_minutes: int) -> list[dict[str, Any]]:
    """Project session rows to peer entries: never the current session, only sessions
    active within the window, ended sessions excluded."""
    cutoff = time.time() - active_within_minutes * 60
    own = str(current_session_id or "").strip()
    peers: list[dict[str, Any]] = []
    for s in rows or []:
        sid = str(s.get("id") or "").strip()
        if not sid or sid == own:
            continue
        if s.get("ended_at"):
            continue
        last_active = _to_epoch(s.get("last_active") or s.get("last_activity_at") or s.get("started_at"))
        if last_active is None or last_active < cutoff:
            continue
        peers.append({
            "session_id": sid,
            "title": s.get("title") or None,
            "source": s.get("source") or None,
            "cwd": s.get("cwd") or None,
            "last_active": s.get("last_active") or s.get("last_activity_at") or None,
            "last_activity_description": s.get("last_activity_description") or None,
        })
    return peers


def _to_epoch(value: Any) -> Optional[float]:
    """Best-effort epoch seconds from the store's timestamp shapes (epoch float or ISO text)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Receiving (called from agent/turn_iteration_prep.py before each model request; never raises)
# ---------------------------------------------------------------------------

def format_peer_message(entry: dict[str, Any]) -> str:
    """One message attributed to its sending session (the row's frame says what a peer is)."""
    sender = str(entry.get("from_session_id") or "(unidentified)").strip()
    body = str(entry.get("message") or "").strip()
    return f"From Hermes session {sender}:\n{body}"


def deliver_peer_messages(agent: Any, store: Callable[[dict[str, Any]], bool]) -> bool:
    """Hand ``agent``'s queued messages to ``store`` as one transcript row; True iff stored.

    ``store`` adds the row to the transcript and returns True once it is persisted, or False
    when the transcript has no slot for it yet. Message files are deleted only after True, so
    a crash leaves each message stored or still queued (a crash between the two can deliver
    it twice, never zero times). Exits on one ``is_dir()`` when nothing was ever sent to this
    session. An agent that persists nothing (the background-review and /btw forks share their
    parent's session id) never reads it: the row would vanish with that agent."""
    session_id = getattr(agent, "session_id", None)
    if not session_id or getattr(agent, "_persist_disabled", False):
        return False
    try:
        # The agent's store pins the recipient home even in a multiplexed process.
        session_db = getattr(agent, "_session_db", None)
        home = Path(session_db.db_path).parent if session_db is not None else None
        inbox = _inbox_dir(session_id, hermes_home=home)
        if not inbox.is_dir():
            return False
        files = sorted(p for p in inbox.iterdir() if p.suffix == ".json")
    except Exception:
        logger.debug("peer inbox scan failed", exc_info=True)
        return False
    entries = [entry for entry in map(_read_peer_message, files) if entry]
    if entries:
        from agent.prompt_builder import peer_message_row
        if not store(peer_message_row("\n\n".join(format_peer_message(e) for e in entries))):
            return False
    # Unreadable files go too, so a poison message can't wedge the inbox.
    for path in files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return bool(entries)


def _read_peer_message(path: Path) -> Optional[dict[str, Any]]:
    """The message envelope at ``path``, or None when it is unreadable or empty."""
    try:
        entry = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        logger.debug("peer inbox: unreadable message %s", path.name, exc_info=True)
        return None
    return entry if isinstance(entry, dict) and str(entry.get("message") or "").strip() else None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

PEER_SESSIONS_SCHEMA = {
    "name": "peer_sessions",
    "description": (
        "List the user's other recently active Hermes sessions on this machine — the "
        "peers this session can message with peer_send. Returns each peer's session id, "
        "title, source, working directory and last activity. Local-only discovery over "
        "the session store; nothing crosses the network."),
    "parameters": {
        "type": "object",
        "properties": {
            "active_within_minutes": {
                "type": "integer",
                "description": ("Only list sessions active within this many minutes "
                                f"(default {_DEFAULT_ACTIVE_WITHIN_MINUTES}, max 1440)."),
            },
        },
        "required": [],
    },
}

PEER_SEND_SCHEMA = {
    "name": "peer_send",
    "description": (
        "Send a message to another of the user's Hermes sessions on this machine (find "
        "targets with peer_sessions). Use it when a change here affects what another "
        "session is building, or to hand over an answer another session is blocked on. "
        "Delivery is local-only (a mailbox under the Hermes home, never the network); "
        "the peer receives it mid-turn if running, else at the start of its next turn."),
    "parameters": {
        "type": "object",
        "properties": {
            "target_session_id": {
                "type": "string",
                "description": "Exact session id of the target session (from peer_sessions).",
            },
            "message": {
                "type": "string",
                "description": ("The message. Be self-contained — the peer has none of this "
                                f"session's context. Max {_MAX_MESSAGE_CHARS} chars."),
            },
        },
        "required": ["target_session_id", "message"],
    },
}

registry.register(
    name="peer_sessions",
    toolset="peer_messaging",
    schema=PEER_SESSIONS_SCHEMA,
    handler=lambda args, **kw: peer_sessions(
        active_within_minutes=args.get("active_within_minutes", _DEFAULT_ACTIVE_WITHIN_MINUTES),
        current_session_id=kw.get("session_id")),
    check_fn=check_peer_messaging_requirements,
    emoji="📬")

registry.register(
    name="peer_send",
    toolset="peer_messaging",
    schema=PEER_SEND_SCHEMA,
    handler=lambda args, **kw: peer_send(
        target_session_id=args.get("target_session_id", ""),
        message=args.get("message", ""),
        from_session_id=kw.get("session_id")),
    check_fn=check_peer_messaging_requirements,
    emoji="📨")
