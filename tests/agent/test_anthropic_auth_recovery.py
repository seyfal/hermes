"""Anthropic 401 recovery uses real pools and isolated on-disk token generations."""

import time
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from agent.credential_pool import load_pool
from hermes_cli.auth import read_credential_pool, write_credential_pool


@pytest.fixture
def pool_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", lambda: None)
    return tmp_path


def _row(entry_id, priority=0, **fields):
    return {
        "id": entry_id, "priority": priority, "source": "manual:hermes_pkce",
        "auth_type": "oauth", "access_token": f"access-{entry_id}",
        "refresh_token": f"refresh-{entry_id}",
        "expires_at_ms": int((time.time() + 3600) * 1000),
        **fields,
    }


def _unauthorized():
    return anthropic.AuthenticationError(
        "OAuth access token has expired",
        response=httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")),
        body={"error": {"type": "authentication_error", "message": "OAuth access token has expired"}},
    )


@pytest.mark.parametrize("seconds_left", [-1, 30], ids=["expired", "inside-skew"])
def test_expired_saved_rotation_is_refreshed_before_retry(pool_home, monkeypatch, seconds_left):
    write_credential_pool("anthropic", [_row("a")])
    pool = load_pool("anthropic")
    stale = pool.entries()[0]
    peer_pool = load_pool("anthropic")
    write_credential_pool("anthropic", [_row(
        "a", access_token="expired-peer-access", refresh_token="unspent-peer-refresh",
        expires_at_ms=int((time.time() + seconds_left) * 1000),
    )])
    posted = []

    def refresh(token, **kwargs):
        posted.append(token)
        return dict(access_token="usable-access", refresh_token="next-refresh",
                    expires_at_ms=int((time.time() + 3600) * 1000))

    monkeypatch.setattr("agent.anthropic_credentials.refresh_anthropic_oauth_pure", refresh)

    def request(token):
        if token != "usable-access":
            raise _unauthorized()
        return "ok"

    with pytest.raises(anthropic.AuthenticationError):
        request(stale.runtime_api_key)
    first = pool.try_refresh_matching(api_key_hint=stale.runtime_api_key, credential_id=stale.id)
    assert first is not None and request(first.runtime_api_key) == "ok"
    from concurrent.futures import ThreadPoolExecutor
    # Both shared-instance callers and another process's stale snapshot must adopt
    # the one successful rotation, not consume its new refresh token again.
    pools = [pool, peer_pool] * 10
    with ThreadPoolExecutor(max_workers=len(pools)) as workers:
        refreshed = list(workers.map(
            lambda candidate: candidate.try_refresh_matching(api_key_hint=stale.runtime_api_key, credential_id=stale.id),
            pools,
        ))
    assert all(entry is not None and request(entry.runtime_api_key) == "ok" for entry in refreshed)
    assert posted == ["unspent-peer-refresh"]
    assert read_credential_pool("anthropic")[0]["access_token"] == "usable-access"


class _Clock:
    def __init__(self, monkeypatch):
        self.elapsed = 0.0
        self.waits = []
        epoch = time.time()
        self.time = SimpleNamespace(monotonic=lambda: self.elapsed, time=lambda: epoch + self.elapsed, sleep=self.sleep)
        monkeypatch.setattr("agent.credential_pool.time", self.time)
        try:
            from agent import agent_runtime_helpers_auth
        except ImportError:  # The regression witness must also run on the unpatched base.
            pass
        else:
            monkeypatch.setattr(agent_runtime_helpers_auth, "time", self.time)
        monkeypatch.setattr("agent.retry_utils.jittered_backoff",
                            lambda attempt, base_delay, max_delay: min(base_delay * 2**attempt, max_delay))

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.elapsed += seconds


def _session(pool, monkeypatch):
    from run_agent import AIAgent

    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda *args, **kwargs: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", lambda *args, **kwargs: {})
    session = AIAgent(
        model="claude-sonnet-4-5", provider="anthropic", api_mode="anthropic_messages",
        api_key=pool.select().runtime_api_key, base_url="https://api.anthropic.com", credential_pool=pool,
        quiet_mode=True, skip_context_files=True, skip_memory=True, save_trajectories=False,
    )
    session._disable_streaming = True
    # Exercise pool recovery, not the independent legacy singleton resolver.
    monkeypatch.setattr(session, "_try_refresh_anthropic_client_credentials", lambda: False)
    return session


