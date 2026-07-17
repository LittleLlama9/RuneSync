import pytest

from riot_api import (
    RiotApiAuthError,
    RiotApiNotFoundError,
    RiotApiRateLimitError,
    RiotApiTransientError,
    RiotApiTransportError,
)
from riot_provider_status import (
    ProviderStatus,
    RiotProviderStatusTracker,
    get_riot_provider_status,
    status_for_error,
)
from secret_store import RiotSecretStore, SecretStoreCorruptError
from timeline_provider import PRIVATE_MATCH_V5_ENV


class FakeBackend:
    def protect(self, plaintext: bytes) -> bytes:
        return b"enc:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"enc:"):
            raise SecretStoreCorruptError("corrupt")
        return ciphertext[4:][::-1]


_ENABLED = {PRIVATE_MATCH_V5_ENV: "1"}
_DISABLED = {}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RiotApiAuthError("super-secret-token"), ProviderStatus.AUTH_REJECTED),
        (RiotApiRateLimitError("super-secret-token"), ProviderStatus.RATE_LIMITED),
        (RiotApiTransientError("super-secret-token"), ProviderStatus.UPSTREAM_UNAVAILABLE),
        (RiotApiTransportError("super-secret-token"), ProviderStatus.UPSTREAM_UNAVAILABLE),
        # A 404 means the request authenticated and reached Riot fine -- the
        # specific match/timeline just doesn't exist. That is provider
        # availability, not an upstream outage, so it must not collapse
        # into UPSTREAM_UNAVAILABLE.
        (RiotApiNotFoundError("super-secret-token"), ProviderStatus.AVAILABLE),
    ],
)
def test_status_for_error_maps_known_riot_errors(error, expected):
    assert status_for_error(error) is expected


def test_not_found_does_not_regress_a_prior_error_status(tmp_path):
    # A 404 confirms auth/connectivity worked for that request, so it should
    # move status toward AVAILABLE rather than leaving a stale worse status
    # (e.g. a previously recorded rate-limit) in place.
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("some-key")
    tracker = RiotProviderStatusTracker(store=store, env=_ENABLED)
    tracker.record_error(RiotApiRateLimitError("nope"))
    assert tracker.status() is ProviderStatus.RATE_LIMITED

    tracker.record_error(RiotApiNotFoundError("nope"))

    assert tracker.status() is ProviderStatus.AVAILABLE


def test_status_for_error_ignores_unrelated_exceptions():
    assert status_for_error(ValueError("not a riot error")) is None


def test_status_for_error_never_needs_the_message():
    # Regression guard: status_for_error must only branch on type(exc), so
    # even an error whose message happens to contain the word "token" is
    # classified purely by class, and the message itself is never read here.
    error = RiotApiAuthError("token=super-secret-token")
    assert status_for_error(error) is ProviderStatus.AUTH_REJECTED


def test_tracker_reports_private_disabled_before_anything_else(tmp_path):
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("some-key")
    tracker = RiotProviderStatusTracker(store=store, env=_DISABLED)

    assert tracker.status() is ProviderStatus.PRIVATE_DISABLED


def test_tracker_reports_missing_when_no_key_saved(tmp_path):
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    tracker = RiotProviderStatusTracker(store=store, env=_ENABLED)

    assert tracker.status() is ProviderStatus.MISSING


def test_tracker_reports_corrupt_without_reading_ciphertext(tmp_path):
    path = tmp_path / "riot.bin"
    store = RiotSecretStore(path, backend=FakeBackend())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not-encrypted-with-our-backend")
    tracker = RiotProviderStatusTracker(store=store, env=_ENABLED)

    assert tracker.status() is ProviderStatus.CORRUPT


def test_tracker_reports_available_once_key_present_and_no_errors(tmp_path):
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("some-key")
    tracker = RiotProviderStatusTracker(store=store, env=_ENABLED)

    assert tracker.status() is ProviderStatus.AVAILABLE


def test_tracker_records_and_reports_auth_rejected_without_clearing_key(tmp_path):
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("some-key")
    tracker = RiotProviderStatusTracker(store=store, env=_ENABLED)

    tracker.record_error(RiotApiAuthError("super-secret-token"))

    assert tracker.status() is ProviderStatus.AUTH_REJECTED
    # The encrypted key must be preserved so the user can replace it through
    # the normal set_key flow -- a rejected key is never auto-cleared.
    assert store.get_key() == "some-key"


