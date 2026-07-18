#!/usr/bin/env python3
"""Regression: the boot console echo must never be fatal.

On an in-place restart (``os.execv`` from the /api/v1/update/restart button)
the child inherits fd 2 as a pipe whose reader has already gone away. The very
first ``_boot()`` progress line then writes to that dead pipe and, unguarded,
raises ``BrokenPipeError`` — killing boot before any module loads (observed
2026-07-11: the re-exec'd server died in ``_boot`` with ``BrokenPipeError:
[Errno 32] Broken pipe``). The authoritative boot record is ``_boot_logger``
(→ app.log); the stderr echo is cosmetic and must be best-effort.

Run:
    pytest tests/test_server_boot_broken_pipe.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def server_module():
    try:
        import quart  # noqa: F401
        import hypercorn  # noqa: F401
    except ImportError as e:
        pytest.skip(f'quart/hypercorn not installed: {e}')
    import server
    return server


def test_boot_survives_broken_stderr_pipe(server_module, monkeypatch, caplog):
    """_boot() must swallow BrokenPipeError from the stderr echo and still log."""
    class _BrokenStderr:
        def write(self, _s):
            raise BrokenPipeError(32, 'Broken pipe')

        def flush(self):
            raise BrokenPipeError(32, 'Broken pipe')

    monkeypatch.setattr(server_module.sys, 'stderr', _BrokenStderr())

    with caplog.at_level('INFO', logger='server.boot'):
        # Must NOT raise — this is exactly the first-line-of-boot scenario.
        server_module._boot('probe %d', 1)

    # The authoritative record still lands in the logger.
    assert any('probe 1' in r.getMessage() for r in caplog.records)


def test_boot_writes_to_healthy_stderr(server_module, monkeypatch):
    """When stderr is healthy the echo is emitted (no silent regression)."""
    captured = []

    class _OkStderr:
        def write(self, s):
            captured.append(s)

        def flush(self):
            pass

    monkeypatch.setattr(server_module.sys, 'stderr', _OkStderr())
    server_module._boot('hello %s', 'world')
    assert any('hello world' in c for c in captured)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
