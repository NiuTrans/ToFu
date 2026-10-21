"""Render-local hash reuse cannot authorize later working-fact dependencies.

Preparation verifies actual admitted content; final-wire observation also
rechecks bytes instead of inheriting stale or unowned task metadata.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg.context_composer._render import (
    _managed_block_hashes, verified_block_hash)


def _composer_msg(block_id, body):
    return {'role': 'user', '_contextComposer': True,
            'content': [{'type': 'text', 'text': (
                f'<!-- tofu-context:{block_id}:start -->\n'
                '<system-reminder>\n[Context authority: workflow; source: test]\n'
                f'{body}\n</system-reminder>\n'
                f'<!-- tofu-context:{block_id}:end -->')}]}


def test_verified_block_hash_rechecks_current_content():
    messages = [_composer_msg('working_facts', 'the facts')]
    before = verified_block_hash(messages, 'working_facts')
    messages[0]['content'][0]['text'] = _composer_msg(
        'working_facts', 'different evidence')['content'][0]['text']
    after = verified_block_hash(messages, 'working_facts')
    assert before != after
    assert after == _managed_block_hashes(messages)['working_facts']


def test_wire_hash_rechecks_bytes_without_composer_metadata():
    messages = [_composer_msg('working_facts', 'the facts')]
    expected = _managed_block_hashes(messages)['working_facts']
    messages[0].pop('_contextComposer')
    result = verified_block_hash(messages, 'working_facts', wire=True)
    assert result == expected


@pytest.mark.parametrize('change', ['unchanged', 'append', 'new_list', 'replace', 'edit_content'])
def test_prepare_binds_only_the_actual_admitted_content(change, monkeypatch):
    from lib.identity import PrincipalContext
    from lib.tasks_pkg import context_dependencies

    messages = [_composer_msg('working_facts', 'the facts')]
    hashes = _managed_block_hashes(messages)
    principal = PrincipalContext.user(subject_id='fact-owner', owner_user_id=1)
    task = {'id': 'consumer-task', 'convId': 'consumer-conversation', '_userId': 1,
            '_principalContext': principal.to_payload(),
            '_managedBlockHashes': {'_messages_identity': id(messages),
                                    '_messages_length': len(messages), 'map': hashes},
            '_contextManifest': [{'id': 'working_facts', 'injected': True,
                'appended': True, 'hash': hashes['working_facts'],
                'provenance': {'conversationId': 'consumer-conversation',
                    'taskId': 'consumer-task', 'facts': [{'id': 'fact-a', 'revision': 2}]}}]}
    prepared = []
    monkeypatch.setattr('lib.memory.working_facts.consumers.prepare',
                        lambda **kwargs: prepared.append(kwargs))
    if change == 'append':
        messages.append({'role': 'user', 'content': 'continue'})
    elif change == 'new_list':
        messages = list(messages)
    elif change == 'replace':
        messages[0] = _composer_msg('working_facts', 'different evidence')
    elif change == 'edit_content':
        messages[0]['content'][0]['text'] = _composer_msg(
            'working_facts', 'different evidence')['content'][0]['text']

    context_dependencies.prepare(task, messages)
    if change in {'replace', 'edit_content'}:
        assert prepared == []
        assert '_workingFactDependencyPending' not in task
    else:
        assert len(prepared) == 1
        assert prepared[0]['principal'] == principal
        assert prepared[0]['conversation_id'] == task['convId']
        assert prepared[0]['task_id'] == task['id']
        assert prepared[0]['facts'] == [{'id': 'fact-a', 'revision': 2}]
        pending = task['_workingFactDependencyPending']
        assert pending['hash'] == verified_block_hash(messages, 'working_facts')
        assert not pending['wireMatched'] and pending['binding']
