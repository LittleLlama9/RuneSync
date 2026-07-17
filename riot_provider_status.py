"""Sanitized status reporting for the Riot Match-V5 integration.

This module never reads, returns, logs, or stores token material. Status is
restricted to a small closed enum (:class:`ProviderStatus`) describing
operational state only -- callers get enough to render a UI indicator or
decide whether to retry, and nothing else. In particular:

- Exception *messages* are never inspected or surfaced here, only exception
  *types* (see :func:`status_for_error`). ``riot_api`` already keeps its
  exception messages free of secrets, but this module treats that as
  defense-in-depth, not the only guard.
- :class:`RiotProviderStatusTracker` stores only an enum value and a
  monotonic timestamp.
- A rejected/expired key is never cleared automatically. Riot personal
  development keys expire on a fixed schedule (see
  ``docs/RIOT_API_KEY_POLICY.md``) and production keys can be revoked or
  rate limited independent of local storage; the encrypted key on disk is
  left untouched so the user can inspect status and replace it through the
  normal ``RiotSecretStore.set_key`` flow.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Mapping, Optional

from riot_api import (
    RiotApiAuthError,
    RiotApiError,
    RiotApiNotFoundError,
    RiotApiRateLimitError,
    RiotApiTransientError,
    RiotApiTransportError,
)
from secret_store import RiotSecretStore, SecretStoreStatus
from timeline_provider import is_private_match_v5_enabled


class ProviderStatus(Enum):
    """Closed set of safe-to-display Riot Match-V5 provider states."""

    MISSING = "missing"
    AVAILABLE = "available"
    CORRUPT = "corrupt"
    PRIVATE_DISABLED = "private-disabled"
    AUTH_REJECTED = "auth-rejected"
    RATE_LIMITED = "rate-limited"
    UPSTREAM_UNAVAILABLE = "upstream-unavailable"


# Ordered most- to least-specific; the first matching exception type wins.
# RiotApiNotFoundError (404) is intentionally mapped to AVAILABLE: a 404
# means the request authenticated and reached Riot successfully -- the
# specific match/timeline resource just doesn't exist. That is not an
# upstream connectivity/availability problem, so it must not be reported as
# `upstream-unavailable`.
_UPSTREAM_STATUS_BY_ERROR: tuple[tuple[type, ProviderStatus], ...] = (
    (RiotApiAuthError, ProviderStatus.AUTH_REJECTED),
    (RiotApiRateLimitError, ProviderStatus.RATE_LIMITED),
    (RiotApiNotFoundError, ProviderStatus.AVAILABLE),
    (RiotApiTransientError, ProviderStatus.UPSTREAM_UNAVAILABLE),
    (RiotApiTransportError, ProviderStatus.UPSTREAM_UNAVAILABLE),
)


def status_for_error(exc: BaseException) -> Optional[ProviderStatus]:
    """Map a Riot API exception *type* to a sanitized provider status.

    Only ``type(exc)`` is inspected -- never ``str(exc)``, ``exc.args``, or
    any other attribute that could carry response text -- so this cannot
    leak token material even if a caller passes an exception that somehow
    embedded sensitive text.
    """
    for error_type, status in _UPSTREAM_STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return status
    if isinstance(exc, RiotApiError):
        return ProviderStatus.UPSTREAM_UNAVAILABLE
    return None


class RiotProviderStatusTracker:
    """Tracks the most recent sanitized Riot Match-V5 provider status.

    Holds only an enum value plus a monotonic timestamp -- no token
    material, no raw exception text, no response bodies -- so it is always
    safe to surface to UI, logs, or a future bridge/status endpoint.
    """

    def __init__(
        self,
        store: Optional[RiotSecretStore] = None,
        env: Optional[Mapping[str, str]] = None,
    ):
        self._store = store
        self._env = env
        self._last_upstream_status: Optional[ProviderStatus] = None
        self._last_updated: Optional[float] = None

    @property
    def last_updated(self) -> Optional[float]:
        """Monotonic timestamp of the last recorded transition, if any."""
        return self._last_updated

    @property
    def store(self) -> Optional[RiotSecretStore]:
        return self._store

    @property
    def env(self) -> Optional[Mapping[str, str]]:
        return self._env

    @env.setter
    def env(self, value: Optional[Mapping[str, str]]) -> None:
        self._env = value

    def record_success(self) -> None:
        """Record that the most recent Riot Match-V5 request succeeded."""
        self._last_upstream_status = ProviderStatus.AVAILABLE
        self._last_updated = time.monotonic()

    def record_error(self, exc: BaseException) -> None:
        """Record a sanitized status for a failed Riot Match-V5 request."""
        status = status_for_error(exc)
        if status is not None:
            self._last_upstream_status = status
            self._last_updated = time.monotonic()

    def record_disabled(self) -> None:
        """Record that a request was blocked by the private feature gate.

        This must not clear a previously remembered upstream status: while
        the gate is off, ``status()`` already reports ``PRIVATE_DISABLED``
        regardless of ``_last_upstream_status`` (see the precedence order in
        ``status()``), so overwriting the remembered status here would only
        cause it to be lost once the gate is re-enabled -- turning, e.g., a
        real AUTH_REJECTED back into a misleading AVAILABLE without any new
        successful request. Just update the timestamp.
        """
        self._last_updated = time.monotonic()

    def reset(self) -> None:
        """Clear any remembered upstream status (does not touch storage)."""
        self._last_upstream_status = None
        self._last_updated = None

    def status(self) -> ProviderStatus:
        """Return the current sanitized provider status.

        Precedence: the private feature gate, then local key storage state,
        then the last known upstream outcome, then a plain ``available``
        default once a key is present and nothing has failed yet.
        """
        if not is_private_match_v5_enabled(self._env):
            return ProviderStatus.PRIVATE_DISABLED

        if self._store is not None:
            # RiotSecretStore.status()'s documented contract is that it
            # never raises: it internally catches SecretStoreNotConfiguredError
            # and SecretStoreCorruptError and returns a SecretStoreStatus
            # value. Relying on that contract instead of a broad except
            # keeps this from ever swallowing an unrelated bug.
            store_status = self._store.status()
            if store_status is SecretStoreStatus.MISSING:
                return ProviderStatus.MISSING
            if store_status is SecretStoreStatus.CORRUPT:
                return ProviderStatus.CORRUPT

        if self._last_upstream_status is not None:
            return self._last_upstream_status

        return ProviderStatus.AVAILABLE if self._store is not None else ProviderStatus.MISSING


_default_tracker: Optional[RiotProviderStatusTracker] = None


def get_default_status_tracker() -> RiotProviderStatusTracker:
    """Return the process-wide tracker backed by the default secret store.

    Provider code (e.g. ``RiotMatchV5Provider``) should share this instance
    so that a request's outcome is reflected in later status checks within
    the same process.
    """
    global _default_tracker
    if _default_tracker is None:
        _default_tracker = RiotProviderStatusTracker(store=RiotSecretStore())
    return _default_tracker


def get_riot_provider_status(env: Optional[Mapping[str, str]] = None) -> ProviderStatus:
    """Module-level convenience: current sanitized Riot Match-V5 status.

    Never returns, logs, or otherwise exposes token material -- only one of
    the :class:`ProviderStatus` values.
    """
    tracker = get_default_status_tracker()
    if env is None:
        return tracker.status()
    # Build a throwaway tracker sharing the same store so an explicit env
    # override (tests, alternate config) doesn't disturb the shared
    # singleton's remembered upstream status.
    return RiotProviderStatusTracker(store=tracker.store, env=env).status()
