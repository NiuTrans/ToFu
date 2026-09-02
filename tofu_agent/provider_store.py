"""Encrypted, database-free persistence for the managed Provider.

Responsibility
--------------
Persist the one Provider selected by the standalone ``tofu-agent`` control
panel.  The endpoint and model remain inspectable operational metadata; API
keys and custom header values are stored only inside a Fernet envelope.

This module deliberately does not import the application storage authority or
``lib.config_dir``.  Its lifecycle is one small file plus an adjacent key,
which keeps the public wheel usable without SQLite/PostgreSQL or a checkout.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import uuid
from typing import Mapping

from cryptography.fernet import Fernet, InvalidToken

from tofu_agent.models import AgentConfigurationError, ProviderConfig


_SCHEMA_VERSION = 1
_CONFIG_KEY_ENV = 'TOFU_AGENT_CONFIG_KEY'
_MAX_CONFIG_BYTES = 2 * 1024 * 1024


class ProviderStoreError(AgentConfigurationError):
    """The encrypted Provider settings could not be read or written."""


def default_provider_config_path(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the per-user settings path without touching the filesystem."""
    source = os.environ if environ is None else environ
    explicit = str(source.get('TOFU_AGENT_CONFIG_PATH') or '').strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg_home = str(source.get('XDG_CONFIG_HOME') or '').strip()
    if xdg_home:
        return Path(xdg_home).expanduser() / 'tofu-agent' / 'provider.json'
    app_data = str(source.get('APPDATA') or '').strip()
    if os.name == 'nt' and app_data:
        return Path(app_data).expanduser() / 'tofu-agent' / 'provider.json'
    return Path.home() / '.config' / 'tofu-agent' / 'provider.json'


def secret_hint(value: str) -> str:
    """Return a recognition hint that cannot be used as the credential."""
    normalized = str(value or '').strip()
    if not normalized:
        return ''
    if len(normalized) <= 16:
        return '••••'
    return f'{normalized[:4]}…{normalized[-4:]}'


