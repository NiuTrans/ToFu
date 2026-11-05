"""One failed workspace recovery cannot starve independent deliveries."""
from copy import deepcopy

import pytest

from lib.project_mod import delivery

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('failure_at', ['checkpoint', 'publication'])
def test_recovery_continues_other_rows_and_retries_failed_row(monkeypatch, failure_at):
    receipt = {'state': 'pending', 'taskOutcome': 'completed'}
    rows = [{
        'project_root': '/fixture', 'task_id': task, 'user_id': 7,
        'state': 'running' if failure_at == 'checkpoint' else 'ready',
        'origin': {'delivery': deepcopy(receipt)},
    } for task in ('temporarily-unavailable', 'independent')]
    available = {row['task_id']: row for row in rows}
    attempts, completed, faults = [], [], []
    monkeypatch.setattr(delivery, '_RECOVERY_CURSOR', 0)
    monkeypatch.setattr(delivery.state, 'delivery_action_page',
                        lambda *_args, **_kwargs: {'rows': rows, 'nextAfterId': 0, 'hasMore': False})
    def maybe_fail(stage, task):
        if task == rows[0]['task_id'] and stage == failure_at and not faults:
            faults.append(stage)
            raise RuntimeError('transient workspace fault')
    def read(_root, task, **_kwargs):
        return available[task]
    def settle(row, _receipt):
        task = row['task_id']
        attempts.append(task)
        maybe_fail('checkpoint' if failure_at == 'checkpoint' else 'publication', task)
        completed.append(task)
        row['origin']['delivery'] = {**receipt, 'state': 'delivered'}
        return row['origin']['delivery']
    monkeypatch.setattr(delivery.state, 'get_workspace', read)
    monkeypatch.setattr(delivery, '_settle_registered', settle)
    monkeypatch.setattr(delivery, 'deliver_registered_workspace',
                        lambda _root, task, **_kwargs: settle(available[task], receipt))
    monkeypatch.setattr(delivery, '_checkpoint_delivered_workspace', lambda _row: None)
    monkeypatch.setattr(delivery, '_reclaim_completed_source', lambda _row: None)
    assert delivery.resume_pending_workspace_deliveries()
    assert completed == ['independent']
    assert available[rows[0]['task_id']]['origin']['delivery']['state'] == 'pending'
    assert delivery.resume_pending_workspace_deliveries()
    assert completed == ['independent', 'temporarily-unavailable']
    assert delivery.resume_pending_workspace_deliveries() is False
