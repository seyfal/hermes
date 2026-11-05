"""Per-account usage caps: keep part of a pooled subscription account's plan window for its owner.

``credential_pool_usage_caps`` in config.yaml sets, per pooled account, the highest share of a plan
window Hermes may use::

    credential_pool_usage_caps:
      anthropic:
        08a4e9:      # pool entry id (``hermes auth list``), or its label
          7d: 0.9    # stop at 90% of the weekly window; ``5h`` caps the 5-hour session window

Anthropic reports each window's utilization on every OAuth response
(``anthropic-ratelimit-unified-<5h|7d>-utilization``), counting all use of the account, claude.ai and
Claude Code included. When a response shows a capped window at or past its cap, the entry is benched
until that window resets, the same way as a window the provider reports spent:

- selection, leases and the ``balanced`` strategy skip it in every process (the bench is persisted),
  whatever the entry's priority;
- a session bound to it moves to another pooled credential before its next request: the account keeps
  answering 200 until 100%, so waiting for a 429 would spend the share its owner kept;
- with no other credential left, the turn in flight finishes on it and the next turn goes to
  ``fallback_providers``, the same as an exhausted pool;
- raising or removing the cap lifts the bench at the next selection (``hermes auth reset`` too).

A cap is checked on responses only: an account nothing has been sent to yet is not known to be over
its cap, so its first response can land past the cap by that one request.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Mapping, Optional

from agent.credential_pool_quota_window import _UNIFIED_PREFIX
from agent.credential_pool_timestamps import _parse_absolute_timestamp

logger = logging.getLogger(__name__)

CONFIG_KEY = "credential_pool_usage_caps"
USAGE_CAP_REASON = "usage_cap"
# Windows Anthropic reports, by the name its headers use.
WINDOWS = ("5h", "7d")
PROVIDERS = ("anthropic",)
_MESSAGE_RE = re.compile(r"^usage cap: (\w+) at ([0-9.]+)%")
_warned: set[tuple[str, ...]] = set()


def _warn_once(*key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning("%s: %s", CONFIG_KEY, message)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_caps(provider: str, key: str, raw: Any) -> dict[str, float]:
    """Window -> cap for one account; an invalid window or value is dropped with a warning."""
    if not isinstance(raw, Mapping):
        _warn_once(provider, key, message=f"{provider}.{key} must map windows ({', '.join(WINDOWS)}) to a share, "
                   "e.g. {7d: 0.9}; ignored")
        return {}
    caps: dict[str, float] = {}
    for window, value in raw.items():
        name = str(window).strip().lower()
        if name not in WINDOWS:
            _warn_once(provider, key, name, message=f"{provider}.{key}.{window}: unknown window "
                       f"(use {' or '.join(WINDOWS)}); ignored")
            continue
        cap = _number(value)
        if cap is None or not 0.0 < cap <= 1.0:
            _warn_once(provider, key, name, str(value), message=f"{provider}.{key}.{window}: {value!r} is not a "
                       "share between 0 and 1 (0.9 means 90%); ignored")
            continue
        caps[name] = cap
    return caps


def usage_caps(provider: str, entry: Any, config: Optional[Mapping[str, Any]] = None) -> dict[str, float]:
    """Window -> cap configured for *entry* (by entry id, else by label); empty when none."""
    if config is None:
        from agent.credential_pool import _load_config_safe
        config = _load_config_safe()
    section = (config or {}).get(CONFIG_KEY) if isinstance(config, Mapping) else None
    if not section:
        return {}
    if not isinstance(section, Mapping):
        _warn_once("", message="must map providers to accounts; ignored")
        return {}
    per_provider = section.get(provider)
    if per_provider is None:
        return {}
    if provider not in PROVIDERS or not isinstance(per_provider, Mapping):
        _warn_once(provider, message=f"{provider}: only {', '.join(PROVIDERS)} pools report usage windows "
                   "on their responses; ignored")
        return {}
    by_key = {str(key): raw for key, raw in per_provider.items()}
    for key in (getattr(entry, "id", None), getattr(entry, "label", None)):
        if key and str(key) in by_key:
            return _parse_caps(provider, str(key), by_key[str(key)])
    return {}


def capped_window(headers: Any, caps: Mapping[str, float]) -> Optional[tuple[str, float, float, float]]:
    """``(window, used, cap, reset_at)`` for the capped window at or past its cap that resets last
    (every one of them must reopen before the account is under its caps again), else ``None``."""
    getter = getattr(headers, "get", None)
    if not callable(getter) or not caps:
        return None
    now = time.time()
    hits = []
    for window, cap in caps.items():
        used = _number(getter(f"{_UNIFIED_PREFIX}{window}-utilization"))
        reset_at = _parse_absolute_timestamp(getter(f"{_UNIFIED_PREFIX}{window}-reset"))
        if used is not None and used >= cap and reset_at is not None and reset_at > now:
            hits.append((window, used, cap, reset_at))
    return max(hits, key=lambda hit: hit[3]) if hits else None


def bench_capped_entry(pool: Any, credential_id: Optional[str], api_key_hint: Optional[str], headers: Any) -> bool:
    """Bench the entry that served *headers* when one of its capped windows reached its cap.
    Returns True when the entry's state changed."""
    entries = list(pool.entries())
    entry = next((e for e in entries if api_key_hint and e.runtime_api_key == api_key_hint), None) or next(
        (e for e in entries if credential_id and e.id == credential_id), None)
    if entry is None:
        return False
    hit = capped_window(headers, usage_caps(pool.provider, entry))
    if hit is None:
        return False
    window, used, cap, reset_at = hit
    message = f"usage cap: {window} at {used * 100:.1f}% (cap {cap * 100:.0f}%)"
    changed = pool.mark_quota_exhausted(
        reset_at=reset_at, credential_id=entry.id, api_key_hint=entry.runtime_api_key or None,
        reason=USAGE_CAP_REASON, message=message,
    )
    if changed:
        logger.info("credential pool: %s %s", entry.label or entry.id[:8], message)
    return changed


