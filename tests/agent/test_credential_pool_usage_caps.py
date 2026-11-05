"""Per-account usage caps (``credential_pool_usage_caps``) keep a share of a plan window free.

A real ``CredentialPool`` is loaded from a temp ``HERMES_HOME``; header shapes mirror live
Anthropic OAuth responses (utilization is a 0..1 fraction, resets are epoch seconds).
"""

import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from agent import anthropic_adapter
from agent import credential_pool as credential_pool_mod
from agent.credential_pool_usage_caps import (
    USAGE_CAP_REASON, capped_window, leave_capped_credential_between_turns, usage_caps,
)

WEEK_RESET = int(time.time()) + 3 * 86400
SESSION_RESET = int(time.time()) + 2 * 3600
CAPPED, OTHER = "cc-capped-account-test-only", "cc-other-account-test-only"


def _unified(week_util, session_util="0.05"):
    return {
        "anthropic-ratelimit-unified-5h-status": "allowed",
        "anthropic-ratelimit-unified-5h-utilization": session_util,
        "anthropic-ratelimit-unified-5h-reset": str(SESSION_RESET),
        "anthropic-ratelimit-unified-7d-status": "allowed",
        "anthropic-ratelimit-unified-7d-utilization": week_util,
        "anthropic-ratelimit-unified-7d-reset": str(WEEK_RESET),
        "anthropic-ratelimit-unified-overage-status": "rejected",
        "anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-reset": str(SESSION_RESET),
    }


def _row(cid, token, priority):
    return {
        "id": cid, "label": f"label-{cid}", "auth_type": "oauth", "priority": priority,
        "source": "manual:hermes_pkce", "access_token": token, "refresh_token": f"fake-refresh-{cid}",
        "expires_at_ms": int((time.time() + 86400) * 1000), "base_url": "https://api.anthropic.com",
    }


@pytest.fixture
def config(monkeypatch):
    """The config every pool read sees; tests edit it in place (a cap raised, removed, ...)."""
    cfg = {"model": {"provider": "anthropic"}, "credential_pool_strategies": {},
           "credential_pool_usage_caps": {"anthropic": {"capped": {"7d": 0.9}}}}
    monkeypatch.setattr(credential_pool_mod, "_load_config_safe", lambda: cfg)
    return cfg


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", lambda: None)
    home = Path(os.environ["HERMES_HOME"])
    (home / "auth.json").write_text(json.dumps({
        "version": 1, "providers": {}, "suppressed_sources": {"anthropic": ["claude_code"]},
        "credential_pool": {"anthropic": [_row("capped", CAPPED, 0), _row("other", OTHER, 1)]},
    }))
    return home


def _pool():
    return credential_pool_mod.load_pool("anthropic")


def _entry(pool, cid):
    return next(e for e in pool.entries() if e.id == cid)


def _respond(pool, entry_id, headers):
    """The response hook, as the agent runs it on every Anthropic response."""
    from agent.rate_limit_credits import RateLimitCreditsMixin

    agent = RateLimitCreditsMixin()
    agent._credential_pool, agent._credential_pool_entry_id = pool, entry_id
    agent._anthropic_api_key = agent.api_key = _entry(pool, entry_id).runtime_api_key
    agent._bench_spent_subscription_window(SimpleNamespace(headers=headers))


# ---- config -------------------------------------------------------------------------------


def test_caps_match_entry_id_then_label():
    cfg = {"credential_pool_usage_caps": {"anthropic": {
        "abc123": {"7d": 0.9}, "oauth-1": {"7D": 0.5, "5h": 0.8}}}}
    assert usage_caps("anthropic", SimpleNamespace(id="abc123", label="oauth-1"), cfg) == {"7d": 0.9}
    assert usage_caps("anthropic", SimpleNamespace(id="zzz", label="oauth-1"), cfg) == {"7d": 0.5, "5h": 0.8}
    assert usage_caps("anthropic", SimpleNamespace(id="zzz", label="nope"), cfg) == {}
    assert usage_caps("anthropic", SimpleNamespace(id="a", label="a"), {}) == {}