class ProviderSettingsStore:
    """Atomically persist one encrypted :class:`ProviderConfig`.

    A caller may inject ``TOFU_AGENT_CONFIG_KEY`` for containers or replicated
    deployments.  Otherwise an adjacent ``.key`` file is created once.  A
    copied provider JSON file alone is intentionally insufficient to recover
    its credentials.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.environ = os.environ if environ is None else environ
        self.path = (Path(path).expanduser() if path is not None
                     else default_provider_config_path(self.environ))
        self.key_path = self.path.with_name(f'.{self.path.name}.key')
        self._lock = threading.RLock()

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _ensure_parent(self) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise ProviderStoreError(
                'provider settings directory could not be created') from exc

    @staticmethod
    def _tighten_permissions(path: Path) -> None:
        try:
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                os.chmod(path, 0o600)
        except OSError as exc:
            raise ProviderStoreError(
                'provider settings permissions could not be secured') from exc

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        self._ensure_parent()
        temporary = path.with_name(
            f'.{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
        descriptor: int | None = None
        try:
            descriptor = os.open(
                str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError('provider settings write made no progress')
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            try:
                directory = os.open(str(path.parent), os.O_RDONLY)
            except OSError:
                directory = None
            if directory is not None:
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            raise ProviderStoreError(
                'provider settings could not be written atomically') from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _configured_key(self) -> bytes | None:
        value = str(self.environ.get(_CONFIG_KEY_ENV) or '').strip()
        return value.encode('ascii') if value else None

    def _key_material(self, *, create: bool) -> bytes:
        configured = self._configured_key()
        if configured is not None:
            return configured
        if self.key_path.is_file():
            self._tighten_permissions(self.key_path)
            try:
                if self.key_path.stat().st_size > 4096:
                    raise ProviderStoreError(
                        'provider encryption key file is unexpectedly large')
                return self.key_path.read_bytes().strip()
            except OSError as exc:
                raise ProviderStoreError(
                    'provider encryption key could not be read') from exc
        if not create:
            raise ProviderStoreError(
                'provider encryption key is missing; re-save the Provider '
                'from /setup or restore its adjacent key file')
        key = Fernet.generate_key()
        self._atomic_write(self.key_path, key + b'\n')
        return key

    def _fernet(self, *, create: bool) -> Fernet:
        try:
            return Fernet(self._key_material(create=create))
        except ProviderStoreError:
            raise
        except (TypeError, ValueError) as exc:
            raise ProviderStoreError(
                f'{_CONFIG_KEY_ENV} or the adjacent key file is invalid') from exc

    def load(self) -> ProviderConfig | None:
        """Decrypt and validate the stored Provider, or return ``None``."""
        with self._lock:
            if not self.path.is_file():
                return None
            self._tighten_permissions(self.path)
            try:
                if self.path.stat().st_size > _MAX_CONFIG_BYTES:
                    raise ProviderStoreError(
                        'provider settings file is unexpectedly large')
                document = json.loads(self.path.read_text(encoding='utf-8'))
            except ProviderStoreError:
                raise
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ProviderStoreError(
                    'provider settings file is unreadable or invalid JSON') from exc
            if not isinstance(document, dict) \
                    or document.get('schema_version') != _SCHEMA_VERSION:
                raise ProviderStoreError(
                    'provider settings schema is unsupported')
            stored = document.get('provider')
            if not isinstance(stored, dict):
                raise ProviderStoreError(
                    'provider settings payload is invalid')
            ciphertext = stored.get('secret_envelope')
            if not isinstance(ciphertext, str) or not ciphertext:
                raise ProviderStoreError(
                    'provider secret envelope is missing')
            try:
                plaintext = self._fernet(create=False).decrypt(
                    ciphertext.encode('ascii'))
                secrets = json.loads(plaintext.decode('utf-8'))
            except ProviderStoreError:
                raise
            except (InvalidToken, UnicodeError, ValueError, TypeError,
                    json.JSONDecodeError) as exc:
                raise ProviderStoreError(
                    'provider secrets could not be decrypted') from exc
            if not isinstance(secrets, dict) \
                    or secrets.get('schema_version') != _SCHEMA_VERSION:
                raise ProviderStoreError(
                    'provider secret envelope is invalid')
            return ProviderConfig(
                base_url=stored.get('base_url') or '',
                model=stored.get('model') or '',
                api_key=secrets.get('api_key') or '',
                extra_headers=secrets.get('extra_headers') or {},
                thinking_format=stored.get('thinking_format') or '',
                capabilities=stored.get('capabilities') or (),
            )

    def save(self, provider: ProviderConfig) -> None:
        """Encrypt secrets, then atomically replace the settings file."""
        if not isinstance(provider, ProviderConfig):
            raise TypeError('provider must be ProviderConfig')
        with self._lock:
            secret_payload = json.dumps({
                'schema_version': _SCHEMA_VERSION,
                'api_key': provider.api_key,
                'extra_headers': dict(provider.extra_headers),
            }, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
            ciphertext = self._fernet(create=True).encrypt(
                secret_payload.encode('utf-8')).decode('ascii')
            document = {
                'schema_version': _SCHEMA_VERSION,
                'provider': {
                    'base_url': provider.base_url,
                    'model': provider.model,
                    'thinking_format': provider.thinking_format,
                    'capabilities': sorted(provider.capabilities),
                    'secret_envelope': ciphertext,
                },
            }
            encoded = (json.dumps(
                document, ensure_ascii=False, indent=2, sort_keys=True)
                + '\n').encode('utf-8')
            self._atomic_write(self.path, encoded)

    def delete(self) -> bool:
        """Remove the Provider document but retain its encryption key."""
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise ProviderStoreError(
                    'provider settings could not be removed') from exc
            return True


__all__ = [
    'ProviderSettingsStore',
    'ProviderStoreError',
    'default_provider_config_path',
    'secret_hint',
]
