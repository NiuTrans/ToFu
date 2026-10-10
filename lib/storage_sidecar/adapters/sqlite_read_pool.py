"""SQLite reset/classification adapter for shared connection lease ownership."""
import sqlite3
from lib.storage_sidecar.adapters.connection_pool import (
    ConnectionSlot as _SQLiteReadSlot_fixture, DemandConnectionPool as DemandConnectionPool,
)
SQLiteReadSlot = _SQLiteReadSlot_fixture


class DemandSQLiteReadPool(DemandConnectionPool):
    def __init__(self, connect, *, idle_reuse_hint, max_lifetime_s=float('inf'),
                 idle_lifetime_s=float('inf'), classify_error=None):
        super().__init__(lambda deadline_at: connect(), idle_reuse_hint=idle_reuse_hint,
            max_lifetime_s=max_lifetime_s, idle_lifetime_s=idle_lifetime_s,
            classify_error=classify_error, driver_errors=(sqlite3.Error,),
            reset=lambda connection: connection.set_progress_handler(None, 0))
