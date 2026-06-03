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
  Memories (both global and project-scoped) are stored under
  ``<project>/.tofu/skills/`` — no external ``~/`` dependency. This
  module does NOT touch memory paths.
"""

import os

from lib.log import get_logger

logger = get_logger(__name__)

# ── Project base directory (same as lib/database.py BASE_DIR) ──
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Per-project config directory ──
CONFIG_DIR = os.path.join(_BASE_DIR, 'data', 'config')


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


# ── Auto-create config dir on import ──
_ensure_config_dir()
