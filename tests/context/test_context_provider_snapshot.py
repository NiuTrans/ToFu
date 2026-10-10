"""Context reads keep detached metadata while history stays with its owner."""
from __future__ import annotations

from concurrent.futures import Future
import threading
import pytest
from lib.tasks_pkg.context_composer import ComposeRequest
from lib.tasks_pkg.context_composer import _providers as providers
from lib.tasks_pkg.context_composer._provider_snapshot import context_provider_snapshot

pytestmark = pytest.mark.unit


class UnreadableBuffer(list):
    def __iter__(self):
        raise AssertionError('context provider traversed an executor buffer')


def test_snapshot_is_independent_of_history_and_preserves_identity():
    future = Future()
    abort = threading.Event()
    task = {'id': 'task', '_userId': 7, 'config': {'projectPaths': ['/project']},
            '_prefetch_project': future, 'abort_event': abort,
            '_selectedSkillsSnapshot': {'receipts': [{'status': 'loaded'}]},
            'messages': UnreadableBuffer(), 'events': UnreadableBuffer(),
            '_turn_input_messages': UnreadableBuffer(),
            'result': UnreadableBuffer(), 'toolRounds': UnreadableBuffer()}
    snapshot = context_provider_snapshot(task)
    assert snapshot['id'] == 'task' and snapshot['_userId'] == 7
    assert snapshot['_prefetch_project'] is future
    assert snapshot['abort_event'] is abort
    snapshot['config']['projectPaths'].append('/other')
    snapshot['_selectedSkillsSnapshot']['receipts'][0]['status'] = 'changed'
    assert task['config']['projectPaths'] == ['/project']
    assert task['_selectedSkillsSnapshot']['receipts'][0]['status'] == 'loaded'
    assert 'messages' not in snapshot and 'events' not in snapshot


def test_composer_returns_provider_receipt_without_copying_history(monkeypatch):
    task = {'id': 'task', '_userId': 7, 'config': {'projectPaths': ['/project']},
            'messages': UnreadableBuffer(), 'events': UnreadableBuffer()}

    def profile(request):
        assert request.user_id == 7
        assert request.task['config']['projectPaths'] == ['/project']
        request.task['config']['projectPaths'].append('/provider-only')
        request.task['_appliedPreferences'] = {'chars': 5}
        return 'prefs'

    monkeypatch.setattr(providers, '_profile_block', profile)
    monkeypatch.setattr(providers.system_prompt_base, 'build_static_prompt',
                        lambda **kwargs: 'static')
    monkeypatch.setattr(providers, '_project_rules', lambda request: '')
    monkeypatch.setattr(providers, '_memory_guidance', lambda request: '')
    monkeypatch.setattr(providers, '_skill_context', lambda request, query: {
        'index': '', 'selection': {'content': '', 'receipts': []}})
    monkeypatch.setattr(providers, '_swarm_guidance', lambda request, query: '')
    monkeypatch.setattr(providers, '_project_blocks', lambda request, query: [])
    blocks = providers.collect_context_blocks(
        [{'role': 'user', 'content': 'request'}], ComposeRequest(task=task, user_id=7))
    assert task['config']['projectPaths'] == ['/project']
    assert task['_appliedPreferences'] == {'chars': 5}
    assert any(block.content == 'prefs' for block in blocks)
