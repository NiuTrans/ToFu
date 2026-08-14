"""Canonical Context Composer ordering, lifecycle, and observability."""

from __future__ import annotations

import copy

import pytest

from lib.tasks_pkg.context_composer import (
    ComposeRequest,
    ContextBlock,
    append_context_blocks,
    render_context,
)


pytestmark = pytest.mark.unit


def _block(block_id, content, *, authority='ambient', placement='tail',
           priority=10, dedupe_key=''):
    return ContextBlock(
        id=block_id, source=f'test.{block_id}', content=content,
        authority=authority, placement=placement, stability='turn',
        lifecycle='task', priority=priority, dedupe_key=dedupe_key,
    )


def _texts(message):
    content = message.get('content')
    if isinstance(content, str):
        return content
    return '\n'.join(block.get('text', '') for block in content or []
                     if isinstance(block, dict))


def test_authority_order_is_deterministic_and_higher_authority_is_closer():
    messages = [
        {'role': 'system', 'content': 'operator system'},
        {'role': 'user', 'content': 'request'},
    ]
    blocks = [
        _block('ambient', 'ambient body', authority='ambient',
               placement='system'),
        _block('platform', 'platform body', authority='platform',
               placement='system'),
        _block('workflow', 'workflow body', authority='workflow',
               placement='system'),
    ]
    result = render_context(messages, blocks, ComposeRequest(model='m'))
    system = _texts(result.messages[0])
    assert system.index('operator system') < system.index('ambient body')
    assert system.index('ambient body') < system.index('workflow body')
    assert system.index('workflow body') < system.index('platform body')
    assert [row['id'] for row in result.manifest if row['injected']] == [
        'ambient', 'workflow', 'platform']


def test_head_tail_placement_and_single_reminder_wrapper():
    messages = [{'role': 'user', 'content': 'real request'}]
    blocks = [
        _block('rules', '<system-reminder>\nRULES\n</system-reminder>',
               authority='project', placement='head'),
        _block('evidence', 'EVIDENCE', authority='evidence', placement='tail'),
    ]
    result = render_context(messages, blocks, ComposeRequest())
    assert result.messages[0]['_contextComposer'] is True
    assert result.messages[-1]['_contextComposer'] is True
    assert _texts(result.messages[0]).count('<system-reminder>') == 1
    assert 'RULES' in _texts(result.messages[0])
    assert 'EVIDENCE' in _texts(result.messages[-1])


def test_dedupe_manifest_explains_suppression_and_rerender_is_idempotent():
    original = [{'role': 'user', 'content': 'request'}]
    messages = copy.deepcopy(original)
    blocks = [
        _block('winner', 'ONE', dedupe_key='same'),
        _block('loser', 'TWO', priority=20, dedupe_key='same'),
    ]
    first = render_context(messages, blocks, ComposeRequest())
    loser = next(row for row in first.manifest if row['id'] == 'loser')
    assert loser['injected'] is False
    assert loser['reason'] == 'duplicate_of:winner'

    second = render_context(messages, blocks, ComposeRequest())
    joined = '\n'.join(_texts(message) for message in second.messages)
    assert joined.count('tofu-context:winner:start') == 1
    assert 'TWO' not in joined


def test_round_append_preserves_stable_prefix_and_extends_task_manifest():
    task = {'_contextManifest': [{'id': 'stable', 'injected': True}]}
    messages = [
        {'role': 'system', 'content': 'stable system'},
        {'role': 'user', 'content': 'stable request'},
    ]
    prefix = copy.deepcopy(messages)
    append_context_blocks(
        messages,
        [_block('attachment', 'TODO state', placement='tail')],
        ComposeRequest(task=task),
    )
    assert messages[:2] == prefix
    assert messages[-1]['_contextComposer'] is True
    assert task['_contextManifest'][-1]['id'] == 'attachment'


def test_manifest_contains_budget_hash_and_provenance():
    block = ContextBlock(
        id='budgeted', source='test.source', content='x' * 2000,
        authority='evidence', placement='tail', stability='turn',
        lifecycle='task', max_tokens=10, provenance={'match': 'exact'},
    )
    result = render_context(
        [{'role': 'user', 'content': 'request'}], [block],
        ComposeRequest(model=''),
    )
    row = result.manifest[0]
    assert row['injected'] is True
    assert row['reason'] == 'truncated'
    assert row['hash']
    assert row['tokens'] > 0
    assert row['provenance'] == {'match': 'exact'}
    assert row['order'] == 0
