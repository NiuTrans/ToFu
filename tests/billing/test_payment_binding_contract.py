"""Binding preserves local quote authority, uniqueness, replay and rollback."""

import pytest
from uuid import uuid4

from lib.storage.errors import StorageError
from tests.support import billing_storage

pytestmark = pytest.mark.api

application_storage = billing_storage.application_storage
application_storage_owner_id = billing_storage.application_storage_owner_id


def record(client, payment_id='pay_local', **changes):
    value = dict(id=payment_id, user_id='usr_local', provider='stripe', provider_id='',
                 amount_minor=200, currency='USD', credit_micro=333, status='pending', raw={})
    value.update(changes)
    return client.command('billing.payment.record', value, uuid4().hex)['payment']


def bind(client, payment_id='pay_local', *, command_id=None, **changes):
    value = dict(payment_id=payment_id, user_id='usr_local', provider='stripe',
                 provider_id='pi_local', amount_minor=200, currency='USD')
    value.update(changes)
    return client.command('billing.payment.bind', value, command_id or uuid4().hex)


def rows(client):
    return client.query('billing.payment.list', dict(
        user_id='usr_local', provider='', status='', limit=100, offset=0))


def test_unbound_quotes_are_independent_and_primary_id_replay_freezes_fields(application_storage):
    client = application_storage
    original = record(client)
    record(client, 'pay_second')
    assert record(client, amount_minor=999, credit_micro=1, raw={'changed': True}) == original
    assert len(rows(client)) == 2
    for changes in ({'user_id': 'other'}, {'provider': 'other'}):
        with pytest.raises(StorageError) as error:
            record(client, **changes)
        assert error.value.code == 'database_conflict'
    assert sorted(row['provider_id'] for row in rows(client)) == ['', '']


@pytest.mark.parametrize('changes', [dict(payment_id='missing'), dict(user_id='other'),
    dict(provider='other'), dict(amount_minor=201), dict(currency='EUR')])
def test_binding_requires_every_frozen_fact(application_storage, changes):
    client = application_storage
    quote = record(client)
    assert bind(client, **changes) == {'accepted': False, 'payment': None}
    assert rows(client) == [quote]


@pytest.mark.parametrize('provider_id,status', [('', 'pending'), ('pi_failed', 'failed')])
def test_unbound_and_failed_rows_cannot_credit_a_wallet(application_storage, provider_id, status):
    client = application_storage
    quote = record(client, provider_id=provider_id, status=status)
    with pytest.raises(StorageError) as error:
        client.command('billing.payment.settle',
                       dict(payment_id=quote['id'], raw=None, ledger_id='ledger_local'), uuid4().hex)
    assert error.value.code == 'database_conflict'
    assert rows(client) == [quote]
    assert client.query('billing.wallet.get', {'user_id': 'usr_local'})['balance_micro'] == 0
    if status == 'failed':
        assert not bind(client, provider_id=provider_id)['accepted']


@pytest.mark.parametrize('changes', [dict(raw={}), dict(provider_id='pi_existing'),
    dict(status='failed'), dict(raw={'checkout_session': 'cs_other'})])
def test_legacy_migration_requires_a_pending_locally_recorded_session(application_storage, changes):
    client = application_storage
    values = dict(provider_id='cs_local', raw={'checkout_session': 'cs_local'})
    values.update(changes)
    quote = record(client, **values)
    assert not bind(client, legacy_session_id='cs_local')['accepted']
    assert rows(client) == [quote]


def test_sql_bind_failure_rolls_back_claim_and_same_command_can_retry(application_storage, monkeypatch):
    from lib.storage import service
    runtime = service.storage_runtime()
    if runtime.status()['backend'] not in {'sqlite', 'postgres'}:
        pytest.skip('SQL transaction fault point; native commit ACK has separate coverage')
    client = application_storage
    quote = record(client)
    runtime.stop()
    monkeypatch.setenv('TOFU_STORAGE_ENABLE_FAULT_INJECTION', '1')
    monkeypatch.setenv('TOFU_STORAGE_FAULT_ONCE', 'billing.payment.bind.before_commit')
    runtime.start()
    client = runtime.client()
    with pytest.raises(StorageError) as error:
        bind(client, command_id='bind-rollback')
    assert error.value.code == 'database_internal'
    assert rows(client) == [quote]
    assert client.query('billing.payment.find', dict(provider='stripe', provider_id='pi_local')) is None
    result = bind(client, command_id='bind-rollback')
    assert result['accepted'] and result['payment']['credit_micro'] == 333
    assert bind(client, command_id='bind-rollback') == result
