"""Driver-bearing storage backends.  No other package may import a DB driver."""

from __future__ import annotations

from lib.storage_sidecar.config import SidecarConfig
from lib.storage.startup_control import StartupProgressCallback


def create_backend(
    config: SidecarConfig,
    *,
    startup_progress: StartupProgressCallback | None = None,
):
    if config.backend == 'sqlite':
        from lib.storage_sidecar.adapters.sqlite import SQLiteBackend
        return SQLiteBackend(config, startup_progress=startup_progress)
    if config.backend == 'postgres':
        from lib.storage_sidecar.adapters.postgres import PostgresBackend
        return PostgresBackend(config)
    raise RuntimeError('unsupported storage backend')


__all__ = ['create_backend']
