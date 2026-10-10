"""Settings → 数据库 panel backend.

User-facing surface for the storage authority: engine status and backend
switching (TofuDB <-> SQLite, persisted to the project .env and applied by
the existing restart machinery), storage-path discovery with an accelerated
write front, and dual-path verified backup mirroring.

Everything here respects the one-authority rule in docs/STORAGE.md: the
panel never opens a second store against live data and never weakens the
selected backend; configuration changes take effect only through restart.
"""
"""
 Owner modules:
 - config_store: panel preferences + last-backup summary persistence
 - probe:        engine status and storage-path discovery
 - switch:       backend switching + offline SQLite→TofuDB import job
 - backup:       verified snapshot mirroring to a second path
 - maintenance:  daily backup scheduler
"""
