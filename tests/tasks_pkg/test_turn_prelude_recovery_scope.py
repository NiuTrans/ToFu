"""Durable inbox recovery belongs to a durable root, never an embed carrier."""
import uuid

import pytest

from lib.identity import PrincipalContext
from lib.tasks_pkg.orchestrator._turn_prelude import run_turn_prelude

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('flags', [
    {'_transientRuntime': True},
    {'_invocationStorageFree': True},
    {'_transientRuntime': True, '_invocationStorageFree': True},
    {},
])
def test_prelude_only_recovers_receipts_for_a_durable_root(monkeypatch, flags):
    from lib import agent_inbox, message_queue
    from lib.swarm import integration

    principal = PrincipalContext.user(subject_id='prelude-test', owner_user_id=7)
    task = {'id': uuid.uuid4().hex, 'convId': uuid.uuid4().hex,
            '_userId': 7, '_principalContext': principal.to_payload(), **flags}
    reads, delivered = [], []
    receipt = {'value': 'committed completion', 'receipt_id': 'durable-receipt'}

    def recover(conv_id, *, user_id):
        reads.append((conv_id, user_id))
        return [receipt]

    monkeypatch.setattr(message_queue, 'pending_background_command_injections', recover)
    monkeypatch.setattr(agent_inbox, 'is_tombstoned', lambda key: False)
    monkeypatch.setattr(integration, 'reset_autocontinue_chain', lambda key: None)
    monkeypatch.setattr(agent_inbox, 'enqueue',
                        lambda *args, **kwargs: delivered.append((args, kwargs)))
    cfg = {'model': 'test-model'}
    assert run_turn_prelude(task, cfg, task['id']) == cfg
    if flags:
        assert reads == [] and delivered == []
    else:
        assert reads == [(task['convId'], 7)]
        assert delivered == [((task['convId'], receipt['value']),
            {'priority': 'next', 'mode': 'background-command',
             'extra': {'receipt_id': 'durable-receipt'}})]
