"""Aggregate fee coverage uses original quotes, never cumulative receipt sums."""
import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest

from lib.billing.native_actor_fees import from_checkpoint
from lib.billing.native_aggregate_fees import project
from tests.tasks_pkg.test_native_actor_fee_checkpoint import generation

pytestmark = pytest.mark.unit


def family(count=3):
    async def make():
        result = []
        for number in range(count):
            result.append(await generation(number, result[-1] if result else None))
        return result
    return asyncio.run(make())


def proof(current):
    return from_checkpoint(current['state'], owner_user_id=7, root_task_id=current['parent']['id'],
        actor_task_id=current['actor']['id'], tenant_id='')


def invoice(current, fees):
    return project(current['cut'], fees, owner_user_id=7, root_task_id=current['parent']['id'],
        wallet_user_id='original-wallet', reserved_micro=100000, model='alpha')


def test_cost_only_proof_keeps_original_nonzero_cost_without_wallet_authority():
    current = family(1)[0]; fees = [proof(current)]
    cost = project(current['cut'], fees, owner_user_id=7, root_task_id=current['parent']['id'],
                   wallet_user_id=None, reserved_micro=0, model='alpha')
    paid = invoice(current, fees)
    assert cost['format'] == 'tofu.native-aggregate-cost/v1'
    assert cost['componentsMicro'] == paid['componentsMicro']
    assert cost['componentsMicro']['totalMicro'] == 7593 and cost['roundCount'] == 1
    assert 'wallet_user_id' not in cost and 'reserved_micro' not in cost
    from lib.billing.native_settlement import decode
    with pytest.raises(ValueError):
        decode(cost, task_id=cost['task_id'], owner_user_id=7, wallet_user_id='original-wallet')


def test_overlapping_three_generation_prefixes_cover_once_and_preserve_each_payer():
    generations = family(); fees = [proof(item) for item in generations]
    before = deepcopy((generations[-1]['state'], fees, generations[-1]['cut']))
    with patch('lib.cost.compute_cost', side_effect=AssertionError('must not reprice')):
        for index, current in enumerate(generations):
            capsule = invoice(current, fees[:index+1])
            assert capsule['componentsMicro']['totalMicro'] == 7593
            assert capsule['roundCount'] == 1 and capsule['task_id'] == 'root-'+str(index)
        assert invoice(generations[-1], fees) == invoice(generations[-1], list(reversed(fees)))
        assert invoice(generations[-1], fees) == invoice(generations[-1], fees[-1:])
    assert (generations[-1]['state'], fees, generations[-1]['cut']) == before
    # 1 + 2 + 3 local receipt prefixes are only three original model calls.
    assert sum(row['tokens'] for row in fees) == 61200
    assert generations[-1]['cut']['snapshot']['tokens'] == 30600


@pytest.mark.parametrize('damage', ['quote', 'payer', 'missing_active', 'missing_history',
    'duplicate', 'owner', 'active_tokens', 'root_spend', 'unknown', 'overflow'])
