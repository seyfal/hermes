"""Authentication recovery for the runtime credential pool."""

from dataclasses import dataclass, field
import logging
import time

AUTH_RECOVERY_WINDOW_SECONDS = 600.0
logger = logging.getLogger(__name__)


@dataclass
class _AuthRecovery:
    deadline: float
    attempts: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    rejected_keys: dict[str, str] = field(default_factory=dict)


def reset_auth_recovery(agent):
    agent._auth_pool_recovery = None
    agent._auth_pool_refresh_counts = {}


def _recover_anthropic_401(agent, pool, failed_entry, api_key_hint):
    from agent.retry_utils import jittered_backoff

    state = getattr(agent, "_auth_pool_recovery", None)
    if state is None:
        state = agent._auth_pool_recovery = _AuthRecovery(time.monotonic() + AUTH_RECOVERY_WINDOW_SECONDS)
    state.failures[failed_entry.id] = state.failures.get(failed_entry.id, 0) + 1
    state.rejected_keys[failed_entry.id] = api_key_hint or failed_entry.runtime_api_key
    while time.monotonic() < state.deadline:
        remaining = state.deadline - time.monotonic()
        delay = min(remaining, jittered_backoff(state.attempts, base_delay=2.0, max_delay=60.0))
        state.attempts += 1
        logger.info("Anthropic OAuth 401 — retrying saved credentials/pool in %.1fs (%.0fs remaining)", delay, remaining)
        if touch_activity := getattr(agent, "_touch_activity", None):
            touch_activity("Waiting for Anthropic OAuth recovery")
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if getattr(agent, "_interrupt_requested", False):
                return True  # Let the turn loop's interrupt/redirect path finish the turn.
            time.sleep(min(0.2, end - time.monotonic()))
        if getattr(agent, "_interrupt_requested", False):
            return True
        if time.monotonic() >= state.deadline:
            break
        try:
            entry = pool.auth_retry_entry(
                state.failures, state.rejected_keys, failed_id=failed_entry.id,
                model=getattr(agent, "model", None), base_url=getattr(agent, "base_url", None),
            )
        except TimeoutError:
            # A busy cross-process refresh lock is not a dead credential.
            continue
        if entry is not None and agent._swap_credential(entry) is not False:
            logger.info("Anthropic OAuth 401 — retrying pool entry %s", entry.id)
            return True
    logger.warning("Anthropic OAuth 401 persisted for %.0fs — allowing fallback", AUTH_RECOVERY_WINDOW_SECONDS)
    return False

def recover_auth_failure(agent, pool, *, status_code, has_retried_429, error_context, api_key_hint, credential_id, rotate_and_swap):
    from agent.agent_runtime_helpers import _is_entitlement_403, _ra, _MAX_AUTH_REFRESH_ATTEMPTS

    if _is_entitlement_403(agent, status_code, error_context):
        _ra().logger.info(
            "Credential %s — entitlement-shaped 403 from %s; "
            "skipping pool refresh (account lacks subscription, "
            "not a transient auth failure).", status_code if status_code is not None else "auth",
            agent.provider or "provider",
        )
        return False, has_retried_429
    entry = next((e for e in pool.entries() if e.id == credential_id or
                  (not credential_id and e.runtime_api_key == api_key_hint)), None)
    if status_code == 401 and agent.provider == "anthropic" and entry and entry.auth_type == "oauth":
        return _recover_anthropic_401(agent, pool, entry, api_key_hint), has_retried_429
    # Refresh the entry that supplied the failing key, not current(): refreshing a healthy entry
    # burns its single-use refresh token for a failure it never had.
    refresh_kwargs = {"api_key_hint": api_key_hint}
    if credential_id:
        refresh_kwargs["credential_id"] = credential_id
    refreshed = pool.try_refresh_matching(**refresh_kwargs)
    if refreshed is None:
        # Refresh failed; rotate (the failed entry is already marked exhausted).
        return (True, False) if rotate_and_swap(401, "auth refresh failed") else (False, has_retried_429)
    # try_refresh_matching() reports success even when upstream keeps rejecting; cap same-entry
    # refreshes so a single-entry pool falls through to fallback.
    refreshed_id = getattr(refreshed, "id", None)
    if refreshed_id is not None:
        if getattr(agent, "_auth_pool_refresh_counts", None) is None:
            agent._auth_pool_refresh_counts = {}
        refresh_counts = agent._auth_pool_refresh_counts
        refresh_key = (agent.provider, refreshed_id)
        refresh_counts[refresh_key] = refresh_counts.get(refresh_key, 0) + 1
        if refresh_counts[refresh_key] > _MAX_AUTH_REFRESH_ATTEMPTS:
            _ra().logger.warning(
                "Credential auth failure persists after %s refreshes for "
                "pool entry %s — treating as unrecoverable and allowing "
                "fallback to activate.", refresh_counts[refresh_key] - 1, refreshed_id,
            )
            return False, has_retried_429
    _ra().logger.info("Credential auth failure — refreshed pool entry %s", getattr(refreshed, 'id', '?'))
    if agent._swap_credential(refreshed) is False:
        return False, has_retried_429
    return True, has_retried_429


