"""Real wallets retain unknown request debt and replay frozen invoices exactly."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import uuid

import pytest

from lib.billing.request_settlement import RequestSettlement, IncompleteRequestUsage, recover
from lib.storage_authority_sql.generated_billing_contract import REQUEST_ADMISSION_KIND, REQUEST_INVOICE_KIND
from tests.storage import test_storage_application_contract as storage_contract

pytestmark = pytest.mark.api
application_storage = storage_contract.application_storage


@pytest.fixture
def application_storage_owner_id():
    return 2


def setup(storage):
    from lib.billing import users, deposit
    user = users.create_user(uuid.uuid4().hex + '@request.example.test')
    deposit(user.id, 10000, ref_id='seed')
    storage.command('billing.wallet.apply', dict(user_id=user.id, amount_micro=-100,
        kind='reserve', ref_type='reserve', ref_id='request', note='old original hold',
        allow_negative=False, ledger_id='original-hold', occurred_at=int(time.time()) - 3600), 'hold')
    return user, RequestSettlement(request_id='request', owner=user.owner_user_id,
                                  wallet=user.id, held=100)


def attempt(owner, usage, *, dispatched=True):
    from lib.llm.request import seal_request
    lease = owner(input_tokens=10, max_output_tokens=20)
    lease.prepare_input(seal_request({'model': 'gpt-4o-mini', 'messages': []},
        model='gpt-4o-mini', provider_id='', input_tokens=10, semantic_id='synthetic'))
    if dispatched:
        lease.mark_dispatched()
    lease.settle(usage, unsent=not dispatched)
    lease.confirm_input(successful=dispatched)
    return lease


@pytest.mark.parametrize('usage', [None, {}, {'prompt_tokens': 10},
    {'prompt_tokens': 10, 'completion_tokens': True},
    {'prompt_tokens': 10, 'completion_tokens': 2, 'cached_tokens': -1}])
def test_unknown_or_partial_usage_is_never_a_zero_invoice(application_storage, usage):
    from lib.billing import get_balance, reserve_release
    from lib.billing.wallet_janitor import sweep_stale_reserves
    from lib.storage.errors import StorageError
    user, owner = setup(application_storage)
    owner.protect()
    attempt(owner, usage)
    attempt(owner, {'prompt_tokens': 10, 'completion_tokens': 2})
    with pytest.raises(IncompleteRequestUsage):
        owner.settle()
    del owner
    with pytest.raises(IncompleteRequestUsage):
        recover(wallet_user_id=user.id, task_id='request', held_micro=100)
    with pytest.raises(StorageError):
        reserve_release(user.id, 100, ref_id='request')
    summary = sweep_stale_reserves(ttl_seconds=60)
    assert summary['errors'] == 1 and summary['reclaimed'] == 0
    assert get_balance(user.id) == 9900


@pytest.mark.parametrize('lost', ['invoice', 'wallet', 'none'])
def test_cold_recovery_sums_attempts_and_never_reprices_or_double_charges(
        application_storage, monkeypatch, lost):
    from lib.billing import get_balance, compute_request_cost
    from lib.billing.wallet_janitor import sweep_stale_reserves
    from lib.billing import wallet
    user, owner = setup(application_storage)
    owner.protect()
    usages = [{'prompt_tokens': 1000, 'completion_tokens': 500},
              {'prompt_tokens': 2000, 'completion_tokens': 200,
               'prompt_tokens_details': {'cached_tokens': 1500}}]
    expected = sum(compute_request_cost('gpt-4o-mini', raw_usage=u).micro for u in usages)
    assert expected > 0
    for usage in usages:
        attempt(owner, usage)
    command = application_storage.command
    def invoice_ack_lost(operation, payload, *args, **kwargs):
        result = command(operation, payload, *args, **kwargs)
        if operation == 'billing.wallet.apply' and payload.get('kind') == REQUEST_INVOICE_KIND:
            raise OSError('invoice committed ACK lost')
        return result
    def wallet_ack_lost(*args, **kwargs):
        wallet.settle(*args, **kwargs)
        raise OSError('wallet committed ACK lost')
    with monkeypatch.context() as fault:
        if lost == 'invoice':
            fault.setattr(application_storage, 'command', invoice_ack_lost)
        elif lost == 'wallet':
            fault.setattr('lib.billing.settle', wallet_ack_lost)
        if lost == 'none':
            result = owner.settle()
            assert result['cost_micro'] == expected and result['attempt_count'] == 2
        else:
            with pytest.raises(OSError, match='ACK lost'):
                owner.settle()
    del owner
    monkeypatch.setattr('lib.billing.compute_request_cost', lambda *a, **k: pytest.fail('cold recovery repriced'))
    summary = sweep_stale_reserves(ttl_seconds=60)
    assert summary['errors'] == 0
    assert summary['settled_requests'] == (1 if lost == 'invoice' else 0)
    assert get_balance(user.id) == 10000 - expected
    assert recover(wallet_user_id=user.id, task_id='request', held_micro=100)
    assert get_balance(user.id) == 10000 - expected


def test_explicit_zero_is_complete_and_private_diagnostics_are_not_stored(application_storage):
    from lib.billing import get_balance
    user, owner = setup(application_storage)
    owner.protect()
    usage = {'prompt_tokens': 0, 'completion_tokens': 0, '_dispatch': {'secret': 'never-store'}}
    attempt(owner, usage)
    usage['completion_tokens'] = 99999
    result = owner.settle()
    assert result['cost_micro'] == 0 and get_balance(user.id) == 10000
    entry = application_storage.query('billing.ledger.find', dict(user_id=user.id,
        kind=REQUEST_INVOICE_KIND, ref_type='reserve', ref_id='request'))
    assert 'never-store' not in entry['note']
    assert json.loads(entry['note'])['quotes'][0]['usage']['completion_tokens'] == 0


def test_refund_and_admission_have_only_one_winner(application_storage):
    from lib.billing import get_balance, reserve_release
    from lib.storage.errors import StorageError
    user, owner = setup(application_storage)
    barrier = threading.Barrier(2)
    def act(admit):
        barrier.wait(timeout=10)
        try:
            if admit:
                owner.protect()
            else:
                reserve_release(user.id, 100, ref_id='request')
            return True
        except StorageError:
            return False
    with ThreadPoolExecutor(max_workers=2) as workers:
        one, two = workers.submit(act, True), workers.submit(act, False)
        admitted, refunded = one.result(timeout=20), two.result(timeout=20)
    assert admitted != refunded
    assert get_balance(user.id) == (9900 if admitted else 10000)


def test_lost_admission_ack_never_grants_provider_entry(application_storage, monkeypatch):
    from lib.billing import get_balance
    user, owner = setup(application_storage)
    command = application_storage.command
    def lost(operation, payload, *args, **kwargs):
        result = command(operation, payload, *args, **kwargs)
        if operation == 'billing.wallet.apply' and payload.get('kind') == REQUEST_ADMISSION_KIND:
            raise OSError('admission committed ACK lost')
        return result
    with monkeypatch.context() as fault:
        fault.setattr(application_storage, 'command', lost)
        with pytest.raises(OSError):
            owner.protect()
        with pytest.raises(IncompleteRequestUsage):
            attempt(owner, {'prompt_tokens': 1, 'completion_tokens': 1})
    with pytest.raises(IncompleteRequestUsage):
        recover(wallet_user_id=user.id, task_id='request', held_micro=100)
    assert get_balance(user.id) == 9900


@pytest.mark.parametrize('kind', [REQUEST_ADMISSION_KIND, REQUEST_INVOICE_KIND])
def test_raw_ledger_append_cannot_forge_request_evidence(application_storage, kind):
    from lib.storage.errors import StorageError
    user, owner = setup(application_storage)
    owner.protect()
    with pytest.raises(StorageError):
        application_storage.command('billing.ledger.append', dict(id='forged', user_id=user.id,
            ts=1, amount_micro=0, kind=kind, ref_type='reserve', ref_id='request',
            balance_after_micro=9900, note='{}'), 'forged')


def test_foreign_owner_and_changed_reservation_are_rejected(application_storage):
    user, _ = setup(application_storage)
    for owner, held in [(user.owner_user_id + 1, 100), (user.owner_user_id, 101)]:
        with pytest.raises(ValueError):
            RequestSettlement(request_id='request', owner=owner, wallet=user.id, held=held).protect()


def test_proven_unsent_attempt_refunds_but_unverified_counts_do_not(application_storage):
    from lib.billing import get_balance
    user, owner = setup(application_storage)
    owner.protect()
    attempt(owner, None, dispatched=False)
    assert owner.settle()['cost_micro'] == 0
    assert get_balance(user.id) == 10000
    assert owner.settle()['cost_micro'] == 0


def test_known_counters_from_failed_transport_cannot_settle(application_storage):
    from lib.billing import get_balance
    user, owner = setup(application_storage)
    owner.protect()
    lease = attempt(owner, {'prompt_tokens': 100, 'completion_tokens': 20})
    lease.confirm_input(successful=False)
    with pytest.raises(IncompleteRequestUsage):
        owner.settle()
    assert get_balance(user.id) == 9900


def test_missing_price_retains_hold(application_storage):
    from lib.billing import get_balance
    user, owner = setup(application_storage)
    owner.protect()
    lease = attempt(owner, {'prompt_tokens': 100, 'completion_tokens': 20})
    lease.model = 'synthetic-no-known-price-row'
    with pytest.raises(IncompleteRequestUsage, match='price'):
        owner.settle()
    assert get_balance(user.id) == 9900


def test_oversized_counter_evidence_retains_hold(application_storage):
    from lib.billing import get_balance
    user, owner = setup(application_storage)
    owner.protect()
    with pytest.raises(IncompleteRequestUsage, match='bound'):
        attempt(owner, {'prompt_tokens': '1' * 5000, 'completion_tokens': 1})
    with pytest.raises(IncompleteRequestUsage):
        owner.settle()
    assert get_balance(user.id) == 9900