def test_aggregate_refuses_conflicting_origins_or_incomplete_coverage(damage):
    generations = family(); current = generations[-1]; fees = [proof(item) for item in generations]
    if damage == 'quote': fees[0]['rounds'][0]['quote']['price']['input'] += 1
    elif damage == 'payer': fees[-1]['rounds'][0]['payer_task_id'] = 'another-root'
    elif damage == 'missing_active': fees.pop()
    elif damage == 'missing_history':
        fees = fees[-1:]; fees[0]['rounds'] = fees[0]['rounds'][-1:]
        fees[0]['rounds'][0]['round'] = 1; fees[0]['tokens'] = 10200
    elif damage == 'duplicate': fees.append(deepcopy(fees[-1]))
    elif damage == 'owner': fees[0]['owner_user_id'] = 8
    elif damage == 'active_tokens':
        current['cut']['actors']['actor-2']['tokens'] += 1
        current['cut']['snapshot']['tokens'] += 1
    elif damage == 'root_spend':
        current['cut']['actors']['root-2']['tokens'] = 1
        current['cut']['snapshot']['root_tokens'] = 1; current['cut']['snapshot']['tokens'] += 1
    elif damage == 'unknown': current['cut']['snapshot']['usage_known'] = False
    else:
        # Each quote fits the wallet; their aggregate for one payer does not.
        current = generations[0]; fees = [proof(current)]
        second = deepcopy(fees[0]); second.update(actor_task_id='sibling')
        second['rounds'][0]['producer_task_id'] = 'sibling'; fees.append(second)
        current['cut']['actors']['sibling'] = deepcopy(current['cut']['actors']['actor-0'])
        current['cut']['snapshot']['tokens'] *= 2
        for row in fees:
            row['rounds'][0]['quote']['credits'] = dict(input=9223372036854775807, output=0,
                cache_read=0, cache_write=0, base=9223372036854775807, margin=0, total=9223372036854775807)
    expected = {'quote': 'original quote changed', 'payer': 'conflicting payers',
                'overflow': 'storage integer range', 'root_spend': 'root-owned'}
    with pytest.raises(ValueError, match=expected.get(damage, '')): invoice(current, fees)


@pytest.mark.parametrize('damage', ['tenant', 'wallet', 'reservation', 'tree', 'owner', 'root',
    'actor', 'usage', 'unknown_usage', 'next_round', 'missing_payer', 'quote', 'public_round'])
def test_pending_checkpoint_requires_same_fee_and_usage_contract_as_execution(damage):
    current = family(1)[0]; state = current['state']; fees = state['host_fee_projection']
    if damage == 'tenant': fees['tenant_id'] = 'foreign'
    elif damage == 'wallet': fees['wallet_user_id'] = 'wallet'
    elif damage == 'reservation': fees['reserved_micro'] = 1
    elif damage == 'tree': state['shared_model_account']['root_tokens'] = 0
    elif damage == 'owner': fees['owner_user_id'] = True
    elif damage == 'root': state['shared_model_account']['root_task_id'] = 'foreign'
    elif damage == 'actor': state['task_id'] = 'foreign'
    elif damage == 'usage': state['usage'][0]['completion_tokens'] += 1
    elif damage == 'unknown_usage': state['usage'][0] = {}
    elif damage == 'next_round': state['next_round'] = True
    elif damage == 'missing_payer': fees['rounds'][0].pop('billable_task_id')
    elif damage == 'quote': fees['rounds'][0]['cost']['credits']['total'] += 1
    else: fees['rounds'][0]['public_round'] = 1
    with pytest.raises(ValueError): proof(current)


def test_known_zero_and_historical_only_debt_are_distinct_from_missing_evidence():
    from tests.billing.test_native_actor_fee_evidence import completed_actor
    from lib.billing.native_actor_fees import capture
    from lib.agent_core.account_frontier import capture_frontier
    root, actor = completed_actor(zero=True)
    cut = capture_frontier(root['_executionBudget'], version=1, reason='bound')
    capsule = project(cut, [capture(actor)], owner_user_id=7, root_task_id=root['id'],
        wallet_user_id='wallet', reserved_micro=0, model='alpha')
    assert capsule['roundCount'] == 0 and not any(capsule['componentsMicro'].values())
    current = family(1)[0]; fees = proof(current)
    # A settled successor cut retains the paid prefix, but its invoice is empty.
    cut = deepcopy(current['cut']); cut['snapshot'].update(root_task_id='successor', complete=True)
    cut['actors'] = {'successor': deepcopy(cut['actors']['root-0'])}
    cut['closed_tokens'] = cut['snapshot']['tokens']
    capsule = project(cut, [fees], owner_user_id=7, root_task_id='successor',
        wallet_user_id='wallet', reserved_micro=100000, model='alpha')
    assert capsule['roundCount'] == 0 and capsule['componentsMicro']['totalMicro'] == 0
    with pytest.raises(ValueError, match='whole-account'):
        project(cut, [], owner_user_id=7, root_task_id='successor',
            wallet_user_id='wallet', reserved_micro=100000, model='alpha')
