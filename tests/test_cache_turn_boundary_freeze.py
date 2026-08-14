"""Composer cache-boundary contracts.

The old implementation froze and restored hidden context across tasks. The
Composer instead keeps historical user messages immutable and carries every
volatile block in synthetic head/tail messages that can be replaced as a unit.
"""

from __future__ import annotations

import copy

import pytest


pytestmark = pytest.mark.unit


def _inject(messages, task=None):
    from lib.tasks_pkg.context_composer import (
        ComposeRequest,
        ContextBlock,
        render_context,
    )
    blocks = [
        ContextBlock(
            id='stable_rules', source='test.rules', content='RULES',
            authority='project', placement='head', stability='conversation',
            lifecycle='conversation'),
        ContextBlock(
            id='volatile_tail', source='test.turn', content='TURN STATE',
            authority='ambient', placement='tail', stability='turn',
            lifecycle='task'),
    ]
    result = render_context(messages, blocks, ComposeRequest(task=task))
    if task is not None:
        task['_contextManifest'] = result.manifest
    return result


def test_historical_messages_are_byte_identical_after_composition():
    messages = [
        {'role': 'system', 'content': 'base'},
        {'role': 'user', 'content': 'Q1'},
        {'role': 'assistant', 'content': 'A1'},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': 'Q2'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,x'}},
        ]},
    ]
    original = copy.deepcopy(messages)
    _inject(messages)
    # System gets its own structured blocks; every historical non-system row
    # remains byte-identical and in the same relative order.
    history = [m for m in messages if not m.get('_contextComposer')][1:]
    assert history == original[1:]


def test_reentry_replaces_managed_context_without_duplication():
    messages = [{'role': 'user', 'content': 'Q'}]
    _inject(messages)
    _inject(messages)
    text = repr(messages)
    assert text.count('tofu-context:stable_rules:start') == 1
    assert text.count('tofu-context:volatile_tail:start') == 1


def test_manifest_exposes_stability_and_lifecycle():
    task = {}
    result = _inject([{'role': 'user', 'content': 'Q'}], task)
    rows = {row['id']: row for row in result.manifest}
    assert rows['stable_rules']['stability'] == 'conversation'
    assert rows['stable_rules']['lifecycle'] == 'conversation'
    assert rows['volatile_tail']['stability'] == 'turn'
    assert rows['volatile_tail']['lifecycle'] == 'task'
    assert task['_contextManifest'] == result.manifest
