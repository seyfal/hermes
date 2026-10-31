"""Foreign transcripts mount read-only and adopt only when continued (#142091)."""
import json
from types import SimpleNamespace

import pytest

from gateway.session_contract import SessionRef


async def _imported_connection(tmp_path, monkeypatch, source='claude-code'):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = {'platform_toolsets': {'cli': []}, 'model': {'default': 'demo-model', 'provider': 'custom'}}
    monkeypatch.setattr(run, '_load_gateway_config', lambda *_path: config)
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store._db
    origin = {'tool': source, 'path': str(tmp_path / 'abc-123.jsonl'), 'foreign_session_id': 'abc-123'}
    sid = db.import_foreign_history(origin, [{'role': 'user', 'content': 'Please fix the flaky test.'},
                                            {'role': 'assistant', 'content': 'Looking.'}],
                                   title='Fix the flaky test', cwd=None, profile='default')['session_id']
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='owner')
    connection = AuthorityConnection(authority, object(), {
        'provider': 'local', 'user_id': 'native', 'profile_id': str(tmp_path), 'instance_id': 'owner'})
    return connection, SessionRef(str(tmp_path), sid), origin


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['claude-code', 'codex-cli'])
async def test_an_imported_mount_is_adopted_in_place_and_reopened(tmp_path, monkeypatch, source):
    """Local adoption preserves the open mount's subscribers, history and event stream."""
    import asyncio
    import queue
    from gateway.session_local_migration import resolve_local_target
    from hermes_state_local import local_receipt

    connection, ref, _ = await _imported_connection(tmp_path, monkeypatch, source)
    authority, sid = connection.authority, ref.session_id
    frames = queue.Queue()
    authority.events[connection.actor.transport_id] = SimpleNamespace(write=lambda frame: frames.put(frame) or True)
    opened = await connection.resume(ref, {})
    assert [message['content'] for message in opened['messages']] == ['Please fix the flaky test.', 'Looking.']
    info = await connection.dispatch({'id': 1, 'method': 'session.info', 'params': {'session_id': sid}})
    assert info['result']['source'] == source

    assert resolve_local_target(authority, connection.actor, sid) == ref
    receipt = local_receipt(authority.db, sid)
    assert receipt['policy']['source'] == 'cli'
    assert authority.db.get_session(sid)['source'] == source
    authority.authorize(connection.actor, ref, 'session:submit')
    authority.sessions[sid].event_stream.publish(sid, {'text': 'continued'})
    frame = await asyncio.to_thread(frames.get, True, 5)
    assert frame['params']['payload'] == {'text': 'continued'}
    detached = await connection.detach(ref, {'session_id': sid, 'subscription_id': opened['subscription_id']})
    assert detached['detached'] is True

    authority.sessions.clear()
    again = await connection.resume(ref, {})
    assert again['session_id'] == sid
    assert local_receipt(authority.db, sid) == receipt
    assert [message['content'] for message in again['messages']] == ['Please fix the flaky test.', 'Looking.']


@pytest.mark.asyncio
async def test_title_resume_continues_an_imported_mount_through_the_fifo(tmp_path, monkeypatch):
    """The CLI's local-title resume binds a writable route without changing a history-only open."""
    from hermes_state_runtime import list_session_admissions

    connection, ref, origin = await _imported_connection(tmp_path, monkeypatch)
    await connection.resume(ref, {})
    continued = await connection.resume(ref, {'title': ref.session_id})
    assert continued['session_id'] == ref.session_id
    # Admission is real; only execution (which would call a model) is held here.
    monkeypatch.setattr(connection.authority, '_schedule', lambda ref: None)
    admitted = await connection.submit(ref, {'text': 'Continue the fix.', 'submission_id': 'next'})
    assert admitted['status'] == 'queued'
    pending = list_session_admissions(connection.authority.db, session_id=ref.session_id)
    assert [item['payload']['text'] for item in pending] == ['Continue the fix.']
    assert connection.authority.db.find_foreign_import(origin) == ref.session_id


def _imported_row(**over):
    row = {'id': 'imported-1', 'source': 'claude-code', 'chat_id': None,
           'session_key': None, 'profile_name': None, 'user_id': None,
           'origin_json': json.dumps({'imported_from': {
               'tool': 'claude-code', 'path': '/x/abc-123.jsonl', 'foreign_session_id': 'abc-123'}})}
    row.update(over)
    return row


def test_validate_legacy_row_still_refuses_routed_and_foreign_import_labels(tmp_path):
    """Only the importer's own unrouted, unowned row is adoptable under a foreign-tool label."""
    from hermes_state import SessionDB
    from hermes_state_local_migration import validate_legacy_row
    from hermes_state_runtime import RuntimeStoreError

    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        for override in (
            {'session_key': 'agent:main:cli:dm:imported-1'},
            {'chat_id': 'local-other-route'},
            {'user_id': 'nous:someone-else'},
            {'origin_json': None},
            {'profile_name': 'someone-else'},
            {'source': 'cli', 'session_key': 'agent:main:cli:dm:x'},
        ):
            with pytest.raises(RuntimeStoreError, match='not_found'):
                validate_legacy_row(db, _imported_row(**override))
        validate_legacy_row(db, _imported_row())
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_desktop_import_resumes_and_the_importer_still_finds_it(tmp_path, monkeypatch):
    """Adoption and each turn's peer refresh retain the browser's import identity."""
    from gateway.session_local_migration import resolve_local_target

    connection, ref, origin = await _imported_connection(tmp_path, monkeypatch)
    authority, sid = connection.authority, ref.session_id
    db, store = authority.db, authority.runner.session_store
    await connection.resume(ref, {})
    resolve_local_target(authority, connection.actor, sid)
    assert db.find_foreign_import(origin) == sid
    live = authority.sessions[sid]
    store._record_gateway_session_peer(sid, live.route, live.source)
    assert db.find_foreign_import(origin) == sid


def test_an_import_stamp_beside_a_route_is_not_adoptable(tmp_path):
    """``imported_from`` makes a row unrouted history only when it is all ``origin_json`` holds."""
    from hermes_state import SessionDB
    from hermes_state_local_migration import validate_legacy_row
    from hermes_state_runtime import RuntimeStoreError

    routed = {"platform": "telegram", "chat_id": "4242", "chat_type": "dm",
              "imported_from": json.loads(_imported_row()["origin_json"])["imported_from"]}
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        with pytest.raises(RuntimeStoreError, match="not_found"):
            validate_legacy_row(db, _imported_row(origin_json=json.dumps(routed)))
    finally:
        db.close()
