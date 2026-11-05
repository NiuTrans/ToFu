"""Authenticated handoff between co-located app and storage containers.

The Sidecar publishes one short-lived, mode-0600 JSON document on a shared
Pod ``emptyDir`` after it has bound its loopback listener. The application
validates and consumes that document instead of spawning a child process.
No database credential is copied into it; the token authenticates only the
Pod-local storage RPC transport.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import re
import uuid
from typing import Any

from lib.storage.protocol import PROTOCOL_VERSION


CONNECTION_FILE_FORMAT = 'tofu.storage-connection/v1'
_NATIVE_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / 'contracts/native_storage_connection_v2.json')
    .read_text(encoding='utf-8'))
NATIVE_CONNECTION_FILE_FORMAT = _NATIVE_SCHEMA['properties']['format']['const']
_MAX_CONNECTION_FILE_BYTES = _NATIVE_SCHEMA['x-maxBytes']


def resolve_connection_file(raw_path: str | os.PathLike[str]) -> Path:
    """Resolve an absolute connection path whose parent already exists."""
    path = Path(raw_path)
    if not path.is_absolute():
        raise RuntimeError('TOFU_STORAGE_CONNECTION_FILE must be absolute')
    parent = path.parent.resolve()
    if not parent.is_dir():
        raise RuntimeError('storage connection-file directory does not exist')
    return parent / path.name


def _validate_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise RuntimeError('storage connection file must contain an object')
    native = document.get('format') == NATIVE_CONNECTION_FILE_FORMAT
    expected_keys = (set(_NATIVE_SCHEMA['required']) if native else
                     {'format', 'protocol', 'host', 'port', 'token', 'backend'})
    if set(document) != expected_keys:
        raise RuntimeError('storage connection file has an invalid schema')
    if not native and document.get('format') != CONNECTION_FILE_FORMAT:
        raise RuntimeError('storage connection file format mismatch')
    protocol = _NATIVE_SCHEMA['properties']['protocol']['const'] if native else PROTOCOL_VERSION
    if (document.get('protocol') != protocol
            or (native and type(document.get('protocol')) is not int)):
        raise RuntimeError('storage connection file protocol mismatch')
    if document.get('host') not in {'127.0.0.1', 'localhost', '::1'}:
        raise RuntimeError('storage connection endpoint must be loopback-only')
    port = document.get('port')
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RuntimeError('storage connection file has an invalid port')
    token = document.get('token')
    if not isinstance(token, str) or len(token) < 32 or len(token) > 512:
        raise RuntimeError('storage connection file has an invalid token')
    if document.get('backend') not in {'sqlite', 'postgres', 'tofudb'}:
        raise RuntimeError('storage connection file has an invalid backend')
    if native:
        if document['backend'] != 'tofudb' or len(token) > 256 or not token.isascii():
            raise RuntimeError('native storage connection identity is invalid')
        for name in ('ownerId', 'pid', 'processStartTime'):
            if type(document[name]) is not int or document[name] <= 0:
                raise RuntimeError('native storage connection identity is invalid')
        tenant = document['tenantId']
        if tenant is not None and (type(tenant) is not int or tenant <= 0):
            raise RuntimeError('native storage tenant is invalid')
        for name in ('projectPath', 'dataDirectory'):
            path = document[name]
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise RuntimeError('native storage project/data directory is invalid')
        for name in ('binarySha256', 'serviceId'):
            value = document[name]
            if not isinstance(value, str) or not re.fullmatch(
                    _NATIVE_SCHEMA['properties'][name]['pattern'], value):
                raise RuntimeError('native storage generation is invalid')
        projection = document['searchProjectionDirectory']
        if not isinstance(projection, str) or not Path(projection).is_absolute():
            raise RuntimeError('native search projection directory is invalid')
        fastpath = document['nativeFastpath']
        if fastpath is not None:
            expected = _NATIVE_SCHEMA['properties']['nativeFastpath']['required']
            if not isinstance(fastpath, dict) or set(fastpath) != set(expected):
                raise RuntimeError('native fastpath descriptor is invalid')
            for name in ('durableAuthority', 'localAuthority', 'shadowDirectory'):
                if not isinstance(fastpath[name], str) or not Path(fastpath[name]).is_absolute():
                    raise RuntimeError('native fastpath directory is invalid')
            interval = fastpath['syncIntervalMs']
            if interval is not None and (type(interval) is not int or interval <= 0):
                raise RuntimeError('native fastpath interval is invalid')
    return dict(document)


def read_connection_file(path: Path) -> dict[str, Any]:
    """Read and validate one private, non-symlink connection document."""
    resolved = resolve_connection_file(path)
    try:
        metadata = resolved.lstat()
    except OSError as exc:
        raise RuntimeError('storage connection file is not available') from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError('storage connection file must be a regular file')
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError('storage connection file permissions are too broad')
    if not 0 < metadata.st_size <= _MAX_CONNECTION_FILE_BYTES:
        raise RuntimeError('storage connection file has an invalid size')
    try:
        document = json.loads(resolved.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError('storage connection file is unreadable') from exc
    return _validate_document(document)


def write_connection_file(
    path: Path,
    *,
    host: str,
    port: int,
    token: str,
    backend: str,
) -> None:
    """Atomically publish one fsynced private connection document."""
    document = _validate_document({
        'format': CONNECTION_FILE_FORMAT,
        'protocol': PROTOCOL_VERSION,
        'host': host,
        'port': port,
        'token': token,
        'backend': backend,
    })
    write_connection_document(path, document)


def write_connection_document(path: Path, document: dict[str, Any]) -> None:
    """Publish either negotiated transport through the same private file owner."""
    resolved = resolve_connection_file(path)
    document = _validate_document(document)
    encoded = json.dumps(
        document, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if len(encoded) > _MAX_CONNECTION_FILE_BYTES:
        raise RuntimeError('storage connection file has an invalid size')
    temporary = resolved.with_name(f'.{resolved.name}.tmp-{uuid.uuid4().hex}')
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, resolved)
        directory_descriptor = os.open(resolved.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def remove_connection_file(path: Path, *, token: str) -> bool:
    """Remove only the document still carrying this Sidecar's token."""
    resolved = resolve_connection_file(path)
    try:
        document = read_connection_file(resolved)
    except RuntimeError:
        return False
    if document['token'] != token:
        return False
    resolved.unlink(missing_ok=True)
    return True


__all__ = [
    'CONNECTION_FILE_FORMAT',
    'read_connection_file',
    'remove_connection_file',
    'resolve_connection_file',
    'write_connection_file',
    'write_connection_document',
    'NATIVE_CONNECTION_FILE_FORMAT',
]
