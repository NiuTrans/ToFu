"""lib/config_dir.py — Per-project config directory.

All persistent config lives in ``<project>/data/config/`` so that
multiple copies of Tofu on the same machine stay fully isolated.

Contents:
  data/config/server_config.json  — providers, models, presets, search
  data/config/features.json       — feature flags (trading_enabled etc.)
  data/config/daily_reports/      — daily task reports
  data/config/api_keys.json       — Bearer-token store (lib/api_keys.py)
  data/config/.first_run_token    — plaintext bootstrap admin token (0600)
  data/config/auth.json           — auth-mode policy (lib/auth_mode.py)
  data/config/pricing.json        — model price table (lib/billing/pricing.py)
  data/config/payments.json       — Stripe/Alipay credentials + minor-unit
                                     conversion (lib/billing/payments/)
  data/config/relay.json          — relay onboarding policy (signup_enabled,
                                     welcome credit, default role)
  data/config/usage.json          — per-key usage counters

  All of the above are auto-excluded from `export.py` exports (the
  whole `data/` directory is in `ALWAYS_EXCLUDE_DIRS`). Adding a new
  config file under data/config/ inherits that exclusion; if you ever
  need a config file OUTSIDE this directory, update `export.py`.

Note:
  Project-scoped MEMORIES live under ``<project>/.tofu/memories/`` and
  project-scoped SKILL PACKAGES under ``<project>/.tofu/skills/<id>/`` —
  two different nouns, split 2026-07 (they travel with the project tree).
  GLOBAL memories live in the server-side
  store ``<data>/memories/global/`` (``$TOFU_DATA_DIR`` or ``<root>/data``)
  so they are shared across all projects and reachable with no project
  attached — see ``lib/memory/storage.py``. Both inherit ``data/``'s
  export exclusion. This module does NOT touch memory paths.
"""

import os

from lib.log import get_logger
from lib.runtime_paths import data_root

logger = get_logger(__name__)

# ── Per-project config directory ──
# Anchored to the WRITABLE data root (see lib/runtime_paths) so a frozen
# desktop install writes config under a user-writable dir, not the read-only
# _internal/ bundle.
CONFIG_DIR = os.path.join(data_root(), 'config')


def _ensure_config_dir():
    """Create data/config/ if it doesn't exist."""
    os.makedirs(CONFIG_DIR, exist_ok=True)


def config_path(*parts):
    """Build a path under data/config/.

    Usage:
        config_path('server_config.json')
        config_path('daily_reports', '2026-04-01.json')
    """
    return os.path.join(CONFIG_DIR, *parts)


# ── Per-project fetched-files staging directory ──
# data/fetched/ holds file assets downloaded by the fetch_url tool that
# cannot be extracted as text (images, archives, office docs, …). The model
# is handed the saved path and reads it back via read_files. Lives under the
# fully-gitignored data/ tree, so it stays isolated per project copy and is
# never exported.
FETCHED_DIR = os.path.join(data_root(), 'fetched')


def fetched_path(*parts):
    """Build a path under data/fetched/, creating the directory on first use."""
    os.makedirs(FETCHED_DIR, exist_ok=True)
    return os.path.join(FETCHED_DIR, *parts)


# ── Auto-create config dir on import ──
_ensure_config_dir()
