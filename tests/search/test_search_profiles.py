"""Search profile preset and backward-compatible override contracts."""

from __future__ import annotations

import pytest

from lib.search_profiles import resolve_search_profile

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(('profile', 'top_n', 'chars', 'filter_on', 'deepen'), [
    ('fast', 3, 30_000, False, False),
    ('balanced', 6, 60_000, True, False),
    ('deep', 10, 100_000, True, True),
])
def test_profile_presets(profile, top_n, chars, filter_on, deepen):
    resolved = resolve_search_profile({'profile': profile})
    assert resolved['fetch_top_n'] == top_n
    assert resolved['max_chars_search'] == chars
    assert resolved['llm_content_filter'] is filter_on
    assert resolved['deepen_enabled'] is deepen


def test_explicit_overrides_win_and_legacy_concrete_keys_win_last():
    resolved = resolve_search_profile({
        'profile': 'deep',
        'overrides': {'fetch_top_n': 8, 'max_chars_search': 70_000},
        'fetch_top_n': 4,
    })
    assert resolved['fetch_top_n'] == 4
    assert resolved['max_chars_search'] == 70_000
    assert resolved['deepen_enabled'] is True


def test_unknown_profile_falls_back_to_balanced():
    assert resolve_search_profile({'profile': 'unknown'})['profile'] == 'balanced'


def test_legacy_concrete_values_are_exposed_as_custom_overrides():
    resolved = resolve_search_profile({'fetch_top_n': 9,
                                       'llm_content_filter': False})
    assert resolved['overrides'] == {
        'fetch_top_n': 9,
        'llm_content_filter': False,
    }
