"""Selected wallet authorities serialize native provider admission and refunds."""
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

import pytest

from tests.storage.test_storage_application_contract import application_storage as application_storage
from lib.agent_core.execution_budget import _ensure_execution_budget, inherit_execution_budget
from lib.billing.native_admission import protect_before_dispatch, is_protected
from lib.storage_authority_sql.generated_billing_contract import NATIVE_ADMISSION_KIND

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 2


def setup(storage):
    from lib.billing import users, deposit, reserve
    user = users.create_user(uuid.uuid4().hex + '@native-hold.example')
    assert user.owner_user_id == 2
    deposit(user.id, 10000, ref_id='seed')
    root = dict(id='root', _userId=2, status='running', config={},
        _billingUserId=user.id, _billing_reservation_micro=100, _nativePricingRequired=True)
    reserve(user.id, 100, ref_id=root['id'])
    budget = _ensure_execution_budget(root)
    child = dict(id='child', _userId=2, _parentTaskId=root['id'], status='running', config={})
    inherit_execution_budget(root, child)
    return user, root, child, budget


def entry(storage, user, task='root'):
    return storage.query('billing.ledger.find', dict(user_id=user.id,
        kind=NATIVE_ADMISSION_KIND, ref_type='reserve', ref_id=task))


def test_admitted_hold_survives_missing_task_metadata_and_only_exact_settlement_releases(application_storage):
    from lib.billing import get_balance, reserve_release, settle
    from lib.billing.native_settlement import recover
    from lib.storage.errors import StorageError
    user, root, child, budget = setup(application_storage)
    assert recover(wallet_user_id=user.id, task_id='root', held_micro=100) is False
    protect_before_dispatch(child, budget=budget, owner=2)
    first = entry(application_storage, user)
    assert first['amount_micro'] == 0 and get_balance(user.id) == 9900
    protect_before_dispatch(root, budget=budget, owner=2)
    assert entry(application_storage, user) == first
    with pytest.raises(ValueError, match='no committed terminal'):
        recover(wallet_user_id=user.id, task_id='root', held_micro=100)
    with pytest.raises(StorageError): reserve_release(user.id, 100, ref_id='root')
    assert get_balance(user.id) == 9900
    settle(user.id, reserved_micro=100, actual_micro=37, ref_id='root')
    assert get_balance(user.id) == 9963
    root.pop('_nativeBillingAdmission')
    with pytest.raises(StorageError): protect_before_dispatch(child, budget=budget, owner=2)
    assert get_balance(user.id) == 9963


def test_lost_admission_ack_stays_protected_and_retry_never_adds_another_entry(application_storage, monkeypatch):
    from lib.billing import get_balance, reserve_release
    from lib.storage.errors import StorageError
    user, root, child, budget = setup(application_storage)
    command = application_storage.command
    lost = []
    def fail(operation, payload, *args, **kwargs):
        result = command(operation, payload, *args, **kwargs)
        if operation == 'billing.wallet.apply' and payload.get('kind') == NATIVE_ADMISSION_KIND and not lost:
            lost.append(True)
            raise OSError('native admission committed, ACK lost')
        return result
    monkeypatch.setattr(application_storage, 'command', fail)
    with pytest.raises(OSError, match='ACK lost'): protect_before_dispatch(child, budget=budget, owner=2)
    assert '_nativeBillingAdmission' not in root
    first = entry(application_storage, user)
    with pytest.raises(StorageError): reserve_release(user.id, 100, ref_id='root')
    protect_before_dispatch(child, budget=budget, owner=2)
    assert entry(application_storage, user) == first and get_balance(user.id) == 9900


def test_racing_refund_and_admission_have_one_winner(application_storage):
    from lib.billing import get_balance, reserve_release
    from lib.storage.errors import StorageError
    user, root, child, budget = setup(application_storage)
    barrier = threading.Barrier(2)
    def invoke(admit):
        barrier.wait(timeout=10)
        try:
            if admit: protect_before_dispatch(child, budget=budget, owner=2)
            else: reserve_release(user.id, 100, ref_id='root')
            return True
        except StorageError: return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(invoke, True); b = pool.submit(invoke, False)
        admitted, refunded = a.result(timeout=20), b.result(timeout=20)
    assert admitted != refunded
    assert is_protected(application_storage, wallet_user_id=user.id, task_id='root') == admitted
    assert get_balance(user.id) == (9900 if admitted else 10000)


def test_generic_ledger_append_cannot_bypass_the_wallet_barrier(application_storage):
    from lib.billing import get_balance
    from lib.storage.errors import StorageError
    user, root, child, budget = setup(application_storage)
    value = dict(id='forged', user_id=user.id, ts=1, amount_micro=0,
        kind=NATIVE_ADMISSION_KIND, ref_type='reserve', ref_id='root', balance_after_micro=9900, note='')
    with pytest.raises(StorageError): application_storage.command('billing.ledger.append', value, 'forged')
    protect_before_dispatch(child, budget=budget, owner=2)
    value.update(id='release', kind='reserve_release', amount_micro=100, balance_after_micro=10000)
    with pytest.raises(StorageError): application_storage.command('billing.ledger.append', value, 'forged-release')
    assert get_balance(user.id) == 9900


@pytest.mark.parametrize('field,value', [('_billingUserId', ''), ('_billing_reservation_micro', 0),
                                         ('_billing_reservation_micro', 101)])
def test_acknowledged_payer_cannot_be_changed_or_disabled(application_storage, field, value):
    user, root, child, budget = setup(application_storage)
    protect_before_dispatch(child, budget=budget, owner=2)
    root[field] = value
    with pytest.raises(ValueError, match='identity changed'):
        protect_before_dispatch(child, budget=budget, owner=2)
    assert is_protected(application_storage, wallet_user_id=user.id, task_id='root')


@pytest.mark.parametrize('changes', [{'amount_micro': 1}, {'ref_type': 'task'}, {'ref_id': 'absent'}])
def test_wallet_rejects_malformed_or_unreserved_admission(application_storage, changes):
    from lib.storage.errors import StorageError
    user, _, _, _ = setup(application_storage)
    payload = dict(user_id=user.id, amount_micro=0, kind=NATIVE_ADMISSION_KIND,
        ref_type='reserve', ref_id='root', ledger_id='bad', note='', allow_negative=True)
    payload.update(changes)
    with pytest.raises(StorageError): application_storage.command('billing.wallet.apply', payload, 'bad-admission')
    assert entry(application_storage, user) is None


@pytest.mark.parametrize('damage', ['foreign', 'amount', 'root_cancelled', 'child_cancelled', 'detached'])
def test_wrong_or_dead_execution_cannot_admit_a_hold(application_storage, damage):
    from lib.agent_core.execution_budget import close_execution_budget, ModelBudgetExceeded
    user, root, child, budget = setup(application_storage)
    if damage == 'foreign': child['_userId'] = 3
    elif damage == 'amount': root['_billing_reservation_micro'] = 101
    elif damage == 'root_cancelled': root['aborted'] = True
    elif damage == 'child_cancelled': child['aborted'] = True
    elif damage == 'detached': close_execution_budget(child)
    with pytest.raises((ValueError, ModelBudgetExceeded)):
        protect_before_dispatch(child, budget=budget, owner=2)
    assert entry(application_storage, user) is None