def usage_cap_bench_holds(entry: Any) -> bool:
    """Whether a usage-cap bench still stands under the current config: the cap that benched the
    entry must still be configured at or below the utilization it was benched at."""
    match = _MESSAGE_RE.match(str(getattr(entry, "last_error_message", "") or ""))
    if match is None:
        return True  # not ours to judge; keep the bench until its reset
    window, used = match.group(1), float(match.group(2)) / 100
    cap = usage_caps(str(getattr(entry, "provider", "") or ""), entry).get(window)
    return cap is not None and cap <= used + 1e-9


def _benched_entry(pool: Any, agent: Any) -> Any:
    """The entry the agent sends, when it is benched for a spent or capped window."""
    key = getattr(agent, "_anthropic_api_key", None) or getattr(agent, "api_key", None)
    spent = getattr(pool, "token_quota_spent", None)
    if not key or not isinstance(key, str) or not callable(spent) or spent(key) is not True:
        return None
    return next((e for e in pool.entries() if e.runtime_api_key == key), None)


def move_off_benched_credential(agent: Any, pool: Any) -> bool:
    """Before a request: a session whose credential is benched for a spent or capped window moves to
    the pool's pick now, rather than on the 429 a capped account would never send. Returns True when
    the agent now sends another credential; False (nothing changed) when no other one is usable."""
    benched = _benched_entry(pool, agent)
    if benched is None:
        return False
    from agent.credential_pool import credential_pool_entry_serves_endpoint
    from agent.credential_pool_balanced import observe_agent_placement

    model, base_url = getattr(agent, "model", None), getattr(agent, "base_url", None)
    if getattr(pool, "balanced", False) is True:
        entry = pool.best_entry_for_cold_session(model=model, base_url=base_url)
    else:
        entry = pool.select(model=model)
    if (entry is None or entry.id == benched.id or entry.runtime_api_key == benched.runtime_api_key
            or not credential_pool_entry_serves_endpoint(entry, base_url)):
        return False
    if agent._swap_credential(entry) is False:
        return False
    if getattr(pool, "balanced", False) is not True and not getattr(agent, "_credential_pool_revert_id", None) \
            and benched.priority < entry.priority:
        # Same rule as a 429 rotation: come back to the preferred entry once its window reopens.
        agent._credential_pool_revert_id = benched.id
    observe_agent_placement(agent, entry.id)
    logger.info("Session moved to credential %s (%s): %s is benched (%s)", entry.id, entry.label,
                benched.label or benched.id[:8], benched.last_error_reason or "spent window")
    return True


def leave_capped_credential_between_turns(agent: Any) -> bool:
    """Between turns: a session still on a usage-capped credential (the pre-request move found no
    other usable one) runs its next turn on ``fallback_providers`` instead, as it would after a
    429 on an exhausted pool. Returns True when the fallback took over."""
    pool = getattr(agent, "_credential_pool", None)
    if pool is None or getattr(pool, "provider", None) not in PROVIDERS:
        return False
    benched = _benched_entry(pool, agent)
    if benched is None or benched.last_error_reason != USAGE_CAP_REASON:
        return False
    if move_off_benched_credential(agent, pool):
        return False
    from agent.error_classifier import FailoverReason

    reset_at = _parse_absolute_timestamp(benched.last_error_reset_at)
    if not agent._try_activate_fallback(reason=FailoverReason.rate_limit, reset_at=reset_at):
        return False
    logger.info("Credential %s (%s) is at its usage cap and no pooled credential is left: "
                "the next turn runs on the fallback provider", benched.id, benched.label)
    return True
