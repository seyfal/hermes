"""Serialized OAuth refresh and explicit credential refresh entry points."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agent.credential_pool import PooledCredential


def _token_is_usable(pool, entry) -> bool:
    if pool.provider == "anthropic" and entry.auth_type == "oauth" and entry.expires_at_ms is None:
        return False
    return bool(entry.runtime_api_key and not pool._entry_needs_refresh(entry))


def _can_adopt_rotation(pool, previous, synced) -> bool:
    changed = synced.access_token != previous.access_token or synced.refresh_token != previous.refresh_token
    # An expired peer generation supplies the refresh token, never the request bearer.
    return changed and (pool.provider != "anthropic" or _token_is_usable(pool, synced))


class CredentialPoolRefreshMixin:
    def _refresh_entry(self, entry: PooledCredential, *, force: bool) -> Optional[PooledCredential]:
        from agent.credential_pool import AUTH_TYPE_OAUTH, _SINGLE_USE_REFRESH_PROVIDERS, _auth_store_lock, plugin_refresh_hook

        if entry.auth_type != AUTH_TYPE_OAUTH or not entry.refresh_token:
            if force:
                self._mark_exhausted(entry, None)
            return None
        # Plugin providers with a ``refresh_credential`` hook are treated as single-use by default:
        # the pool cannot know their grant semantics, and a needless in-lock re-read is cheaper than
        # a ``refresh_token_reused`` login loss. Eligibility comes from the hook, never a name set.
        if self.provider not in _SINGLE_USE_REFRESH_PROVIDERS and plugin_refresh_hook(self.provider) is None:
            return self._refresh_entry_impl(entry, force=force)

        # Single-use refresh tokens: sync -> POST -> write-back must be atomic
        # across Hermes processes, or two processes adopt the same on-disk
        # token, both POST it, and the loser gets ``refresh_token_reused`` /
        # ``invalid_grant`` (for Anthropic sources other than claude_code
        # there was no recovery path at all). Serialize through the shared
        # cross-process auth-store flock; a waiter's in-lock re-sync picks up
        # the winner's rotated token and skips the POST.
        with _auth_store_lock(timeout_seconds=self._single_use_refresh_lock_timeout()):
            if self.provider == "openai-codex":
                synced = self._sync_entry_from_auth_store(entry)
                if synced is not entry and not force and not self._entry_needs_refresh(synced):
                    return synced
                return self._refresh_entry_impl(synced, force=force)
            synced = self._sync_entry_from_pool_store(entry)
            if self.provider == "anthropic" and synced.source == "claude_code":
                # claude_code entries are NOT profile-owned: the refresh token
                # lives in one shared ~/.claude/.credentials.json (or Keychain)
                # every profile reads. The profile-scoped lock above only covers
                # THIS profile's auth.json, so take the dedicated shared-file
                # lock (inner, per the ordering invariant on ``_auth_store_lock``)
                # and re-read that authoritative file before any
                # adopt-and-return shortcut fires. The official ``claude`` CLI
                # rotating out-of-band is handled by the sync-and-retry-once
                # fallback in ``_recover_failed_refresh``.
                with self._claude_code_credentials_lock():
                    synced = self._sync_anthropic_entry_from_credentials_file(synced)
                    if _can_adopt_rotation(self, entry, synced):
                        return synced
                    return self._refresh_entry_impl(synced, force=force)
            if _can_adopt_rotation(self, entry, synced):
                return synced
            return self._refresh_entry_impl(synced, force=force)

    def try_refresh_current(self) -> Optional[PooledCredential]:
        return self.try_refresh_matching()

    def try_refresh_matching(
        self,
        api_key_hint: Optional[str] = None,
        credential_id: Optional[str] = None,
        *,
        force: bool = True,
    ) -> Optional[PooledCredential]:
        """Refresh the entry that supplied the request (forced by default).

        Direct integrations may reload the pool after a request failed, so
        ``current_id`` cannot identify the issuing credential. With no hint,
        select WITHOUT the normal proactive refresh: the forced refresh below
        must consume a rotating refresh token exactly once.

        ``force=False`` is the pre-request path: it returns the matching entry
        untouched unless it is near expiry, and never moves the cursor, so a
        caller can keep its own binding without rotating accounts or renewing
        a healthy token on every request.
        """
        with self._lock:
            entry = self._find(lambda e: e.id == credential_id) if credential_id else None
            if entry is None:
                if api_key_hint:
                    entry = self._find(lambda e: e.runtime_api_key == api_key_hint)
                else:
                    entry = self._current_unlocked() or self._select_unlocked(refresh=False, count=False)[0]
            if entry is None:
                return None
            if not force:
                if self.provider == "anthropic" and entry.source == "claude_code":
                    # Borrowed login: the external CLI may have rotated (and revoked) the pair.
                    entry = self._sync_anthropic_entry_from_credentials_file(entry)
                if not entry.refresh_token or not self._entry_needs_refresh(entry):
                    return entry
                return self._refresh_entry(entry, force=False)
        # A sibling may already have refreshed this shared pool after the failed request.
        if api_key_hint and entry.runtime_api_key != api_key_hint and _token_is_usable(self, entry):
            return entry
        refreshed = self._refresh_entry(entry, force=True)
        if refreshed is not None:
            with self._lock:
                self._current_id = refreshed.id
        return refreshed

    def auth_retry_entry(self, failures, rejected_keys, *, failed_id, model, base_url):
        """Re-read saved generations, then refresh/retry an eligible account outside the pool lock.

        A request-local failure tally rotates through accounts without benching the entire
        shared pool for a transient 401. Existing quota/model/dead benches remain binding.
        """
        from agent.credential_pool import (
            PooledCredential, credential_pool_entry_serves_endpoint, read_credential_pool,
        )
        from hermes_cli.auth_pool_rows import _merge_disk_cooldown_state

        # These are read-only snapshots; _refresh_entry rechecks under the cross-process
        # lock before spending a token. Reuse the persistence merge's cooldown rules.
        with self._lock:
            stored = {row["id"]: row for row in read_credential_pool(self.provider)
                      if isinstance(row, dict) and row.get("id")}
            for entry in list(self._entries):
                synced = self._sync_entry_from_pool_store(entry)
                if synced.source == "claude_code":
                    synced = self._sync_anthropic_entry_from_credentials_file(synced)
                payload = {**synced.to_dict(), "access_token": synced.access_token,
                           "refresh_token": synced.refresh_token}
                merged = _merge_disk_cooldown_state(payload, stored.get(entry.id), self.provider)
                self._replace_entry(synced, PooledCredential.from_dict(self.provider, merged))
            available, _ = self._available_entries(clear_expired=True, refresh=False, model=model)
        available = [e for e in available if credential_pool_entry_serves_endpoint(e, base_url)]
        if not available:
            return None
        # Give the issuing account one refresh, then prefer the least-failed alternative.
        first = sum(failures.values()) == 1
        entry = min(available, key=lambda e: (
            0 if first and e.id == failed_id else 1,
            failures.get(e.id, 0), e.id == failed_id, e.priority,
        ))
        rejected = rejected_keys.get(entry.id)
        if not _token_is_usable(self, entry) or (rejected and entry.runtime_api_key == rejected):
            return self.try_refresh_matching(credential_id=entry.id, api_key_hint=rejected)
        return entry
