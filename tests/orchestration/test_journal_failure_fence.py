"""A failed journal boundary revokes sibling admission without hiding cancellation."""
import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from lib.orchestration.execution_journal import ExecutionJournal, RecoveryRefused, fingerprint

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('boundary', ['operation', 'publication', 'receipt', 'replay'])
@pytest.mark.parametrize('error_type', [RuntimeError, asyncio.CancelledError])
def test_unsettled_boundary_revokes_every_scoped_sibling(boundary, error_type):
    error = error_type('injected failure after admission')
    events, calls = [], []
    def emit(event):
        if boundary == 'publication' and event['type'] == 'execution_effect_completed':
            raise error
        events.append(deepcopy(event))
    class Uncopyable:
        def __deepcopy__(self, memo):
            raise error
    records = ({'["root","agent","a",0,1]': {
        'kind': 'agent', 'input_hash': fingerprint('input'), 'result': 'prior result'}}
        if boundary == 'replay' else None)
    journal = ExecutionJournal(emit, records=records)
    sibling = journal.child('sibling')
    def operation():
        calls.append('effect')
        if boundary == 'operation':
            raise error
        return Uncopyable() if boundary == 'receipt' else 'completed'
    def on_replay():
        raise error
    with pytest.raises(BaseException) as caught:
        journal.call('agent', 'a', 0, 'input', operation,
                     on_replay=on_replay if boundary == 'replay' else None)
    assert caught.value is error or caught.value.__cause__ is error
    assert journal.abort_requested()
    with pytest.raises(RecoveryRefused):
        sibling.call('agent', 'b', 0, 'input', lambda: calls.append('forbidden'))
    assert calls == ([] if boundary == 'replay' else ['effect'])
    assert not any(row['type'] == 'execution_effect_completed' for row in events)


def test_cancelled_sibling_does_not_drop_already_admitted_completion():
    events, entered, release = [], Event(), Event()
    journal = ExecutionJournal(lambda event: events.append(deepcopy(event)))
    first, second = journal.child('first'), journal.child('second')
    def accepted_operation():
        entered.set()
        assert release.wait(5)
        return 'durable side effect'
    def cancel():
        raise asyncio.CancelledError('owner cancelled this branch')
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(first.call, 'agent', 'a', 0, {}, accepted_operation)
        try:
            assert entered.wait(5)
            with pytest.raises(asyncio.CancelledError):
                second.call('agent', 'b', 0, {}, cancel)
            assert journal.abort_requested()
        finally:
            release.set()
        assert running.result(timeout=5) == 'durable side effect'
    completed = [row for row in events if row['type'] == 'execution_effect_completed']
    assert len(completed) == 1
    assert completed[0]['result'] == 'durable side effect'


@pytest.mark.parametrize('error_type', [RuntimeError, asyncio.CancelledError])
def test_failed_context_restore_fences_replay_siblings(error_type):
    events = []
    journal = ExecutionJournal(lambda event: events.append(deepcopy(event)),
                               worker_context_policy='conversation')
    context = {'messages': [{'role': 'user', 'content': 'retained context'}], 'task_state': {}}
    runner = journal.runner(lambda *args: {'output': 'original work'},
                            context_snapshot=lambda: context, context_restore=lambda _: None)
    assert runner({'id': 'a'}, 'input', 0).output == 'original work'
    completed = next(row for row in events if row['type'] == 'execution_effect_completed')
    records = {completed['key']: {key: value for key, value in completed.items()
                                 if key not in ('type', 'key')}}
    restored = ExecutionJournal(lambda _: pytest.fail('replay dispatched'), records=records,
                                worker_context_policy='conversation')
    error = error_type('restore failed')
    def fail_restore(_):
        raise error
    replay = restored.runner(lambda *args: pytest.fail('effect repeated'),
                              context_snapshot=lambda: context, context_restore=fail_restore)
    with pytest.raises(error_type) as caught:
        replay({'id': 'a'}, 'input', 0)
    assert caught.value is error
    assert restored.abort_requested()
