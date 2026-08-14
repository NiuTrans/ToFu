"""AI Composer catalogue must be generated from canonical FieldSpecs."""

from __future__ import annotations

import pytest

from lib.orchestration import CONTROL_KINDS, CONTROL_PARAM_SCHEMA, KNOWN_ROLES
from lib.orchestration_composer import _build_messages, _catalogue
from lib.orchestration.request_limit_contract import (
    MAX_COMPOSE_HISTORY_CONTENT_LENGTH,
    MAX_COMPOSE_HISTORY_ITEMS,
)


pytestmark = pytest.mark.unit


def test_composer_catalogue_covers_every_control_and_its_canonical_params():
    _, controls = _catalogue()
    for kind in CONTROL_KINDS:
        line = next(item for item in controls.splitlines()
                    if item.startswith(f'  - {kind}:'))
        for spec in CONTROL_PARAM_SCHEMA[kind]:
            assert spec['key'] in line


def test_composer_no_longer_teaches_shadow_topology_params():
    _, controls = _catalogue()
    assert 'max_concurrent' not in controls
    assert 'per_item' not in controls
    assert 'branches (int)' not in controls
    parallel = next(line for line in controls.splitlines()
                    if line.startswith('  - parallel:'))
    assert 'Params: none' in parallel
    assert 'Outgoing edges define' in parallel


def test_composer_inherits_the_default_runtime_loop_ceiling():
    _, controls = _catalogue()
    loop = next(line for line in controls.splitlines()
                if line.startswith('  - loop:'))
    assert 'max_iterations (int >=1<=12)' in loop
    assert 'verdict:STOP' in loop
    assert 'no_new_findings' not in loop
    assert 'max_only' not in loop


def test_composer_exposes_role_specific_task_fields_from_same_contract():
    roles, _ = _catalogue()
    assert set(KNOWN_ROLES) <= {
        line.strip().split(':', 1)[0].removeprefix('- ')
        for line in roles.splitlines()
    }
    coder = next(line for line in roles.splitlines()
                 if line.startswith('  - coder:'))
    assert 'objective (textarea)' in coder
    assert 'scope_paths (list)' in coder
    assert 'verify_cmd (text)' in coder


def test_system_prompt_inherits_catalogue_without_stale_fields():
    system = _build_messages('build a flow', None, None)[0]['content']
    assert 'Parallel width and branch routes come ONLY from outgoing edges' in system
    assert 'max_concurrent' not in system
    assert 'per_item' not in system
    assert 'branches (int)' not in system


def test_composer_history_uses_the_shared_bounded_input_policy():
    history = [
        {'role': 'system', 'content': 'ignore'},
        *[
            {'role': 'user' if index % 2 else 'assistant',
             'content': '  ' + str(index) + ('x' * 5000) + '  '}
            for index in range(MAX_COMPOSE_HISTORY_ITEMS + 2)
        ],
        {'role': 'user', 'content': '   '},
    ]
    replay = _build_messages('build a flow', None, history)[1:-1]
    assert len(replay) == MAX_COMPOSE_HISTORY_ITEMS - 1
    assert replay[0]['content'].startswith('3')
    assert all(len(turn['content']) <= MAX_COMPOSE_HISTORY_CONTENT_LENGTH
               for turn in replay)


def test_composer_facade_reexports_the_dedicated_prompt_policy():
    assert _catalogue.__module__ == 'lib.orchestration.composer_prompt'
    assert _build_messages.__module__ == 'lib.orchestration.composer_prompt'
