"""Serialized OAuth refresh and explicit credential refresh entry points."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agent.credential_pool import PooledCredential


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
                    if synced.refresh_token != entry.refresh_token:
                        return synced
                    return self._refresh_entry_impl(synced, force=force)
            if synced.access_token != entry.access_token or synced.refresh_token != entry.refresh_token:
                return synced
            return self._refresh_entry_impl(synced, force=force)

    def try_refresh_current(self) -> Optional[PooledCredential]:
        with self._lock:
            return self._try_refresh_current_unlocked()

    def try_refresh_matching(
        self,
        api_key_hint: Optional[str] = None,
        credential_id: Optional[str] = None,
    ) -> Optional[PooledCredential]:
        """Force-refresh the entry that supplied the failed request.

        Direct integrations may reload the pool after a request failed, so
        ``current_id`` cannot identify the issuing credential. With no hint,
        select WITHOUT the normal proactive refresh: the forced refresh below
        must consume a rotating refresh token exactly once.
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
            self._current_id = entry.id
            return self._try_refresh_current_unlocked()

    def _try_refresh_current_unlocked(self) -> Optional[PooledCredential]:
        entry = self._current_unlocked()
        if entry is None:
            return None
        refreshed = self._refresh_entry(entry, force=True)
        if refreshed is not None:
            self._current_id = refreshed.id
        return refreshed
