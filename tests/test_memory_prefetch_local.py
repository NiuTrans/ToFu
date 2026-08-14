"""Precision-first local memory surfacing and its orchestrator seam."""

from __future__ import annotations

import threading

import pytest


pytestmark = pytest.mark.unit


def _messages(text='fix ParserState in lib/parser/state.py'):
    return [{'role': 'user', 'content': text}]


def _memories():
    return [
        {
            'name': 'parser-state-rollback',
            'description': 'ParserState rollback rule in lib/parser/state.py',
            'tags': ['parser', 'rollback'],
            'body': 'Preserve the rollback branch.',
            'scope': 'project',
            'filepath': '/p/.tofu/memories/parser.md',
        },
        {
            'name': 'parser-formatting',
            'description': 'ParserState formatting convention',
            'tags': ['parser'],
            'body': 'Use compact formatting.',
            'scope': 'project',
            'filepath': '/p/.tofu/memories/format.md',
        },
        {
            'name': 'unrelated',
            'description': 'CSS palette and typography notes',
            'tags': ['design'],
            'body': 'Blue.',
            'scope': 'project',
            'filepath': '/p/.tofu/memories/css.md',
        },
    ]


def test_prefetch_is_local_metadata_only_and_bounded(monkeypatch):
    import lib.llm_dispatch as llm_dispatch
    from lib.memory.prefetch import _run

    monkeypatch.setattr(
        'lib.memory.storage.get_eligible_memories',
        lambda *a, **k: _memories())
    monkeypatch.setattr(
        llm_dispatch, 'dispatch_chat',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('memory prefetch must not call an LLM')))

    task = {'id': 'memory-local', 'convId': 'conv-local'}
    selected = _run.run_memory_prefetch(_messages(), '/p', task=task)

    assert 1 <= len(selected) <= 2
    assert all(row['name'].startswith('parser-') for row in selected)
    assert task['_prefetchedMemories'] == selected
    assert task['_memoryPrefetch']['auxiliaryLlmCalls'] == 0
    assert task['_memoryPrefetch']['strategy'] == 'local_high_confidence'


def test_low_confidence_candidates_are_not_injected(monkeypatch):
    from lib.memory.prefetch import _run

    monkeypatch.setattr(
        'lib.memory.storage.get_eligible_memories',
        lambda *a, **k: _memories())
    task = {'id': 'memory-low', 'convId': 'conv-low'}
    selected = _run.run_memory_prefetch(
        _messages('please make this nicer'), '/p', task=task)
    assert selected == []
    assert task['_prefetchedMemories'] == []


def test_one_generic_token_is_not_treated_as_an_identifier(monkeypatch):
    from lib.memory.prefetch import _run

    monkeypatch.setattr(
        'lib.memory.storage.get_eligible_memories',
        lambda *a, **k: _memories())
    selected = _run.run_memory_prefetch(
        _messages('parser please'), '/p',
        task={'id': 'memory-one-token', 'convId': 'conv-one-token'})
    assert selected == []


def test_todo_identifiers_can_raise_confidence(monkeypatch):
    from lib.memory.prefetch import _run

    monkeypatch.setattr(
        'lib.memory.storage.get_eligible_memories',
        lambda *a, **k: _memories())
    task = {
        'id': 'memory-todo', 'convId': 'conv-todo',
        '_todos': [{'content': 'Inspect lib/parser/state.py'}],
    }
    selected = _run.run_memory_prefetch(
        _messages('continue'), '/p', task=task)
    assert selected
    assert selected[0]['_prefetch_reason'].startswith('exact_identifier:')


def test_orchestrator_runs_inline_and_does_not_mutate_messages(monkeypatch):
    import lib.memory.prefetch as prefetch
    from lib.tasks_pkg.orchestrator import _memory_prefetch as seam

    calls = []

    def fake(messages, **kwargs):
        calls.append((threading.current_thread().name, kwargs))
        kwargs['task']['_prefetchedMemories'] = [{'name': 'picked'}]
        return kwargs['task']['_prefetchedMemories']

    monkeypatch.setattr(prefetch, 'run_memory_prefetch', fake)
    task = {'id': 'inline', 'convId': 'conv', 'events': [],
            'events_lock': threading.Lock()}
    messages = _messages()
    before = list(messages)
    seam.maybe_run_memory_prefetch(
        task=task, cfg={}, messages=messages,
        tool_list=[{'function': {'name': 'read_files'}}],
        project_path='/p', project_enabled=True, memory_enabled=True,
        has_real_tools=True, injected_tool_calls=0)

    assert calls and calls[0][0] == threading.current_thread().name
    assert messages == before
    assert '_checkpointUsage' not in task


@pytest.mark.parametrize('override', [
    {'memory_enabled': False},
    {'has_real_tools': False},
    {'injected_tool_calls': 1},
])
def test_orchestrator_gates_local_prefetch(monkeypatch, override):
    import lib.memory.prefetch as prefetch
    from lib.tasks_pkg.orchestrator import _memory_prefetch as seam

    monkeypatch.setattr(
        prefetch, 'run_memory_prefetch',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('prefetch should have been gated')))
    kwargs = dict(
        task={'id': 'gate'}, cfg={}, messages=_messages(), tool_list=[],
        project_path='/p', project_enabled=True, memory_enabled=True,
        has_real_tools=True, injected_tool_calls=0)
    kwargs.update(override)
    seam.maybe_run_memory_prefetch(**kwargs)
    assert kwargs['task']['_prefetchedMemories'] == []
