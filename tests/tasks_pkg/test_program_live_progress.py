"""Settled children become visible before guest continuation, without replay."""
import copy
import json

import pytest

from lib.tasks_pkg.orchestrator import _programmatic as projection
from tests.tasks_pkg.test_javascript_dependency_chains import local_gateway  # noqa: F401

pytestmark = pytest.mark.unit


def schema(name):
    return {'type': 'function', 'function': {'name': name,
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}


def capture(monkeypatch, task):
    rows = []
    append = projection.append_event

    def observe(carrier, event):
        if event['type'].startswith('program_'):
            from lib.turn_lifecycle import _task_projection
            parent = next(row for row in carrier['toolRounds'] if row.get('_programSynthetic'))
            persisted = _task_projection(carrier, {}, event)
            durable_parent = next(row for row in persisted['toolRounds'] if row.get('_programSynthetic'))
            assert durable_parent['childCallIds'] == parent['childCallIds']
            assert durable_parent['programStatus'] == parent['programStatus']
            rows.append({'event': copy.deepcopy(event), 'parent': copy.deepcopy(parent),
                         'durableParent': durable_parent})
        append(carrier, event)

    monkeypatch.setattr(projection, 'append_event', observe)
    return rows


@pytest.mark.parametrize('batch', [False, True])
def test_progress_precedes_dependent_guest_continuation(local_gateway, monkeypatch, batch):
    task, dispatched, invoke = local_gateway
    snapshots = capture(monkeypatch, task)

    def next_read(args):
        updates = [r for r in snapshots if r['event']['type'] == 'program_progress']
        assert len(updates) == 1
        parent = updates[0]['parent']
        assert parent['status'] == 'searching' and parent['programStatus'] == 'running'
        assert len(parent['childCallIds']) == (2 if batch else 1)
        assert not any(r['event']['type'] == 'program_output' for r in snapshots)
        return 'done', {'continued': True}

    source = ("await tools.parallel([{name:'first',arguments:{}},{name:'sibling',arguments:{}}]);"
              if batch else "await tools.invoke('first',{});")
    result = invoke(source + "return await tools.invoke('next',{});",
        [schema(n) for n in ['first', 'sibling', 'next']],
        {'first': lambda args: ('done', {'value': 1}),
         'sibling': lambda args: ('done', {'value': 2}), 'next': next_read})
    assert result['result'] == {'continued': True}
    progress = [r for r in snapshots if r['event']['type'] == 'program_progress']
    assert len(progress) == 2
    assert [len(r['event']['childCallIds']) for r in progress] == ([2, 3] if batch else [1, 2])
    for row in progress:
        event, parent = row['event'], row['parent']
        assert event['childCallIds'] == parent['childCallIds']
        assert event['childToolNames'] == parent['childToolNames']
        assert 'code' not in event and 'result' not in event and 'stats' not in event
    terminal = snapshots[-1]
    assert terminal['event']['type'] == 'program_output'
    assert terminal['parent']['programStatus'] == 'completed'
    assert terminal['parent']['childCallIds'] == progress[-1]['parent']['childCallIds']
    # Duplicate state projection cannot emit progress or another terminal frame.
    count = len(snapshots)
    run = task['programRuns'][0]
    projection.project_program_run(task, run, terminal=False, progress=True)
    projection.project_program_run(task, run, terminal=True)
    assert len(snapshots) == count


def test_progress_is_bounded_and_does_not_repeat_large_source(local_gateway, monkeypatch, tmp_path):
    task, _, invoke = local_gateway
    snapshots = capture(monkeypatch, task)
    source = '/*' + 'x' * 30_000 + '*/for(let i=0;i<16;i++) await tools.invoke("read",{}); return null;'
    result = invoke(source, [schema('read')], {'read': lambda args: ('done', {'ok': True})})
    assert result['stats']['tool_calls'] == 16
    progress = [r['event'] for r in snapshots if r['event']['type'] == 'program_progress']
    assert len(progress) == 16
    assert [len(e['childCallIds']) for e in progress] == list(range(1, 17))
    starts = [r['event'] for r in snapshots if r['event']['type'] == 'program_start']
    assert len(starts) == 1 and starts[0]['code'] == source
    assert all('code' not in e and 'result' not in e for e in progress)
    assert len(json.dumps(progress).encode()) < len(source.encode())


def test_rejected_batch_cannot_publish_children_that_never_executed(local_gateway, monkeypatch):
    task, dispatched, invoke = local_gateway
    snapshots = capture(monkeypatch, task)
    result = invoke("return await Promise.all([tools.invoke('read',{}),tools.invoke('missing',{})]);",
        [schema('read')], {'read': lambda args: pytest.fail('Rejected batch executed')})
    assert result['stats']['outcome'] == 'needs_model' and not dispatched
    assert not any(r['event']['type'] == 'program_progress' for r in snapshots)


def test_program_failure_keeps_settled_child_progress(local_gateway, monkeypatch):
    task, _, invoke = local_gateway
    snapshots = capture(monkeypatch, task)
    result = invoke("await tools.invoke('read',{});throw new Error('stop');",
        [schema('read')], {'read': lambda args: ('done', {'ok': True})}, expected_status='error')
    assert result['status'] == 'error'
    assert [r['event']['type'] for r in snapshots] == ['program_start', 'program_progress', 'program_output']
    assert snapshots[-1]['parent']['programStatus'] == 'error'
    assert snapshots[-1]['parent']['childCallIds'] == snapshots[1]['parent']['childCallIds']


def test_progress_publication_failure_preserves_success_and_stops_later_effects(local_gateway, monkeypatch):
    task, dispatched, invoke = local_gateway
    append = projection.append_event

    def reject_progress(carrier, event):
        if event['type'] == 'program_progress':
            raise RuntimeError('injected publication failure')
        append(carrier, event)

    monkeypatch.setattr(projection, 'append_event', reject_progress)
    result = invoke("await tools.invoke('read',{});await tools.invoke('later',{});",
        [schema('read'), schema('later')],
        {'read': lambda args: ('done', {'ok': True}),
         'later': lambda args: pytest.fail('Effect ran after publication failure')},
        expected_status='error')
    assert dispatched == [('read', {})]
    assert result['error']['code'] == 'progress_publication_failed'
    assert result['stats']['tool_calls'] == result['stats']['successful_tool_calls'] == 1
    assert task['programRuns'][0]['childCalls'][0]['status'] == 'done'
    assert task['programRuns'][0]['status'] == 'error'
