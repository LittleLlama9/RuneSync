"""Secure local storage for the private Riot API key."""

from __future__ import annotations

import ctypes
import os
import stat
from enum import Enum
from pathlib import Path
from typing import Optional, Protocol


SECRET_FILENAME = "riot_api_key.bin"
_SECRET_LABEL = "RuneSync Riot API key"


class SecretStoreStatus(Enum):
    MISSING = "missing"
    AVAILABLE = "available"
    CORRUPT = "corrupt"


class SecretStoreError(Exception):
    """Base class for Riot secret store failures."""


class SecretStoreUnavailableError(SecretStoreError):
    """Raised when no secure backend is available."""


class SecretStoreNotConfiguredError(SecretStoreError):
    """Raised when the Riot key has not been saved."""


class SecretStoreCorruptError(SecretStoreError):
    """Raised when stored encrypted bytes cannot be recovered."""


class SecretStoreWriteError(SecretStoreError):
    """Raised when the encrypted file cannot be updated."""


class SecretBackend(Protocol):
    def protect(self, plaintext: bytes) -> bytes:
        """Encrypt plaintext bytes for storage."""

    def unprotect(self, ciphertext: bytes) -> bytes:
        """Decrypt stored bytes."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob_from_bytes(data: bytes) -> tuple[_DataBlob, Optional[ctypes.Array]]:
    if not data:
        return _DataBlob(0, None), None
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _bytes_from_blob(blob: _DataBlob) -> bytes:
    if not blob.cbData or not blob.pbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


class DpapiSecretBackend:
    """Windows DPAPI backend backed by CryptProtectData."""

    def __init__(self):
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        self._protect = crypt32.CryptProtectData
        self._protect.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        self._protect.restype = ctypes.c_int
        self._unprotect = crypt32.CryptUnprotectData
        self._unprotect.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        self._unprotect.restype = ctypes.c_int
        self._local_free = kernel32.LocalFree
        self._local_free.argtypes = [ctypes.c_void_p]
        self._local_free.restype = ctypes.c_void_p

    def protect(self, plaintext: bytes) -> bytes:
        in_blob, buffer = _blob_from_bytes(plaintext)
        out_blob = _DataBlob()
        if not self._protect(
            ctypes.byref(in_blob),
            _SECRET_LABEL,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        ):
            raise SecretStoreWriteError("Failed to secure the Riot API key.")
        try:
            return _bytes_from_blob(out_blob)
        finally:
            self._local_free(out_blob.pbData)
            del buffer

    def unprotect(self, ciphertext: bytes) -> bytes:
        in_blob, buffer = _blob_from_bytes(ciphertext)
        out_blob = _DataBlob()
        description = ctypes.c_wchar_p()
        if not self._unprotect(
            ctypes.byref(in_blob),
            ctypes.byref(description),
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        ):
            raise SecretStoreCorruptError("Stored Riot credentials could not be recovered.")
        try:
            return _bytes_from_blob(out_blob)
        finally:
            self._local_free(out_blob.pbData)
            if description:
                self._local_free(description)
            del buffer


def _default_secret_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return base / "RuneSync" / SECRET_FILENAME


def _restrict_path(path: Path) -> None:
    try:
        if path.is_dir():
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        else:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass


class RiotSecretStore:
    """Best-effort secure Riot API key storage."""

    def __init__(self, path: Optional[Path] = None, backend: Optional[SecretBackend] = None):
        if backend is None and os.name != "nt":
            raise SecretStoreUnavailableError(
                "Secure Riot credential storage requires Windows DPAPI."
            )
        self._backend = backend or DpapiSecretBackend()
        self._path = Path(path) if path else _default_secret_path()

    def __repr__(self) -> str:
        return f"RiotSecretStore(path={str(self._path)!r})"

    @property
    def path(self) -> Path:
        return self._path

    def set_key(self, secret: str) -> None:
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError("Riot API key must be a non-empty string.")
        try:
            ciphertext = self._backend.protect(secret.encode("utf-8"))
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreWriteError("Failed to secure the Riot API key.") from exc
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        _restrict_path(directory)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            with tmp_path.open("wb") as handle:
                handle.write(ciphertext)
                handle.flush()
                os.fsync(handle.fileno())
            _restrict_path(tmp_path)
            os.replace(tmp_path, self._path)
            _restrict_path(self._path)
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise SecretStoreWriteError("Failed to update the Riot API key file.") from exc

    def get_key(self) -> str:
        if not self._path.exists():
            raise SecretStoreNotConfiguredError("No Riot API key is configured.")
        try:
            ciphertext = self._path.read_bytes()
        except OSError as exc:
            raise SecretStoreCorruptError("Stored Riot credentials could not be read.") from exc
        if not ciphertext:
            raise SecretStoreCorruptError("Stored Riot credentials are invalid.")
        try:
            plaintext = self._backend.unprotect(ciphertext)
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreCorruptError("Stored Riot credentials are invalid.") from exc
        try:
            secret = plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretStoreCorruptError("Stored Riot credentials are invalid.") from exc
        if not secret:
            raise SecretStoreCorruptError("Stored Riot credentials are invalid.")
        return secret

    def clear(self) -> bool:
        if not self._path.exists():
            return False
        try:
            self._path.unlink()
        except OSError as exc:
            raise SecretStoreWriteError("Failed to clear the Riot API key file.") from exc
        return True

    def status(self) -> SecretStoreStatus:
        if not self._path.exists():
            return SecretStoreStatus.MISSING
        try:
            self.get_key()
        except SecretStoreNotConfiguredError:
            return SecretStoreStatus.MISSING
        except SecretStoreCorruptError:
            return SecretStoreStatus.CORRUPT
        return SecretStoreStatus.AVAILABLE
