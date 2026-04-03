"""lib/config_dir.py — Per-project config directory.

All persistent config lives in ``<project>/data/config/`` so that
multiple copies of chatui on the same machine stay fully isolated.

Contents:
  data/config/server_config.json  — providers, models, presets, search
  data/config/features.json       — feature flags (trading_enabled etc.)
  data/config/daily_reports/      — daily task reports

Migration:
  On first run, if ``data/config/server_config.json`` does NOT exist but
  ``~/.chatui/server_config.json`` DOES, we copy it once (convenience for
  the main project owner).  Exported copies never have ``data/`` so they
  always start fresh.

Note:
  Skills (both global and project-scoped) are now stored under
  ``<project>/.chatui/skills/`` — no external ``~/.chatui/`` dependency.
  This module does NOT touch skills paths.
"""

import os
import shutil

from lib.log import get_logger

logger = get_logger(__name__)

# ── Project base directory (same as lib/database.py BASE_DIR) ──
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Per-project config directory ──
CONFIG_DIR = os.path.join(_BASE_DIR, 'data', 'config')

# ── Legacy global config directory ──
_LEGACY_DIR = os.path.join(os.path.expanduser('~'), '.chatui')


def _ensure_config_dir():
    """Create data/config/ if it doesn't exist."""
    os.makedirs(CONFIG_DIR, exist_ok=True)


def _maybe_migrate_from_legacy():
    """One-time migration from ~/.chatui/ to data/config/.

    Only copies if local config doesn't exist yet AND legacy does.
    This allows the main project to inherit its existing config,
    while exported copies (which have empty data/) start fresh.
    """
    local_cfg = os.path.join(CONFIG_DIR, 'server_config.json')
    legacy_cfg = os.path.join(_LEGACY_DIR, 'server_config.json')

    if os.path.isfile(local_cfg):
        return  # already have local config — nothing to do

    if not os.path.isfile(legacy_cfg):
        return  # no legacy config either — start fresh

    _ensure_config_dir()

    # Copy server_config.json
    try:
        shutil.copy2(legacy_cfg, local_cfg)
    except Exception as e:
        logger.warning('Failed to migrate legacy server_config.json: %s', e)

    # Copy features.json if present
    legacy_feat = os.path.join(_LEGACY_DIR, 'features.json')
    local_feat = os.path.join(CONFIG_DIR, 'features.json')
    if os.path.isfile(legacy_feat) and not os.path.isfile(local_feat):
        try:
            shutil.copy2(legacy_feat, local_feat)
        except Exception as e:
            logger.warning('Failed to migrate legacy features.json: %s', e)

    # Copy daily_reports/ if present
    legacy_reports = os.path.join(_LEGACY_DIR, 'daily_reports')
    local_reports = os.path.join(CONFIG_DIR, 'daily_reports')
    if os.path.isdir(legacy_reports) and not os.path.isdir(local_reports):
        try:
            shutil.copytree(legacy_reports, local_reports)
        except Exception as e:
            logger.warning('Failed to migrate legacy daily_reports/: %s', e)


def config_path(*parts):
    """Build a path under data/config/.

    Usage:
        config_path('server_config.json')
        config_path('daily_reports', '2026-04-01.json')
    """
    return os.path.join(CONFIG_DIR, *parts)


# ── Auto-migrate on import ──
_ensure_config_dir()
_maybe_migrate_from_legacy()