def _request(session, client, monkeypatch):
    from anthropic.types import Message, TextBlock, Usage

    def call(kwargs):
        return Message(
            id="fake-message", type="message", role="assistant", model=session.model,
            content=[TextBlock(type="text", text=client(session.api_key))], stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(session, "_interruptible_api_call", call)
    return session.run_conversation("hello")


def test_persistent_401_rotates_to_healthy_unbenched_account(pool_home, monkeypatch):
    clock = _Clock(monkeypatch)
    write_credential_pool("anthropic", [
        _row("a"), _row("benched", 1, last_status="exhausted", last_status_at=time.time(),
                                last_error_code=429, last_error_reset_at=time.time() + 3600),
        _row("dead", 2, last_status="dead", last_status_at=time.time()),
        _row("model-benched", 3, model_cooldowns={"claude-sonnet-4-5": time.time() + 3600}), _row("b", 4),
    ])
    pool = load_pool("anthropic")
    session = _session(pool, monkeypatch)
    issued = []

    def refresh(token, **kwargs):
        return dict(access_token="still-rejected-a", refresh_token="next-a",
                    expires_at_ms=int((time.time() + 3600) * 1000))

    monkeypatch.setattr("agent.anthropic_credentials.refresh_anthropic_oauth_pure", refresh)

    def client(token):
        issued.append(token)
        if token != "access-b":
            raise _unauthorized()
        return "healthy reply"

    result = _request(session, client, monkeypatch)
    assert result["completed"] and result["final_response"] == "healthy reply"
    assert session._credential_pool_entry_id == "b"
    assert not {"access-benched", "access-dead", "access-model-benched"}.intersection(issued)
    assert 0 < clock.elapsed < 600
    assert {e.id: e.last_status for e in pool.entries()}["benched"] == "exhausted"
    from agent.agent_runtime_helpers import recover_with_credential_pool
    before = clock.elapsed
    assert recover_with_credential_pool(session, status_code=403, has_retried_429=False,
                                        error_context={"message": "subscription required"}) == (False, False)
    assert clock.elapsed == before


@pytest.mark.parametrize("recover_after, refresh_unavailable", [
    (180, False), (180, True), (None, False),
], ids=["peer-recovers", "refresh-unavailable", "deadline"])
def test_all_accounts_401_share_one_bounded_recovery_window(pool_home, monkeypatch, recover_after, refresh_unavailable):
    clock = _Clock(monkeypatch)
    write_credential_pool("anthropic", [_row("a"), _row("b", 1)])
    pool = load_pool("anthropic")
    session = _session(pool, monkeypatch)
    posted = set()
    issued_accounts = set()
    saved = False

    def refresh(token, **kwargs):
        assert not pool._lock._is_owned(), "refresh network I/O held the pool lock"
        if refresh_unavailable:
            raise RuntimeError("temporary token endpoint failure")
        assert token not in posted, "spent a single-use refresh token twice"
        posted.add(token)
        return dict(access_token=f"rejected-{len(posted)}", refresh_token=f"rotation-{len(posted)}",
                    expires_at_ms=int((time.time() + 3600) * 1000))

    monkeypatch.setattr("agent.anthropic_credentials.refresh_anthropic_oauth_pure", refresh)
    advance = clock.sleep

    def sleep(seconds):
        nonlocal saved
        advance(seconds)
        if recover_after is not None and clock.elapsed >= recover_after and not saved:
            write_credential_pool("anthropic", [
                _row("a", access_token="peer-usable", refresh_token="peer-unspent"),
            ])
            saved = True

    monkeypatch.setattr(clock.time, "sleep", sleep)

    def client(token):
        issued_accounts.add(session._credential_pool_entry_id)
        if token != "peer-usable":
            raise _unauthorized()
        return "recovered reply"

    result = _request(session, client, monkeypatch)
    if recover_after is None:
        assert result["failed"] and not result["completed"]
        assert clock.elapsed == pytest.approx(600)
    else:
        assert result["completed"] and result["final_response"] == "recovered reply"
        assert recover_after <= clock.elapsed < 600
        assert session._auth_pool_recovery is None
        assert "peer-unspent" not in posted, "refreshed a usable peer generation unnecessarily"
    assert issued_accounts == {"a", "b"}
    assert clock.waits
