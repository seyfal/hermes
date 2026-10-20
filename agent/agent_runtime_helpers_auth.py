"""Authentication recovery for the runtime credential pool."""

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


