"""Authenticated encryption for secrets persisted by application domains.

Responsibilities
----------------
* Load one deployment master key from ``TOFU_SECRET_ENCRYPTION_KEY``.
* Generate a mode-0600 personal-installation key when no environment key is
  configured.
* Bind every ciphertext to its purpose, numeric owner, and record identifier.

Domain repositories store only the returned ciphertext.  They must never log
the plaintext, ciphertext, or master key.  Distributed deployments must inject
the same environment key into every application replica; the generated file is
the zero-configuration personal-installation path only.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading

from cryptography.fernet import Fernet, InvalidToken

from lib.config_dir import config_path
from lib.identity import require_user_id
from lib.log import get_logger


logger = get_logger(__name__)

_ENVIRONMENT_KEY = "TOFU_SECRET_ENCRYPTION_KEY"
_KEY_PATH = Path(config_path(".secret_encryption.key"))
_ENVELOPE_VERSION = 1
_STORAGE_PAYLOAD_VERSION = 1
_lock = threading.RLock()
_cached_fernet: Fernet | None = None
_cached_key_fingerprint = ""


class SecretEnvelopeError(RuntimeError):
    """The deployment key or an authenticated ciphertext is invalid."""


def secret_hint(value: str) -> str:
    """Return a recognition hint that is insufficient to use as a secret."""
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if len(normalized) <= 8:
        return "****"
    return f"{normalized[:4]}…{normalized[-4:]}"


def _read_or_create_personal_key() -> bytes:
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        key = Fernet.generate_key()
        descriptor = os.open(
            str(_KEY_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = None
    if descriptor is not None:
        try:
            os.write(descriptor, key)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        logger.info("[SecretEnvelope] created personal deployment key")
        return key
    try:
        key = _KEY_PATH.read_bytes().strip()
    except OSError as exc:
        raise SecretEnvelopeError(
            "secret encryption key could not be read") from exc
    try:
        mode = _KEY_PATH.stat().st_mode & 0o777
        if mode & 0o077:
            os.chmod(_KEY_PATH, 0o600)
            logger.warning(
                "[SecretEnvelope] tightened personal key permissions to 0600")
    except OSError as exc:
        raise SecretEnvelopeError(
            "secret encryption key permissions could not be secured") from exc
    return key


def _key_material() -> bytes:
    configured = os.environ.get(_ENVIRONMENT_KEY, "").strip()
    return configured.encode("ascii") if configured else _read_or_create_personal_key()


def _fernet() -> Fernet:
    global _cached_fernet, _cached_key_fingerprint
    key = _key_material()
    fingerprint = hashlib.sha256(key).hexdigest()
    with _lock:
        if _cached_fernet is not None and fingerprint == _cached_key_fingerprint:
            return _cached_fernet
        try:
            cipher = Fernet(key)
        except (TypeError, ValueError) as exc:
            raise SecretEnvelopeError(
                f"{_ENVIRONMENT_KEY} is not a valid Fernet key") from exc
        _cached_fernet = cipher
        _cached_key_fingerprint = fingerprint
        return cipher


def _binding(*, purpose: str, owner_user_id: int, record_id: str) -> dict[str, object]:
    normalized_purpose = str(purpose or "").strip()
    normalized_record_id = str(record_id or "").strip()
    if not normalized_purpose or len(normalized_purpose) > 128:
        raise ValueError("secret purpose must be 1-128 characters")
    if not normalized_record_id or len(normalized_record_id) > 256:
        raise ValueError("secret record_id must be 1-256 characters")
    return {
        "version": _ENVELOPE_VERSION,
        "purpose": normalized_purpose,
        "owner_user_id": require_user_id(
            owner_user_id, context="secret owner"),
        "record_id": normalized_record_id,
    }


def seal_secret(
    value: str,
    *,
    purpose: str,
    owner_user_id: int,
    record_id: str,
) -> str:
    """Encrypt a secret and authenticate its domain ownership binding."""
    envelope = _binding(
        purpose=purpose, owner_user_id=owner_user_id, record_id=record_id)
    envelope["value"] = str(value or "")
    plaintext = json.dumps(
        envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return _fernet().encrypt(plaintext).decode("ascii")


def open_secret(
    ciphertext: str,
    *,
    purpose: str,
    owner_user_id: int,
    record_id: str,
) -> str:
    """Decrypt only when the authenticated domain binding matches exactly."""
    expected = _binding(
        purpose=purpose, owner_user_id=owner_user_id, record_id=record_id)
    try:
        raw = _fernet().decrypt(str(ciphertext or "").encode("ascii"))
        envelope = json.loads(raw.decode("utf-8"))
    except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise SecretEnvelopeError("stored secret could not be decrypted") from exc
    if not isinstance(envelope, dict) or any(
        envelope.get(key) != value for key, value in expected.items()
    ):
        raise SecretEnvelopeError("stored secret ownership binding is invalid")
    value = envelope.get("value")
    if not isinstance(value, str):
        raise SecretEnvelopeError("stored secret payload is invalid")
    return value


class BoundPayloadCipher:
    """Reusable authenticated codec for potentially sensitive documents.

    The full document is confidential, while its binding keeps ciphertext from
    being transplanted between a logical stream event or owner boundary. One
    instance retains only the already-constructed Fernet object so hot storage
    transactions never reread the deployment key file.
    """

    __slots__ = ("_cipher", "key_id")

    def __init__(self, cipher: Fernet, key_id: str) -> None:
        self._cipher = cipher
        self.key_id = key_id

    @staticmethod
    def _binding(
        *, purpose: str, tenant_id: str, owner_user_id: int, record_id: str,
    ) -> dict[str, object]:
        normalized_purpose = str(purpose or "").strip()
        normalized_tenant = str(tenant_id or "").strip()
        normalized_record_id = str(record_id or "").strip()
        if not normalized_purpose or len(normalized_purpose) > 128:
            raise ValueError("payload purpose must be 1-128 characters")
        if not normalized_tenant or len(normalized_tenant) > 128:
            raise ValueError("payload tenant_id must be 1-128 characters")
        if (
            not isinstance(owner_user_id, int)
            or isinstance(owner_user_id, bool)
            or owner_user_id < 0
        ):
            raise ValueError("payload owner_user_id must be non-negative")
        if not normalized_record_id or len(normalized_record_id) > 256:
            raise ValueError("payload record_id must be 1-256 characters")
        return {
            "version": _STORAGE_PAYLOAD_VERSION,
            "purpose": normalized_purpose,
            "tenant_id": normalized_tenant,
            "owner_user_id": owner_user_id,
            "record_id": normalized_record_id,
        }

    def seal(
        self,
        value: str,
        *,
        purpose: str,
        tenant_id: str,
        owner_user_id: int,
        record_id: str,
    ) -> str:
        envelope = self._binding(
            purpose=purpose,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            record_id=record_id,
        )
        envelope["value"] = str(value)
        plaintext = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return self._cipher.encrypt(plaintext).decode("ascii")

    def open(
        self,
        ciphertext: str,
        *,
        purpose: str,
        tenant_id: str,
        owner_user_id: int,
        record_id: str,
    ) -> str:
        expected = self._binding(
            purpose=purpose,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            record_id=record_id,
        )
        try:
            raw = self._cipher.decrypt(str(ciphertext or "").encode("ascii"))
            envelope = json.loads(raw.decode("utf-8"))
        except (
            InvalidToken,
            UnicodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            raise SecretEnvelopeError(
                "stored payload could not be decrypted") from exc
        if not isinstance(envelope, dict) or any(
            envelope.get(key) != value for key, value in expected.items()
        ):
            raise SecretEnvelopeError(
                "stored payload ownership binding is invalid")
        value = envelope.get("value")
        if not isinstance(value, str):
            raise SecretEnvelopeError("stored payload value is invalid")
        return value


def bound_payload_cipher() -> BoundPayloadCipher:
    """Load one reusable codec bound to the deployment master key."""
    cipher = _fernet()
    with _lock:
        key_id = _cached_key_fingerprint[:16]
    if len(key_id) != 16:
        raise SecretEnvelopeError("secret encryption key identity is invalid")
    return BoundPayloadCipher(cipher, key_id)


def reset_secret_envelope_for_test() -> None:
    """Clear process key state after an isolated test changes configuration."""
    global _cached_fernet, _cached_key_fingerprint
    with _lock:
        _cached_fernet = None
        _cached_key_fingerprint = ""


__all__ = [
    "BoundPayloadCipher",
    "SecretEnvelopeError",
    "bound_payload_cipher",
    "open_secret",
    "reset_secret_envelope_for_test",
    "seal_secret",
    "secret_hint",
]