def test_tracker_records_rate_limited_and_upstream_unavailable(tmp_path):
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("some-key")
    tracker = RiotProviderStatusTracker(store=store, env=_ENABLED)

    tracker.record_error(RiotApiRateLimitError("nope"))
    assert tracker.status() is ProviderStatus.RATE_LIMITED

    tracker.record_error(RiotApiTransientError("nope"))
    assert tracker.status() is ProviderStatus.UPSTREAM_UNAVAILABLE


def test_tracker_success_clears_a_prior_error_status(tmp_path):
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("some-key")
    tracker = RiotProviderStatusTracker(store=store, env=_ENABLED)

    tracker.record_error(RiotApiAuthError("nope"))
    assert tracker.status() is ProviderStatus.AUTH_REJECTED

    tracker.record_success()
    assert tracker.status() is ProviderStatus.AVAILABLE


def test_tracker_disabling_the_gate_overrides_a_remembered_error(tmp_path):
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("some-key")
    tracker = RiotProviderStatusTracker(store=store, env=_ENABLED)
    tracker.record_error(RiotApiAuthError("nope"))

    tracker.env = _DISABLED
    assert tracker.status() is ProviderStatus.PRIVATE_DISABLED


def test_record_disabled_preserves_remembered_error_across_re_enable(tmp_path):
    # Regression: record_disabled() must not clear _last_upstream_status.
    # error -> gate disabled (record_disabled called) -> gate re-enabled
    # must still report the remembered AUTH_REJECTED, not fall back to
    # AVAILABLE without any new successful request.
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("some-key")
    tracker = RiotProviderStatusTracker(store=store, env=_ENABLED)

    tracker.record_error(RiotApiAuthError("nope"))
    assert tracker.status() is ProviderStatus.AUTH_REJECTED

    tracker.env = _DISABLED
    tracker.record_disabled()
    assert tracker.status() is ProviderStatus.PRIVATE_DISABLED

    tracker.env = _ENABLED
    assert tracker.status() is ProviderStatus.AUTH_REJECTED


def test_tracker_missing_store_overrides_a_remembered_error(tmp_path):
    # Corruption/removal of the on-disk key always wins over a stale
    # in-memory upstream status -- storage state is ground truth.
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("some-key")
    tracker = RiotProviderStatusTracker(store=store, env=_ENABLED)
    tracker.record_error(RiotApiAuthError("nope"))

    store.clear()
    assert tracker.status() is ProviderStatus.MISSING


def test_tracker_reset_clears_remembered_upstream_status(tmp_path):
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("some-key")
    tracker = RiotProviderStatusTracker(store=store, env=_ENABLED)
    tracker.record_error(RiotApiAuthError("nope"))

    tracker.reset()

    assert tracker.status() is ProviderStatus.AVAILABLE


def test_tracker_without_a_store_is_missing_until_success_recorded():
    tracker = RiotProviderStatusTracker(store=None, env=_ENABLED)

    assert tracker.status() is ProviderStatus.MISSING

    tracker.record_success()
    assert tracker.status() is ProviderStatus.AVAILABLE


def test_get_riot_provider_status_module_level_api_uses_env_override(tmp_path, monkeypatch):
    # get_riot_provider_status() is the module-level API callers should use;
    # it must never require or expose the underlying secret to answer.
    monkeypatch.setattr(
        "riot_provider_status.RiotSecretStore",
        lambda: RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend()),
    )
    import riot_provider_status

    riot_provider_status._default_tracker = None
    try:
        assert get_riot_provider_status(env=_DISABLED) is ProviderStatus.PRIVATE_DISABLED
        assert get_riot_provider_status(env=_ENABLED) is ProviderStatus.MISSING
    finally:
        riot_provider_status._default_tracker = None


def test_provider_status_values_are_the_documented_safe_set():
    assert {status.value for status in ProviderStatus} == {
        "missing",
        "available",
        "corrupt",
        "private-disabled",
        "auth-rejected",
        "rate-limited",
        "upstream-unavailable",
    }
