"""Bounded slide-media I/O shared by authoring, localisation, and export.

Responsibility: enforce the slide capability's per-image byte ceiling while
hashing/copying local files and consuming remote HTTP streams.  Recipes own
which assets are useful and their aggregate job budget; renderers/exporters
own format interpretation.  This module never retains a process-wide cache.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import stat
from collections.abc import Callable

MAX_SLIDE_IMAGE_BYTES = 20 * 1024 * 1024

_STREAM_CHUNK_BYTES = 256 * 1024

__all__ = [
    'MAX_SLIDE_IMAGE_BYTES',
    'copy_file_bounded',
    'decode_image_base64_bounded',
    'download_bytes_bounded',
    'download_file_bounded',
    'hash_file_bounded',
]


def _positive_limit(max_bytes: int) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise ValueError('max_bytes must be a positive integer')
    if not 0 < max_bytes <= MAX_SLIDE_IMAGE_BYTES:
        raise ValueError(
            f'max_bytes must be within 1..{MAX_SLIDE_IMAGE_BYTES}')
    return max_bytes


def hash_file_bounded(path: str, *,
                      max_bytes: int = MAX_SLIDE_IMAGE_BYTES) -> tuple[str, int]:
    """Return ``(sha256, bytes)`` for one regular file without whole-file RAM."""
    limit = _positive_limit(max_bytes)
    digest = hashlib.sha256()
    total = 0
    with open(path, 'rb') as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('slide image source must be a regular file')
        if not 0 < metadata.st_size <= limit:
            raise ValueError(
                f'slide image size {metadata.st_size} is outside 1..{limit}')
        while True:
            chunk = source.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError(f'slide image exceeds {limit} bytes')
            digest.update(chunk)
    if total != metadata.st_size:
        raise ValueError('slide image changed while it was being hashed')
    return digest.hexdigest(), total


def copy_file_bounded(source_path: str, destination_path: str, *,
                      expected_sha256: str, expected_bytes: int,
                      max_bytes: int = MAX_SLIDE_IMAGE_BYTES) -> None:
    """Atomically copy a file and fail if it changed after the hash pass."""
    limit = _positive_limit(max_bytes)
    if not 0 < expected_bytes <= limit:
        raise ValueError('expected_bytes is outside the slide image limit')
    from lib.json_store import atomic_output_path

    digest = hashlib.sha256()
    total = 0
    with atomic_output_path(destination_path) as temporary_path:
        with open(source_path, 'rb') as source, open(temporary_path, 'wb') as out:
            metadata = os.fstat(source.fileno())
            if (not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size != expected_bytes):
                raise ValueError('slide image changed before it was copied')
            while True:
                chunk = source.read(_STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError(f'slide image exceeds {limit} bytes')
                out.write(chunk)
                digest.update(chunk)
        if total != expected_bytes or digest.hexdigest() != expected_sha256:
            raise ValueError('slide image changed while it was being copied')


def decode_image_base64_bounded(
        encoded: str, *, max_bytes: int = MAX_SLIDE_IMAGE_BYTES) -> bytes:
    """Decode provider image base64 after rejecting oversized text up front."""
    limit = _positive_limit(max_bytes)
    if not isinstance(encoded, str) or not encoded:
        raise ValueError('image provider returned empty base64')
    max_encoded = ((limit + 2) // 3) * 4 + 8
    if len(encoded) > max_encoded:
        raise ValueError(f'encoded slide image exceeds {limit} decoded bytes')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('image provider returned invalid base64') from exc
    if not 0 < len(raw) <= limit:
        raise ValueError(f'decoded slide image is outside 1..{limit} bytes')
    return raw


def _stream_http(
        url: str, sink: Callable[[bytes], object], *, max_bytes: int,
        min_bytes: int, timeout: float,
        abort_check=None) -> tuple[int, str, str]:
    limit = _positive_limit(max_bytes)
    if isinstance(min_bytes, bool) or not isinstance(min_bytes, int):
        raise ValueError('min_bytes must be a non-negative integer')
    minimum = max(0, min(min_bytes, limit))
    if abort_check is not None and abort_check():
        raise InterruptedError('slide media download aborted')

    from lib.http_client import http_stream
    total = 0
    digest = hashlib.sha256()
    with http_stream('GET', url, timeout=timeout) as response:
        status = getattr(response, 'status_code', 0)
        if status != 200:
            raise ValueError(f'HTTP {status}')
        headers = getattr(response, 'headers', {}) or {}
        declared_raw = (headers.get('Content-Length')
                        or headers.get('content-length'))
        try:
            declared = int(declared_raw) if declared_raw is not None else None
        except (TypeError, ValueError):
            declared = None
        if declared is not None and declared > limit:
            raise ValueError(
                f'declared slide image size {declared} exceeds {limit}')
        for chunk in response.iter_content(chunk_size=_STREAM_CHUNK_BYTES):
            if abort_check is not None and abort_check():
                raise InterruptedError('slide media download aborted')
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise ValueError(f'slide image stream exceeds {limit} bytes')
            sink(chunk)
            digest.update(chunk)
        if abort_check is not None and abort_check():
            raise InterruptedError('slide media download aborted')
        if total < minimum:
            raise ValueError(
                f'slide image stream has {total} bytes; minimum is {minimum}')
        content_type = str(headers.get('Content-Type')
                           or headers.get('content-type') or '')
    return total, content_type, digest.hexdigest()


def download_bytes_bounded(
        url: str, *, max_bytes: int = MAX_SLIDE_IMAGE_BYTES,
        min_bytes: int = 1, timeout: float = 60,
        abort_check=None) -> tuple[bytes, str]:
    """Download one remote image into a bounded byte buffer."""
    data = bytearray()
    _size, content_type, _sha256 = _stream_http(
        url, data.extend, max_bytes=max_bytes, min_bytes=min_bytes,
        timeout=timeout, abort_check=abort_check)
    return bytes(data), content_type


def download_file_bounded(
        url: str, path: str, *, max_bytes: int = MAX_SLIDE_IMAGE_BYTES,
        min_bytes: int = 1, timeout: float = 60,
        abort_check=None) -> tuple[int, str, str]:
    """Stream one remote image to an atomically published local file."""
    from lib.json_store import atomic_output_path

    with atomic_output_path(path) as temporary_path:
        with open(temporary_path, 'wb') as output:
            return _stream_http(
                url, output.write, max_bytes=max_bytes, min_bytes=min_bytes,
                timeout=timeout, abort_check=abort_check)