@pytest.mark.parametrize("window,value", [
    ("7d", 0), ("7d", -1), ("7d", 1.5), ("7d", "ninety"), ("7d", True), ("7d", None), ("1w", 0.9), ("opus", 0.5),
])
def test_invalid_window_or_share_is_ignored_with_a_warning(window, value, caplog):
    cfg = {"credential_pool_usage_caps": {"anthropic": {f"id-{window}-{value}": {window: value, "5h": 0.95}}}}
    entry = SimpleNamespace(id=f"id-{window}-{value}", label="x")
    with caplog.at_level(logging.WARNING, logger="agent.credential_pool_usage_caps"):
        assert usage_caps("anthropic", entry, cfg) == {"5h": 0.95}
    assert "credential_pool_usage_caps" in caplog.text


def test_a_provider_without_usage_headers_is_ignored_with_a_warning(caplog):
    cfg = {"credential_pool_usage_caps": {"openai-codex": {"a": {"7d": 0.9}}}}
    with caplog.at_level(logging.WARNING, logger="agent.credential_pool_usage_caps"):
        assert usage_caps("openai-codex", SimpleNamespace(id="a", label="a"), cfg) == {}
    assert "only anthropic" in caplog.text


def test_config_key_is_recognized():
    from hermes_cli.config import _validate_config_key

    assert _validate_config_key("credential_pool_usage_caps.anthropic.08a4e9.7d")[0] is True


def test_capped_window_is_the_one_that_resets_last():
    caps = {"5h": 0.5, "7d": 0.9}
    assert capped_window(_unified("0.89", "0.4"), caps) is None
    assert capped_window(_unified("0.90", "0.4"), caps)[0] == "7d"
    assert capped_window(_unified("0.95", "0.6"), caps)[::3] == ("7d", WEEK_RESET)
    assert capped_window(_unified("0.50", "0.6"), caps)[::3] == ("5h", SESSION_RESET)
    past = {**_unified("0.95"), "anthropic-ratelimit-unified-7d-reset": str(int(time.time()) - 5)}
    assert capped_window(past, caps) is None


# ---- benching -----------------------------------------------------------------------------


def test_capped_window_benches_until_its_reset_in_every_process(home):
    (home / "config.yaml").write_text(
        "model:\n  provider: anthropic\n"
        "credential_pool_usage_caps:\n  anthropic:\n    capped:\n      7d: 0.9\n")
    pool = _pool()
    _respond(pool, "capped", _unified("0.89"))
    assert pool.select().id == "capped"

    _respond(pool, "capped", _unified("0.90"))

    capped = _entry(pool, "capped")
    assert capped.last_error_reason == USAGE_CAP_REASON
    assert credential_pool_mod._exhausted_until(capped) == WEEK_RESET
    assert pool.token_quota_spent(CAPPED) is True
    fresh = _pool()  # another process reading auth.json
    assert fresh.select().id == "other"
    assert _entry(fresh, "capped").last_error_message == "usage cap: 7d at 90.0% (cap 90%)"


def test_uncapped_account_is_left_alone(home, config):
    pool = _pool()
    _respond(pool, "other", _unified("0.97"))
    assert _entry(pool, "other").last_status in (None, "ok")


@pytest.mark.parametrize("strategy", ["fill_first", "balanced", "least_used", "round_robin"])
def test_every_strategy_skips_a_capped_entry_whatever_its_priority(home, config, strategy):
    from agent.credential_pool_balanced import observe

    config["credential_pool_strategies"] = {"anthropic": strategy}
    pool = _pool()
    # The capped account outranks the other one and, for ``balanced``, reports far more headroom.
    observe("anthropic", "capped", {"anthropic-ratelimit-unified-5h-utilization": "0.01",
                                    "anthropic-ratelimit-unified-5h-reset": str(SESSION_RESET)})
    observe("anthropic", "other", {"anthropic-ratelimit-unified-5h-utilization": "0.80",
                                   "anthropic-ratelimit-unified-5h-reset": str(SESSION_RESET)})
    _respond(pool, "capped", _unified("0.92"))
    assert {pool.select().id for _ in range(4)} == {"other"}
    assert pool.acquire_lease() == "other"
    if strategy == "balanced":
        assert pool.best_entry_for_cold_session().id == "other"
    assert pool.reclaim("capped") is None


def test_raising_or_removing_the_cap_lifts_the_bench(home, config):
    pool = _pool()
    _respond(pool, "capped", _unified("0.92"))
    config["credential_pool_usage_caps"]["anthropic"]["capped"]["7d"] = 0.8  # lowered: still capped
    assert pool.select().id == "other"
    config["credential_pool_usage_caps"]["anthropic"]["capped"]["7d"] = 0.95  # raised above 92%
    assert pool.select().id == "capped"
    _respond(pool, "capped", _unified("0.96"))
    del config["credential_pool_usage_caps"]["anthropic"]["capped"]  # removed
    assert pool.select().id == "capped"
    assert _entry(pool, "capped").last_status == "ok"


