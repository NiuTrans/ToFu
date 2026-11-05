"""Request-body compression matching a provider's first-party client.

Responsibility: encode an already-sealed JSON request body for the wire.
Codex CLI compresses ChatGPT-backend ``/responses`` bodies with zstd at level 3
and sends ``Content-Encoding: zstd`` (openai/codex
``http-client/src/request.rs``). The level is that client's constant, not a
local tuning choice.

Python 3.14 ships ``compression.zstd``; earlier interpreters use the declared
``zstandard`` wheel. An install that lacks both keeps identity encoding, which
the backend also accepts, so a stale environment never fails a request.
Entry points: :func:`zstd_available`, :func:`zstd_compress`.
"""

from __future__ import annotations

ZSTD = 'zstd'
CODEX_CLI_ZSTD_LEVEL = 3

_codec = None
_resolved = False


def _resolve_codec():
    global _codec, _resolved
    if _resolved:
        return _codec
    try:
        from compression import zstd as stdlib_zstd
        _codec = lambda data: stdlib_zstd.compress(  # noqa: E731
            data, level=CODEX_CLI_ZSTD_LEVEL)
    except ImportError:
        try:
            import zstandard
        except ImportError:
            _codec = None
        else:
            # One-shot API: a fresh compressor per call, so concurrent
            # request threads never share a (non-thread-safe) context.
            _codec = lambda data: zstandard.compress(  # noqa: E731
                data, CODEX_CLI_ZSTD_LEVEL)
    _resolved = True
    return _codec


def zstd_available() -> bool:
    return _resolve_codec() is not None


def zstd_compress(data: bytes) -> bytes:
    codec = _resolve_codec()
    if codec is None:
        raise RuntimeError('zstd request encoding requested without a codec')
    return codec(data)


__all__ = ['CODEX_CLI_ZSTD_LEVEL', 'ZSTD', 'zstd_available', 'zstd_compress']
