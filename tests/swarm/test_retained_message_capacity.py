"""Complete retained evidence never supplies a private execution admission quota."""
from copy import deepcopy
import json

import pytest

from lib.swarm import retained_repository as storage
from lib.swarm.retained import complete_assignment
from lib.swarm.run_messages import RunMessageError
from tests.swarm.test_retained_messages import context as context, send, settle, read  # noqa: F401

pytestmark = pytest.mark.unit


def test_large_retained_identity_admits_claim_without_private_evidence_quota(context):
    history = [{'role': 'assistant', 'content': 'x' * (9 * 1024 * 1024)}]
    context.repository.change(context.scope, context.agent['id'], lambda value: {**value, 'messages': history})
    receipt = send(context)
    assert receipt['state'] == 'turn_started'
    assert len(context.launches) == 1
    assert context.repository.get(context.scope, context.agent['id'])[0]['messages'] == history


def test_launch_result_omits_duplicate_objective_and_keeps_actionable_ids(context):
    def verbose(name, args, parent, **kwargs):
        del name
        handle = json.loads(context.publish(args, parent, kwargs))
        handle['agents'][0]['objective'] = 'duplicated objective' * 100000
        handle['message'] = 'ordinary presentation prose' * 100000
        return json.dumps(handle)
    context.launch_behavior = verbose
    receipt = send(context)
    assert receipt['state'] == 'turn_started'
    launch = receipt['assignment']['launch']
    assert launch['agents'][0]['id'] == receipt['assignment']['agent_id']
    assert 'objective' not in launch['agents'][0] and 'message' not in launch
    assert 'duplicated objective' not in json.dumps(receipt)


def test_large_checkpoint_preserves_full_messages_and_dispatch_keys(context):
    receipt = send(context)
    value = context.repository.get(context.scope, context.agent['id'])[0]
    preserved = deepcopy(value['message_dispatches'])
    history = [{'role': 'assistant', 'content': 'x' * (9 * 1024 * 1024)},
               {'role': 'assistant', 'content': 'Newest exact evidence'}]
    assert complete_assignment(user_id=7, tenant_id='', scope=context.scope,
        agent_id=context.agent['id'], run_id=receipt['assignment']['agent_id'],
        messages=history, result={'status': 'completed', 'final_answer': 'Newest exact evidence'})
    value = context.repository.get(context.scope, context.agent['id'])[0]
    assert value['state'] == 'idle'
    assert value['messages'] == history
    assert value['message_dispatches'] == preserved
    assert send(context) == receipt
    assert len(context.launches) == 1
