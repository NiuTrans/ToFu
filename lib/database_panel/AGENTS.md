# lib/database_panel/

Settings → 数据库 panel backend. Authority contract: `docs/STORAGE.md`
("Settings database panel"). Frontend owner:
`frontend/src/features/settings/database.ts`; routes:
`routes/api_v1/database_panel.py`.

- `config_store.py` — preferences + last-backup witness
  (`data/config/database_panel.json`).
- `probe.py` — lightweight runtime status and read-only filesystem evidence; optional path capability discovery (10-min cache) is separate from settings entry. Full engine/authority admission remains in switch/launch owners.
- `switch.py` — `.env` backend flip + sqlite→tofudb import job. Also
  publishes the selection to `os.environ` because the approval-gated
  restart re-execs this process and dotenv never overrides inherited
  variables.
- `backup.py` — mirrors `system.backup` artifact + manifest to the user's
  stable directory; prunes only its own ledger entries.
- `maintenance.py` — one daemon thread for daily dual-path backup; no idle benchmark.
- `lib/storage_authority_sql/native_fastpath_settings.py` — native next-launch configuration and activation/lineage lock boundary; no live migration. The panel exposes optional Fast Path for both engines independently of required native search placement.

Tests: `tests/database_panel/`, `tests/frontend/test_database_path_settings.py`,
`tests/visual/test_database_projection_settings.py`.
Never import database drivers here — the storage boundary scanner
(`lib/storage/storage_boundary.py`) fails startup on driver names outside the
storage layer.
