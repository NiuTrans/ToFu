"""Built-in spec list structure: canonical order survives the family split."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _specs():
    from lib.tools.registry._builtin_specs import build_builtin_specs
    return build_builtin_specs()


def test_no_duplicate_keys_and_custom_is_always_last():
    specs = _specs()
    keys = [s.key for s in specs]
    assert len(keys) == len(set(keys))
    assert keys[-1] == 'custom'  # task-local handlers ride last by contract
    assert keys.count('custom') == 1


def test_hot_base_tools_keep_prompt_cache_positions():
    """Append-only cache contract: the hottest base tools keep their
    relative order — a family-section edit must never reshuffle them."""
    keys = [s.key for s in _specs()]
    hot = ['search', 'fetch', 'read_files', 'inspect_image', 'project']
    positions = [keys.index(k) for k in hot]
    assert positions == sorted(positions)
    assert all(s.phase == 'base' for s in _specs()
               if s.key in {'search', 'fetch', 'read_files', 'project'})


def test_base_phase_precedes_capability_phase():
    phases = [s.phase for s in _specs()]
    seen_capability = False
    for phase in phases:
        if phase == 'capability':
            seen_capability = True
        else:
            assert not seen_capability, (
                'base-phase spec after a capability spec breaks the '
                'has_base_tools assembly contract')


def test_every_spec_has_category_description_and_gate():
    for spec in _specs():
        assert spec.category and spec.description and spec.gate, spec.key
        assert spec.phase in ('base', 'capability'), spec.key
