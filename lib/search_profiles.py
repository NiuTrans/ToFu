"""Search-depth presets with backward-compatible concrete overrides."""

from __future__ import annotations

SEARCH_PROFILES = {
    'fast': {
        'fetch_top_n': 3,
        'max_chars_search': 30_000,
        'llm_content_filter': False,
        'deepen_enabled': False,
    },
    'balanced': {
        'fetch_top_n': 6,
        'max_chars_search': 60_000,
        'llm_content_filter': True,
        'deepen_enabled': False,
    },
    'deep': {
        'fetch_top_n': 10,
        'max_chars_search': 100_000,
        'llm_content_filter': True,
        'deepen_enabled': True,
    },
}

PROFILE_KEYS = frozenset(next(iter(SEARCH_PROFILES.values())))


def normalize_profile(value) -> str:
    value = str(value or 'balanced').strip().lower()
    return value if value in SEARCH_PROFILES else 'balanced'


def resolve_search_profile(raw: dict | None) -> dict:
    """Resolve preset → overrides → legacy concrete keys.

    Existing concrete settings remain accepted and win as overrides, which
    preserves old ``server_config.json`` files and the agent settings tool.
    """
    raw = dict(raw or {})
    profile = normalize_profile(raw.get('profile'))
    resolved = dict(raw)
    preset = dict(SEARCH_PROFILES[profile])
    overrides = raw.get('overrides') if isinstance(raw.get('overrides'), dict) else {}
    effective = dict(preset)
    for key, value in overrides.items():
        if key in PROFILE_KEYS:
            effective[key] = value
    for key in PROFILE_KEYS:
        if key in raw:
            effective[key] = raw[key]
    resolved.update(effective)
    resolved['profile'] = profile
    normalized_overrides = {key: value for key, value in overrides.items()
                            if key in PROFILE_KEYS}
    # A pre-profile config stored concrete knobs at the search root. Surface
    # those as custom overrides so the new Settings page preserves them on
    # first load instead of silently snapping back to the balanced preset.
    for key in PROFILE_KEYS:
        if key in raw:
            normalized_overrides[key] = raw[key]
    resolved['overrides'] = normalized_overrides
    return resolved


__all__ = ['SEARCH_PROFILES', 'PROFILE_KEYS', 'normalize_profile',
           'resolve_search_profile']
