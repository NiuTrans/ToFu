"""Pinned downloads with a content-addressed cache.

Entry points: ``fetch_pinned`` (download once, verify SHA-256 and size, reuse
by digest) and ``fetch_bytes`` (unpinned reads, used only by ``resolve`` for
publisher indexes and checksum files). Proxies come from the standard
``https_proxy``/``no_proxy`` environment. Dependencies: stdlib.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import urllib.request

_CHUNK = 1024 * 1024


class FetchError(RuntimeError):
    """A download failed or did not match its pin."""


def fetch_bytes(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url) as response:
            return response.read()
    except OSError as error:
        raise FetchError(f'GET {url}: {error}') from error


def fetch_pinned(url: str, sha256: str, size: int | None, cache: Path) -> Path:
    """Return ``cache/<sha256>/<basename>``, downloading it if absent."""
    name = url.rsplit('/', 1)[-1].split('?', 1)[0]
    target = cache / sha256 / urllib.request.url2pathname(name).replace('/', '_')
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    received = 0
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        try:
            with urllib.request.urlopen(url) as response:
                for chunk in iter(lambda: response.read(_CHUNK), b''):
                    digest.update(chunk)
                    received += len(chunk)
                    handle.write(chunk)
        except OSError as error:
            os.unlink(handle.name)
            raise FetchError(f'GET {url}: {error}') from error
    if digest.hexdigest() != sha256 or (size is not None and received != size):
        os.unlink(handle.name)
        raise FetchError(
            f'{url}: expected sha256 {sha256} ({size} bytes), got {digest.hexdigest()} '
            f'({received} bytes)')
    os.replace(handle.name, target)
    return target
