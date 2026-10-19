"""A cost-only launch is explicit authority, never a missing-wallet fallback."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from lib.goal_runs.fee_account import from_task, original_reservation
from lib.orchestration.execution_journal import RecoveryRefused

pytestmark = pytest.mark.unit


def test_explicit_personal_launch_requires_no_financial_lookup():
    storage = SimpleNamespace(query=Mock(side_effect=AssertionError('cost has no wallet query')))
    assert from_task({}) == {'mode': 'cost'}
    assert original_reservation({}, {'task_id': 'root', 'fee_account': {'mode': 'cost'}},
                                storage=storage, deadline=1) == (None, 0)


@pytest.mark.parametrize('task', [
    {'_billingUserId': 7}, {'_billing_reservation_micro': 1},
    {'_billing_reservation_micro': True}, {'_billing_reservation_micro': -1},
    {'_billingUserId': 'w', '_billing_reservation_micro': 1 << 63},
])
def test_inconsistent_launch_cannot_publish_fee_authority(task):
    with pytest.raises(RecoveryRefused): from_task(task)


def test_zero_estimate_keeps_wallet_identity_but_cannot_claim_pending_paid_work():
    task = {'_billingUserId': 'w'}
    assert from_task(task) == {'mode': 'wallet', 'wallet_user_id': 'w', 'reserved_micro': 0}
    storage = SimpleNamespace(query=Mock(side_effect=AssertionError('refuse before query')))
    with pytest.raises(RecoveryRefused, match='fresh positive'):
        original_reservation(task, {'task_id': 'root'}, storage=storage, deadline=1)


@pytest.mark.parametrize('source', [None, {'mode': 'wallet', 'wallet_user_id': 'w', 'reserved_micro': 9}])
def test_legacy_and_paid_launches_cannot_downgrade_to_cost(source):
    storage = SimpleNamespace(query=Mock(side_effect=AssertionError('refuse before query')))
    with pytest.raises(RecoveryRefused, match='original owned billing wallet'):
        original_reservation({}, {'task_id': 'root', 'fee_account': source}, storage=storage, deadline=1)


@pytest.mark.parametrize('source', [None, {'mode': 'wallet', 'wallet_user_id': 'w', 'reserved_micro': 9}])
def test_paid_source_keeps_exact_original_hold_separate_from_target(source):
    storage = SimpleNamespace(query=Mock(return_value={'user_id': 'w', 'ref_id': 'root', 'amount_micro': -9}))
    task = {'_billingUserId': 'w', '_billing_reservation_micro': 5}
    metadata = {'task_id': 'root'}
    if source is not None: metadata['fee_account'] = source
    assert original_reservation(task, metadata, storage=storage, deadline=2) == ('w', 9)
    if source:
        for changed in ({'mode': 'cost'}, dict(source, wallet_user_id='foreign'), dict(source, reserved_micro=8)):
            with pytest.raises(RecoveryRefused):
                original_reservation(task, dict(metadata, fee_account=changed), storage=storage, deadline=2)
