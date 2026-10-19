"""Original and successor root invoices settle independently, including a lost ACK."""
import uuid

import pytest

from tests.storage.test_storage_application_contract import application_storage as application_storage
from tests.billing.test_recovered_actor_fee_transfer import recovered_family

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 2


def test_original_actor_prefix_never_recharges_successor_wallet(application_storage, monkeypatch):
    from lib.billing import users, deposit, reserve, get_balance, settle
    from lib.billing.native_admission import protect_before_dispatch
    from lib.billing.native_settlement import freeze, recover
    from lib.billing.cost import transfer_native_child_fees
    from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT

    user = users.create_user(uuid.uuid4().hex + '@actor-fee.example')
    assert user.owner_user_id == 2
    deposit(user.id, 100000, ref_id='seed')
    source, target, actor, account = recovered_family(owner=2)
    transfer_native_child_fees(target, actor)
    account.close(actor)
    for task in (source, target):
        task.update(_billingUserId=user.id, _billing_reservation_micro=10000)
        reserve(user.id, 10000, ref_id=task['id'])
        protect_before_dispatch(task, budget=task['_executionBudget'], owner=2)
        capsule = freeze(task)
        assert capsule['roundCount'] == 1 and capsule['componentsMicro']['totalMicro'] == 7593
        application_storage.command('task_results.checkpoint', dict(
            key=task['id'], expected_version=0, guard_contract=TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
            require_parent=False, value=dict(task_id=task['id'], user_id=2, conv_id='', status='done',
                created_at=1, metadata=dict(nativePricingRequired=True, nativeBillingSettlement=capsule))),
            'actor-invoice-' + task['id'])
    assert get_balance(user.id) == 80000
    assert recover(wallet_user_id=user.id, task_id=source['id'], held_micro=10000)
    def lose(*args, **kwargs):
        settle(*args, **kwargs)
        raise OSError('successor settlement ACK lost')
    with monkeypatch.context() as fault:
        fault.setattr('lib.billing.settle', lose)
        with pytest.raises(OSError):
            recover(wallet_user_id=user.id, task_id=target['id'], held_micro=10000)
    for task in (source, target):
        assert recover(wallet_user_id=user.id, task_id=task['id'], held_micro=10000)
    assert get_balance(user.id) == 100000 - 2 * 7593
    assert account.consumption_snapshot() == (20400, 0, True)
