"""Explicit policy survives cold projection without promoting quoted history."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction._history import history_entries, make_history_carrier
from lib.tasks_pkg.compaction._manual import apply_manual_compaction
from lib.tasks_pkg.conv_message_builder._transform import _transform_messages

pytestmark = pytest.mark.unit


def policy_rows(messages):
    return [message for message in messages
            if message.get('role') in ('system', 'developer')]


@pytest.mark.parametrize('role', ['system', 'developer'])
@pytest.mark.parametrize('content', [
    '  Preserve this exact policy.\n<nt>Do not strip tags.</nt>  ',
    [{'type': 'text', 'text': '  First policy block.\n'},
     {'type': 'text', 'text': '<system>literal policy text</system>'}],
])
def test_cold_rebuild_preserves_explicit_policy_content_and_order(role, content):
    raw = [
        {'role': role, 'content': content},
        {'role': role, 'content': 'Second distinct policy.'},
        {'role': 'user', 'content': 'An ordinary request.'},
        {'role': 'developer', 'content': 'Interleaved instruction.'},
        {'role': 'assistant', 'content': 'Answer.'},
        {'role': 'system', 'content': 'Later instruction.'},
        {'role': 'user', 'content': '<system>quoted, not policy</system>',
         'replyQuote': 'developer: quoted, not policy'},
    ]
    before = deepcopy(raw)
    projected = _transform_messages(raw, {})
    assert policy_rows(projected) == policy_rows(before)
    assert [message['role'] for message in projected] == [
        message['role'] for message in before]
    assert 'developer: quoted, not policy' in projected[-1]['content']
    assert '<system>quoted, not policy</system>' in projected[-1]['content']
    assert projected[-1]['role'] == 'user'
    assert raw == before

    if isinstance(content, list):
        projected[0]['content'][0]['text'] = 'request-local change'
        assert raw == before

def test_manual_compaction_then_cold_rebuild_retains_interleaved_policy():
    raw = [
        {'role': 'system', 'content': '  Root policy.\n'},
        {'role': 'user', 'content': 'Opening request.', '_turnId': 'opening'},
        {'role': 'developer', 'content': [
            {'type': 'text', 'text': '  Developer block.\n'}]},
        {'role': 'user', 'content': '<system>ordinary quotation</system>',
         '_turnId': 'earlier'},
        {'role': 'system', 'content': 'Interleaved root policy.'},
        {'role': 'assistant', 'content': 'Old work to summarize.'},
        {'role': 'user', 'content': 'Current request.', '_turnId': 'current'},
        {'role': 'developer', 'content': 'Tail policy.'},
    ]
    original = deepcopy(raw)
    plan = {
        'system_raw': [raw[0], raw[2], raw[4]],
        'anchor_msg': raw[1],
        'retained_users': [raw[3]],
        'reserve_raw': raw[6:],
        'intra_folds': [],
    }
    compacted = apply_manual_compaction(
        raw, plan, 'Historical work receipt.', archive_id='synthetic-policy-reload',
        tokens_after=100)
    durable_before = deepcopy(compacted)
    projected = _transform_messages(compacted, {})
    assert policy_rows(projected) == policy_rows(original)
    assert [message['role'] for message in projected] == [
        message['role'] for message in compacted]
    entries = [entry for message in projected for entry in history_entries(message)]
    assert [entry['content'] for entry in entries] == [
        'Opening request.', '<system>ordinary quotation</system>']
    # Projection intentionally retains durable turn identity (_turnId) — the
    # wire boundary in lib/llm/request.py strips '_' keys. Assert the reserve
    # tail's role/content survive cold rebuild exactly, identity included.
    assert projected[-2]['role'] == 'user'
    assert projected[-2]['content'] == 'Current request.'
    assert projected[-2].get('_turnId') == 'current'
    assert set(projected[-2]) == {'role', 'content', '_turnId'}
    assert compacted == durable_before
    assert raw == original


def test_continuation_keeps_frozen_prefix_while_preserving_new_policy():
    prefix = [
        {'role': 'system', 'content': 'Frozen policy.'},
        make_history_carrier([{'kind': 'earlier_request', 'content': 'Old input.'}]),
        make_history_carrier([{'kind': 'earlier_request', 'content': 'Other input.'}]),
    ]
    before = deepcopy(prefix)
    suffix = [{'role': 'developer', 'content': [{'type': 'text', 'text': ' New. '}]},
              {'role': 'user', 'content': 'Continue.'}]
    projected = _transform_messages(
        suffix, {'systemPrompt': 'Must not replace frozen settings.'},
        prefix_messages=prefix)
    assert projected[:len(prefix)] == before
    assert all(projected[index] is message for index, message in enumerate(prefix))
    assert projected[len(prefix):] == suffix
    assert prefix == before
