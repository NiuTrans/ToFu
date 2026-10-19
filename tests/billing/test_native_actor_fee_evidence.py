"""Private actor fees preserve exact native quotes without granting execution."""
from copy import deepcopy

import pytest

from lib.billing.cost import transfer_native_child_fees
from lib.billing.native_actor_fees import capture, decode
from tests.billing.test_recovered_actor_fee_transfer import root, child, spend

pytestmark = pytest.mark.unit


def paid(actor):
    spend(actor)
    actor['apiRounds'][-1].update(model='alpha', provider_id='provider')


def completed_actor(*, recovered=False, zero=False):
    parent, account = root('original-root')
    cuts = []
    account.bind_account_frontier_observer(lambda cut: cuts.append(deepcopy(cut)))
    actor = child(parent, 'original-actor')
    if not zero: paid(actor)
    source = deepcopy(cuts[-1])
    actor['status'] = 'done'
    transfer_native_child_fees(parent, actor)
    account.close(actor)
    if not recovered:
        return parent, actor
    old = actor
    parent, account = root('successor-root')
    account.restore_pending_frontier(parent, source, owner=7)
    actor = child(parent, 'successor-actor')
    account.rebind_actor_prefix(actor, source_task_id=old['id'], owner=7)
    actor['apiRounds'] = deepcopy(old['apiRounds'])
    for row in actor['apiRounds']: row['billableTaskId'] = 'original-root'
    actor['_checkpointApiRounds'] = deepcopy(actor['apiRounds'])
    paid(actor)
    actor['status'] = 'done'
    transfer_native_child_fees(parent, actor)
    account.close(actor)
    return parent, actor


@pytest.mark.parametrize('recovered', [False, True])
def test_detached_completion_keeps_original_prices_credits_and_payers(recovered):
    parent, actor = completed_actor(recovered=recovered)
    proof = capture(actor)
    assert proof['tokens'] == (20400 if recovered else 10200)
    assert proof['root_task_id'] == parent['id'] and proof['actor_task_id'] == actor['id']
    for source, saved in zip(actor['apiRounds'], proof['rounds']):
        assert saved['quote']['price'] == source['nativePrice']
        assert saved['quote']['credits'] == source['nativeCredits']
        assert saved['producer_task_id'] == source['taskId']
        assert saved['payer_task_id'] == source.get('billableTaskId', parent['id'])
    proof['rounds'][0]['quote']['credits']['total'] += 1
    assert capture(actor)['rounds'][0]['quote']['credits']['total'] == 7593


def test_known_zero_is_distinct_from_absent_or_unacknowledged_fee_evidence():
    parent, actor = completed_actor(zero=True)
    proof = capture(actor)
    assert proof['tokens'] == 0 and proof['rounds'] == []
    assert capture(dict(id='legacy')) is None
    actor.pop('_nativeChildFeeGrant')
    with pytest.raises(ValueError, match='acknowledged'): capture(actor)


@pytest.mark.parametrize('damage', ['owner', 'root', 'actor', 'tokens', 'payer', 'producer',
                                   'extra', 'price', 'amount', 'credits', 'nan', 'boolean', 'order'])
def test_changed_private_fee_evidence_is_not_a_valid_financial_receipt(damage):
    parent, actor = completed_actor(recovered=True)
    proof = capture(actor)
    if damage == 'owner': proof['owner_user_id'] += 1
    elif damage == 'root': proof['root_task_id'] = 'foreign'
    elif damage == 'actor': proof['actor_task_id'] = 'foreign'
    elif damage == 'tokens': proof['tokens'] -= 1
    elif damage == 'payer': proof['rounds'][0]['payer_task_id'] = parent['id']
    elif damage == 'producer': proof['rounds'][0]['producer_task_id'] = parent['id']
    elif damage == 'extra': proof['wallet_settlement'] = {}
    elif damage == 'price': proof['rounds'][0]['quote']['price']['invented'] = True
    elif damage == 'amount': proof['rounds'][0]['quote']['amounts']['outputTokens'] += 1
    elif damage == 'credits': proof['rounds'][0]['quote']['credits']['total'] += 1
    elif damage == 'nan': proof['rounds'][0]['quote']['amounts']['costUsd'] = float('nan')
    elif damage == 'boolean': proof['tokens'] = True
    else: proof['rounds'].reverse()
    with pytest.raises(ValueError):
        decode(proof, owner_user_id=7, root_task_id=parent['id'], actor_task_id=actor['id'])


@pytest.mark.parametrize('damage', ['price', 'payer', 'owner', 'parent', 'flag'])
def test_mutated_task_cannot_change_the_frozen_completion_fees(damage):
    _, actor = completed_actor(recovered=True)
    if damage == 'price': actor['apiRounds'][0]['nativePrice']['input'] += 1
    elif damage == 'payer': actor['_checkpointApiRounds'][0]['billableTaskId'] = 'foreign'
    elif damage == 'owner': actor['_userId'] = 8
    elif damage == 'parent': actor['_parentTaskId'] = 'foreign'
    else: actor['_nativePricingRequired'] = False
    with pytest.raises(ValueError): capture(actor)
