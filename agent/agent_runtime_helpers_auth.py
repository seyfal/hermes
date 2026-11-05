"""Anthropic OAuth 401 recovery for the runtime credential pool.

``agent.credential_auth_recovery.recover_auth_failure`` hands a 401 on a pooled Anthropic OAuth
entry here: back off, re-read saved tokens and rotate pool accounts inside one 600 s window.
"""

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


def reset_auth_recovery(agent, *, refresh_counts: bool = True):
    """Clear the Anthropic 401 window; at turn start also the per-turn refresh episodes."""
    agent._auth_pool_recovery = None
    if refresh_counts:
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
