"""The native aggregate retirement fixture must follow Python's account/fee owners."""
import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

from lib.billing.cost import transfer_native_child_fees
from lib.billing.native_actor_fees import capture, from_checkpoint
from lib.billing.native_aggregate_fees import project
from lib.goal_runs.contract import goal_run_policy
from tests.billing.test_recovered_actor_fee_transfer import child, root
from tests.tasks_pkg.test_native_actor_fee_checkpoint import checkpoint, policy_for, spend

pytestmark = pytest.mark.unit


def test_native_goal_claim_fixture_matches_shared_account_and_frozen_fee_owners():
    async def produce():
        parent, account = root('source-root')
        cuts, states = [], {}
        account.bind_account_frontier_observer(lambda cut: cuts.append(deepcopy(cut)))
        for name in ('pending-a', 'pending-b', 'finished-c'):
            task = child(parent, name)
            policy = policy_for(task)
            await spend(task, policy)
            state = checkpoint(task, policy)
            state.update(format='tofu.native-agent-checkpoint/v1', epoch='epoch', binding='binding', tool_calls=1,
                messages=[{'role': 'user', 'content': 'Preserve work'}, {'role': 'assistant', 'tool_calls': [
                    {'id': 'write-1', 'type': 'function', 'function': {'name': 'write_file', 'arguments': '{}'}}]}],
                round_limit=3, tool_limit=3, no_progress_limit=0, failure_limit=0, tools_pending=True,
                progress=dict(codec='sha256-token-sequence/v1', last_calls=None, last_world='', last_evidence=None,
                    last_failure=None, no_progress_streak=0, failure_streak=0))
            state['host_fee_projection']['tenant_id'] = '3'
            if name == 'finished-c':
                task['status'] = 'done'
                transfer_native_child_fees(parent, task)
                account.close(task)
                completed = capture(task)
            else:
                states[name] = state
        fees = [from_checkpoint(state, owner_user_id=7, root_task_id='source-root', actor_task_id=key,
            tenant_id='3') for key, state in states.items()] + [completed]
        invoice = project(cuts[-1], fees, owner_user_id=7, root_task_id='source-root', wallet_user_id='wallet-7',
            reserved_micro=100000, model='alpha')
        return dict(states=states, completed_fees=completed, cut=cuts[-1], invoice=invoice, goal_policy=goal_run_policy())

    fixture = Path(__file__).resolve().parents[2] / 'packages/tofu-db/tests/fixtures/native_goal_actor_claim.json'
    assert asyncio.run(produce()) == json.loads(fixture.read_text())
