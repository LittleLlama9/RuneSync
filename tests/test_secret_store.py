import os
import sys
from pathlib import Path

import pytest

import secret_store
from secret_store import (
    DpapiSecretBackend,
    RiotSecretStore,
    SECRET_FILENAME,
    SecretStoreCorruptError,
    SecretStoreStatus,
    SecretStoreUnavailableError,
    SecretStoreWriteError,
)


class FakeBackend:
    def protect(self, plaintext: bytes) -> bytes:
        return b"enc:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"enc:"):
            raise SecretStoreCorruptError("corrupt")
        return ciphertext[4:][::-1]


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is Windows-only")
def test_dpapi_backend_round_trip_and_empty_plaintext():
    backend = DpapiSecretBackend()

    plaintext = b"RuneSync DPAPI integration test"
    ciphertext = backend.protect(plaintext)
    assert ciphertext != plaintext
    assert backend.unprotect(ciphertext) == plaintext
    with pytest.raises(SecretStoreWriteError):
        backend.protect(b"")


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is Windows-only")
def test_dpapi_backend_rejects_corrupt_ciphertext():
    backend = DpapiSecretBackend()

    with pytest.raises(SecretStoreCorruptError):
        backend.unprotect(b"not-a-valid-dpapi-payload")


def test_secret_store_round_trip_and_repr_redaction(tmp_path):
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("super-secret-token")

    assert store.status() is SecretStoreStatus.AVAILABLE
    assert store.get_key() == "super-secret-token"
    assert store.path.read_bytes() == b"enc:nekot-terces-repus"
    assert "super-secret-token" not in repr(store)


def test_secret_store_clear_removes_file(tmp_path):
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("keep-me-private")

    assert store.clear() is True
    assert store.status() is SecretStoreStatus.MISSING
    assert store.clear() is False


def test_secret_store_detects_corruption_without_leaking_secret(tmp_path):
    path = tmp_path / "riot.bin"
    store = RiotSecretStore(path, backend=FakeBackend())
    path.write_bytes(b"super-secret-token")

    assert store.status() is SecretStoreStatus.CORRUPT
    with pytest.raises(SecretStoreCorruptError) as exc:
        store.get_key()
    assert "super-secret-token" not in str(exc.value)


def test_secret_store_uses_atomic_replace(tmp_path, monkeypatch):
    path = tmp_path / "riot.bin"
    store = RiotSecretStore(path, backend=FakeBackend())
    store.set_key("first-token")
    observed = {}
    original_replace = secret_store.os.replace

    def checked_replace(source, target):
        source_path = Path(source)
        target_path = Path(target)
        observed["tmp_bytes"] = source_path.read_bytes()
        observed["target_bytes_before"] = target_path.read_bytes()
        return original_replace(source, target)

    monkeypatch.setattr(secret_store.os, "replace", checked_replace)
    store.set_key("second-token")

    assert observed["tmp_bytes"] == b"enc:nekot-dnoces"
    assert observed["target_bytes_before"] == b"enc:nekot-tsrif"
    assert path.read_bytes() == b"enc:nekot-dnoces"


def test_secret_store_requires_secure_backend_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_store.os, "name", "posix")
    with pytest.raises(SecretStoreUnavailableError):
        RiotSecretStore(tmp_path / "riot.bin")


def test_default_secret_path_uses_appdata_not_pyinstaller_bundle_dir(monkeypatch, tmp_path):
    # The encrypted key must live in the per-user writable profile, not
    # inside a PyInstaller onefile extraction dir (sys._MEIPASS), which is a
    # temporary, read-only-in-spirit bundle location that is wiped/rewritten
    # across runs and would be the wrong place for persistent secrets.
    fake_meipass = tmp_path / "meipass-extraction"
    fake_meipass.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(fake_meipass), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))

    path = secret_store._default_secret_path()

    assert str(fake_meipass) not in str(path)
    assert str(path).startswith(str(tmp_path / "AppData" / "Roaming"))
    assert path.name == SECRET_FILENAME


def test_default_secret_path_is_identical_frozen_or_not(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    dev_path = secret_store._default_secret_path()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)
    frozen_path = secret_store._default_secret_path()

    assert dev_path == frozen_path


@pytest.mark.parametrize("spec_name", ["RuneSync.spec", "RuneSyncDebug.spec"])
def test_pyinstaller_spec_never_bundles_the_secret_file(spec_name):
    # Regression guard: the encrypted Riot key must never be listed as a
    # packaged resource (datas/binaries) in the PyInstaller spec files.
    repo_root = Path(__file__).resolve().parent.parent
    spec_path = repo_root / spec_name
    if not spec_path.exists():
        pytest.skip(f"{spec_name} not found")
    contents = spec_path.read_text(encoding="utf-8")
    assert SECRET_FILENAME not in contents
    assert "riot_api_key" not in contents.lower()


def test_secret_store_repr_and_str_never_include_key_material(tmp_path):
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeBackend())
    store.set_key("frozen-build-secret-value")

    assert "frozen-build-secret-value" not in repr(store)
    assert "frozen-build-secret-value" not in str(store)
    assert "frozen-build-secret-value" not in str(vars(store))
