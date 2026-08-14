"""Optimizer repository behavior through a real storage.v1 process."""

from __future__ import annotations

import json

import pytest

from lib.storage import StorageSupervisor


pytestmark = pytest.mark.unit


@pytest.fixture
def optimizer_store(tmp_path, monkeypatch):
    import lib.optimizer.storage as storage

    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=20)
    supervisor.start()
    monkeypatch.setattr(
        storage, '_storage', lambda **_kwargs: supervisor.client)
    try:
        yield storage
    finally:
        supervisor.stop()


def test_repository_lifecycle_preserves_wire_compatibility(
        optimizer_store, monkeypatch):
    storage = optimizer_store
    ids = iter(['opt_adapter', 'act_adapter'])
    monkeypatch.setattr(storage, 'short_id', lambda *_args: next(ids))

    proposal_id = storage.create_proposal(
        title='Tune bounded queue', rationale='avoid unbounded RSS',
        action_type='set_limit', action_args={'limit': 200},
        evidence=['writer-depth'], status='pending_review')
    assert proposal_id == 'opt_adapter'
    proposal = storage.get_proposal(proposal_id)
    assert json.loads(proposal['action_args']) == {'limit': 200}
    assert json.loads(proposal['evidence']) == ['writer-depth']
    assert storage.list_proposals(status='pending_review')[0]['id'] == proposal_id

    storage.update_proposal_status(proposal_id, 'applied', 'approved')
    log_id = storage.record_applied(
        proposal_id=proposal_id, ttl_days=2,
        pre_metric={'writer_depth': 10})
    assert log_id == 'act_adapter'
    storage.record_outcome_metric(log_id, {'writer_depth': 2})
    action = storage.get_action_log_for_proposal(proposal_id)
    assert json.loads(action['pre_metric'])['writer_depth'] == 10
    assert json.loads(action['outcome_metric'])['writer_depth'] == 2
    assert storage.list_applied_actions()[0]['p_status'] == 'applied'

    storage.mark_reverted(log_id, 'test complete')
    assert storage.list_applied_actions() == []
    assert storage.list_applied_actions(include_reverted=True)[0][
        'revert_reason'] == 'test complete'


def test_cost_outlier_signal_uses_daily_cost_semantics(monkeypatch):
    from lib.optimizer.analyzer import _signals

    class Client:
        def query(self, operation, payload):
            assert operation == 'daily_cost.latest'
            assert payload == {'user_id': 1}
            return {'conversations': {
                'conv-low': {'cost': 0.5},
                'conv-high': {'cost': 8.25},
            }}

    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: Client())
    assert _signals._collect_cost_outliers() == {
        'top_cost_conversations': [
            {'conv_id': 'conv-high', 'cost_usd': 8.25},
            {'conv_id': 'conv-low', 'cost_usd': 0.5},
        ],
    }