def test_a_spent_window_is_not_lifted_by_the_cap(home, config):
    """The provider's own verdict (status rejected) stays until its reset, cap or no cap."""
    pool = _pool()
    spent = {**_unified("1.0"), "anthropic-ratelimit-unified-7d-status": "rejected",
             "anthropic-ratelimit-unified-status": "rejected", "anthropic-ratelimit-unified-reset": str(WEEK_RESET)}
    _respond(pool, "capped", spent)
    del config["credential_pool_usage_caps"]["anthropic"]["capped"]
    assert pool.select().id == "other"
    assert _entry(pool, "capped").last_error_reason == "quota_exhausted"


# ---- live sessions ------------------------------------------------------------------------


@pytest.fixture
def wire(monkeypatch):
    sent = []

    def send(client, request, **kwargs):
        sent.append(request.headers["Authorization"])
        return httpx.Response(200, request=request, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-test",
            "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}})

    monkeypatch.setattr(httpx.Client, "send", send)
    return sent


def _agent(pool, entry_id):
    from run_agent import AIAgent

    token = _entry(pool, entry_id).runtime_api_key
    agent = AIAgent.__new__(AIAgent)
    agent.provider, agent.model, agent.api_mode = "anthropic", "claude-test", "anthropic_messages"
    agent.base_url = agent._anthropic_base_url = "https://api.anthropic.com"
    agent.api_key = agent._anthropic_api_key = token
    agent._credential_pool, agent._credential_pool_entry_id = pool, entry_id
    agent._credential_pool_revert_id = None
    agent._oauth_1m_beta_disabled = False
    agent._anthropic_client = anthropic_adapter.build_anthropic_client(token, agent.base_url)
    return agent


def _send(agent):
    client = agent._create_request_anthropic_client(reason="usage_cap_test")
    try:
        client.messages.create(model=agent.model, max_tokens=8, messages=[{"role": "user", "content": "OK"}])
    finally:
        agent._close_request_anthropic_client(client, reason="request_error_cleanup")
        agent._anthropic_client.close()


def test_bound_session_leaves_a_capped_account_before_its_next_request(home, config, wire):
    """A capped account keeps answering 200, so the session must move before sending, not on a 429,
    and (fill_first) come back once the week resets, the way a 429 rotation does."""
    pool = _pool()
    agent = _agent(pool, "capped")
    _respond(pool, "capped", _unified("0.91"))
    _send(agent)
    assert wire == [f"Bearer {OTHER}"]
    assert agent._credential_pool_entry_id == "other"
    assert agent._credential_pool_revert_id == "capped"


def test_with_nothing_else_left_the_turn_finishes_and_the_next_one_falls_back(home, config, wire):
    pool = _pool()
    other = _entry(pool, "other")
    pool._mark_exhausted(other, 429, {"reason": "rate_limit", "reset_at": SESSION_RESET})
    agent = _agent(pool, "capped")
    agent._try_activate_fallback = Mock(return_value=True)
    _respond(pool, "capped", _unified("0.91"))

    _send(agent)  # mid-turn: no other credential, so the request goes out as before
    assert wire == [f"Bearer {CAPPED}"]

    from agent.turn_credential_placement import place_credential_between_turns
    place_credential_between_turns(agent)
    from agent.error_classifier import FailoverReason
    agent._try_activate_fallback.assert_called_once_with(reason=FailoverReason.rate_limit, reset_at=WEEK_RESET)


def test_between_turns_a_usable_sibling_wins_over_the_fallback(home, config):
    pool = _pool()
    agent = _agent(pool, "capped")
    agent._try_activate_fallback = Mock(side_effect=AssertionError("a pooled credential is still usable"))
    _respond(pool, "capped", _unified("0.91"))
    assert leave_capped_credential_between_turns(agent) is False
    assert agent._credential_pool_entry_id == "other"
    agent._anthropic_client.close()


def test_a_session_on_an_uncapped_account_is_untouched(home, config, wire):
    pool = _pool()
    agent = _agent(pool, "other")
    agent._try_activate_fallback = Mock(side_effect=AssertionError("nothing is capped"))
    _respond(pool, "capped", _unified("0.99"))
    _send(agent)
    assert wire == [f"Bearer {OTHER}"]
    assert leave_capped_credential_between_turns(agent) is False
