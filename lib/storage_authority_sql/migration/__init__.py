"""Offline SQLite → native TofuDB migration toolkit.

This subpackage is an operator-only, explicitly-invoked boundary. It is never
imported on the application startup, wire, or live serving path: the only
runtime consumer is ``lib.database_panel.switch``, which launches
``lib.storage_authority_sql.migration.tofudb_migration_import`` as a separate
subprocess. See ``AGENTS.md`` in this directory for the per-module rules.
"""
