"""lib/agent_core/profiles.py — Declarative capability profiles.

A *profile* is a named bundle of agent-run config defaults — which tools are
on, which model to prefer, whether swarm/endpoint modes are enabled.  It turns
deployment from "the caller must remember to send the right 12 cfg toggles"
into "the caller (or operator) picks a profile name".

Where this sits
---------------
``cfg`` (the free-form dict that flows route → orchestrator → model_config /
tool registry) is the runtime source of truth.  A profile supplies *defaults*
that the explicit per-request ``cfg`` overrides key-by-key:

    effective_cfg = {**profile_defaults, **request_cfg}

So a profile never fights an explicit caller value — it only fills gaps.  The
orchestrator applies this once, at the top of ``run_task``, before model
resolution and tool assembly, so every downstream consumer sees merged values.

Sources (later wins on key collision)
-------------------------------------
1. Built-in profiles below (``_BUILTINS``) — always available, vanilla-safe.
2. ``data/config/profiles/<name>.json`` — operator-supplied overrides /
   additions.  A file named the same as a built-in REPLACES it.

Selection
---------
A request selects a profile via ``cfg['profile']`` (camelCase wire key
``profile``).  No profile / unknown name → the ``'default'`` profile, which is
empty (pure pass-through), so existing callers are unaffected.

Design contract
---------------
* **Additive & non-breaking.**  Absent ``cfg['profile']`` ⇒ behaviour is
  byte-identical to before this module existed.
* **Explicit cfg always wins.**  Profiles are defaults, never overrides.
* **camelCase keys.**  Profile JSON uses the same wire keys as ``cfg`` /
  ``TofuOptions`` (``searchMode``, ``swarmEnabled``, ``model``, …).
"""

from __future__ import annotations

from typing import Any

from lib.config_dir import config_path
from lib.json_store import read_json
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'list_profiles',
    'get_profile',
    'apply_profile',
    'resolve_profile_name',
]

_PROFILES_DIR = config_path('profiles')

# ── Built-in profiles (vanilla-safe; operators extend via JSON) ──
# Keys are camelCase wire keys (same as cfg / TofuOptions).  An empty profile
# is pure pass-through.  These intentionally name NO concrete infrastructure.
_BUILTINS: dict[str, dict[str, Any]] = {
    # The implicit profile when none is selected — changes nothing.
    'default': {},

    # Read-only research assistant: web + memory, no project writes, no swarm.
    'research': {
        'searchMode': 'multi',
        'fetchEnabled': True,
        'memoryEnabled': True,
        'swarmEnabled': False,
        'codeExecEnabled': False,
    },

    # Project co-pilot: full project tools, search off by default to keep the
    # model focused on the codebase (caller can still flip searchMode on).
    'coding': {
        'searchMode': 'off',
        'fetchEnabled': True,
        'memoryEnabled': True,
        'swarmEnabled': False,
    },

    # Minimal: only the always-on read_files + nothing else.  Useful for
    # cheap, tightly-scoped Q&A or ablation baselines.
    'minimal': {
        'searchMode': 'off',
        'fetchEnabled': False,
        'memoryEnabled': False,
        'mcpEnabled': False,
        'swarmEnabled': False,
        'codeExecEnabled': False,
    },
}


def _load_file_profiles() -> dict[str, dict[str, Any]]:
    """Load operator profiles from ``data/config/profiles/*.json``.

    Each file ``<name>.json`` contributes (or overrides) profile ``<name>``.
    Malformed files are logged and skipped — never fatal.
    """
    import glob
    import os

    out: dict[str, dict[str, Any]] = {}
    pattern = os.path.join(_PROFILES_DIR, '*.json')
    for path in sorted(glob.glob(pattern)):
        name = os.path.splitext(os.path.basename(path))[0]
        data = read_json(path, default=None)
        if not isinstance(data, dict):
            logger.warning('[Profiles] %s is not a JSON object — skipped', path)
            continue
        out[name] = data
        logger.debug('[Profiles] loaded operator profile %r from %s', name, path)
    return out


def list_profiles() -> dict[str, dict[str, Any]]:
    """Return all known profiles (built-ins overlaid with operator files)."""
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in _BUILTINS.items()}
    merged.update(_load_file_profiles())
    return merged


def get_profile(name: str) -> dict[str, Any]:
    """Return the cfg-default dict for *name*, or ``{}`` if unknown.

    Operator file profiles override built-ins of the same name.
    """
    if not name:
        return {}
    file_profiles = _load_file_profiles()
    if name in file_profiles:
        return dict(file_profiles[name])
    if name in _BUILTINS:
        return dict(_BUILTINS[name])
    logger.warning('[Profiles] unknown profile %r — using empty defaults', name)
    return {}


def resolve_profile_name(cfg: dict | None) -> str:
    """Extract the selected profile name from a cfg dict.

    Reads ``cfg['profile']``; falls back to ``'default'`` (the no-op profile).
    """
    if not cfg or not isinstance(cfg, dict):
        return 'default'
    name = cfg.get('profile')
    if isinstance(name, str) and name.strip():
        return name.strip()
    return 'default'


def apply_profile(cfg: dict | None) -> dict:
    """Merge the selected profile's defaults UNDER the explicit cfg.

    ``effective = {**profile_defaults, **cfg}`` — the request's explicit values
    always win; the profile only fills keys the caller didn't set.

    Returns a NEW dict; the input ``cfg`` is not mutated.  Absent / 'default'
    profile ⇒ returns a shallow copy of ``cfg`` unchanged.
    """
    base = dict(cfg or {})
    name = resolve_profile_name(base)
    if name == 'default':
        return base
    defaults = get_profile(name)
    if not defaults:
        return base
    # Profile fills gaps; explicit cfg overrides. The 'profile' key itself is
    # preserved so downstream/logging can see which profile was applied.
    effective = {**defaults, **base}
    _filled = [k for k in defaults if k not in base]
    logger.info('[Profiles] applied profile=%r — filled %d default(s): %s',
                name, len(_filled), ', '.join(sorted(_filled))[:200])
    return effective
