"""Panel preferences and last-backup summary persistence.

One small JSON document at ``data/config/database_panel.json``.  Writes go
through ``lib.json_store.update_json_atomic`` like every other config
store, so concurrent route + maintenance-thread writers stay consistent.
"""

from __future__ import annotations

import time
from typing import Any

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic

_PANEL_CONFIG_NAME = 'database_panel.json'

_DEFAULTS: dict[str, Any] = {
    'backup': {'enabled': False, 'directory': ''},
    'lastBackup': None,
}


def _path() -> str:
    return config_path(_PANEL_CONFIG_NAME)


def load_preferences() -> dict[str, Any]:
    """Return preferences merged over defaults; never raises."""
    raw = read_json(_path(), default={})
    if not isinstance(raw, dict):
        raw = {}
    merged = dict(_DEFAULTS)
    merged['backup'] = dict(_DEFAULTS['backup'])
    for key, value in raw.items():
        if key == 'backup' and isinstance(value, dict):
            merged['backup'].update(
                {k: v for k, v in value.items()
                 if k in ('enabled', 'directory')})
        elif key in ('lastBackup',):
            merged[key] = value
    merged['backup']['enabled'] = bool(merged['backup']['enabled'])
    merged['backup']['directory'] = str(merged['backup']['directory'] or '')
    return merged


def save_preferences(*, backup_enabled: bool | None = None,
                     backup_directory: str | None = None) -> dict[str, Any]:
    """Persist one preference patch and return the merged result."""
    def _mutate(current: Any) -> dict[str, Any]:
        cfg = current if isinstance(current, dict) else {}
        cfg = dict(cfg)
        backup = dict(cfg.get('backup') or {})
        if backup_enabled is not None:
            backup['enabled'] = bool(backup_enabled)
        if backup_directory is not None:
            backup['directory'] = backup_directory
        cfg['backup'] = backup
        return cfg

    update_json_atomic(_path(), _mutate, default={}, mode=0o600)
    return load_preferences()


def record_last_backup(summary: dict[str, Any]) -> None:
    """Store the bounded last-backup witness (ok, at, bytes, directory)."""
    bounded = {
        'ok': bool(summary.get('ok')),
        'at': float(summary.get('at') or time.time()),
        'bytes': int(summary.get('bytes') or 0),
        'directory': str(summary.get('directory') or ''),
        'error': str(summary.get('error') or '')[:300],
    }

    def _mutate(current: Any) -> dict[str, Any]:
        cfg = current if isinstance(current, dict) else {}
        cfg = dict(cfg)
        cfg['lastBackup'] = bounded
        return cfg

    update_json_atomic(_path(), _mutate, default={}, mode=0o600)
