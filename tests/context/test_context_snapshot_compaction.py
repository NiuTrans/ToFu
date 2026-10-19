"""Superseded snapshots retire only with verified, current replacement bytes."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.context_composer import (
    ComposeRequest, compact_context_snapshots, context_block,
)
from lib.tasks_pkg.context_composer._render import render_context

pytestmark = pytest.mark.unit


def render(messages, *blocks):
    render_context(messages, [context_block(key, value) for key, value in blocks], ComposeRequest())


def test_mixed_carrier_keeps_current_siblings_without_mutating_source():
    messages = [{'role': 'user', 'content': 'Human request'}]
    render(messages, ('project_rules', 'OLD RULES'), ('project_scoped_rules', 'KEEP SIBLING'))
    render(messages, ('project_rules', 'CURRENT RULES'))
    original = deepcopy(messages)
    projected = compact_context_snapshots(messages)
    assert messages == original
    assert projected[-1] == original[-1]
    assert 'KEEP SIBLING' in str(projected)
    assert 'OLD RULES' not in str(projected)
    assert compact_context_snapshots(projected) == projected
    # Recomposition must reuse both surviving sections, not restore the old one.
    before = deepcopy(projected)
    render(projected, ('project_rules', 'CURRENT RULES'), ('project_scoped_rules', 'KEEP SIBLING'))
    assert projected == before
    # The planner's restoration reserve also counts mixed siblings once.
    from lib.tasks_pkg.compaction._layer2._compact import _with_restoration_reserve
    reserved = _with_restoration_reserve(projected, original)
    assert str(reserved).count('KEEP SIBLING') == 1
    assert str(reserved).count('CURRENT RULES') == 1
    assert 'OLD RULES' not in str(reserved)


@pytest.mark.parametrize('defect', ['hash', 'missing_end', 'role', 'unowned', 'media'])
def test_invalid_newer_snapshot_cannot_retire_previous_policy(defect):
    messages = []
    render(messages, ('project_rules', 'OLD RULES'))
    render(messages, ('project_rules', 'CURRENT RULES'))
    latest = messages[-1]
    if defect == 'hash':
        latest['_contextBlocks'][0]['hash'] = 'tampered'
    elif defect == 'missing_end':
        latest['content'][0]['text'] = latest['content'][0]['text'].replace(':end -->', ':broken -->')
    elif defect == 'role':
        latest['role'] = 'tool'
    elif defect == 'unowned':
        latest.pop('_contextComposer')
    elif defect == 'media':
        latest['content'].append({'type': 'image_url', 'image_url': {'url': 'fixture://image'}})
    original = deepcopy(messages)
    assert compact_context_snapshots(messages) == original
    assert messages == original


def test_human_quoted_markers_and_required_system_policy_are_untouched():
    messages = []
    render(messages, ('platform_static', 'Required policy'), ('project_rules', 'OLD RULES'))
    quoted = {'role': 'user', 'content': deepcopy(messages[-1]['content'])}
    messages.append(quoted)
    render(messages, ('project_rules', 'CURRENT RULES'))
    projected = compact_context_snapshots(messages)
    assert messages[0] in projected
    assert quoted in projected
    assert projected[-1] == messages[-1]


def test_withdrawal_is_current_snapshot_and_survives_retirement():
    messages = []
    render(messages, ('project_rules', 'OLD RULES'))
    render_context(messages, [context_block('project_rules', '', reason='project_empty')], ComposeRequest())
    projected = compact_context_snapshots(messages)
    assert len(projected) == 1 and projected[0] == messages[-1]
    assert 'no longer active' in str(projected)


def test_restoration_reserve_cannot_replace_fresh_candidate_state_with_old_source():
    from lib.tasks_pkg.compaction._layer2._compact import _with_restoration_reserve
    source = []
    render(source, ('active_checklist', 'OLD CHECKLIST'))
    candidate = []
    render(candidate, ('active_checklist', 'CURRENT CHECKLIST with additional required work'))
    reserved = _with_restoration_reserve(candidate, source)
    assert candidate[0] in reserved
    assert 'OLD CHECKLIST' not in str(reserved)
