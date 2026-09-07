"""Kubernetes exec-probe for an independently managed Storage Sidecar."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any

from lib.storage.connection_file import read_connection_file


def _read_connection() -> dict[str, Any]:
    raw_path = os.environ.get('TOFU_STORAGE_CONNECTION_FILE', '').strip()
    if not raw_path:
        raise RuntimeError('storage connection file is not configured')
    return read_connection_file(Path(raw_path))


def _storage_is_ready(connection: dict[str, Any]) -> bool:
    from lib.storage.client import StorageClient

    client = StorageClient(
        connection['host'], connection['port'], connection['token'])
    return bool(client.health(deadline=1.5).get('ready'))


def main(argv: list[str] | None = None) -> int:
    """Run a dependency-aware readiness probe or local-only liveness probe."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments not in ([], ['--liveness']):
        return 2
    try:
        connection = _read_connection()
        if arguments == ['--liveness']:
            # Reading the Pod-local handoff proves that this Sidecar completed
            # startup without making liveness depend on PostgreSQL readiness.
            return 0
        return 0 if _storage_is_ready(connection) else 2
    except Exception as exc:
        print(
            f'storage probe failed: {type(exc).__name__}',
            file=sys.stderr,
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())


__all__ = ['main']
