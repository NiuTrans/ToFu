"""Atomic Sidecar contract for redemption-code lifecycle."""

from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest

pytest_plugins = ('tests._billing_user_sidecar',)
pytestmark = pytest.mark.unit


def _unique_code(label: str) -> str:
    return f'TEST-{label}-{uuid.uuid4().hex[:16]}'.upper()


def test_mint_list_and_redeem_share_one_repository_boundary():
    from lib.billing import (
        RedeemCodeAlreadyUsed,
        get_wallet,
        list_redeem_codes,
        mint_redeem_codes,
        redeem_code,
    )

    code = _unique_code('LIFECYCLE')
    created = mint_redeem_codes(
        [code], amount_micro=250, batch='contract', created_by='admin')
    assert created == [code]
    listed = list_redeem_codes(batch='contract', status='unredeemed')
    assert code in {item.code for item in listed}

    redemption = redeem_code(code, user_id='redeem-owner')
    assert redemption.code.redeemed_by == 'redeem-owner'
    assert redemption.wallet.balance_micro == 250
    with pytest.raises(RedeemCodeAlreadyUsed):
        redeem_code(code, user_id='other-owner')
    assert get_wallet('other-owner').balance_micro == 0
    assert get_wallet('redeem-owner').balance_micro == 250


def test_concurrent_redemption_credits_exactly_one_wallet():
    from lib.billing import (
        RedeemCodeAlreadyUsed,
        get_wallet,
        mint_redeem_codes,
        redeem_code,
    )

    code = _unique_code('RACE')
    mint_redeem_codes(
        [code], amount_micro=700, batch='race', created_by='admin')

    def consume(user_id: str) -> str:
        try:
            redeem_code(code, user_id=user_id)
            return 'redeemed'
        except RedeemCodeAlreadyUsed:
            return 'used'

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(consume, ('race-a', 'race-b')))
    assert sorted(outcomes) == ['redeemed', 'used']
    assert (
        get_wallet('race-a').balance_micro
        + get_wallet('race-b').balance_micro
    ) == 700


def test_expired_code_never_credits_the_wallet():
    from lib.billing import (
        RedeemCodeExpired,
        get_wallet,
        mint_redeem_codes,
        redeem_code,
    )

    code = _unique_code('EXPIRED')
    mint_redeem_codes(
        [code], amount_micro=900, batch='expired',
        created_by='admin', created_at=10, expires_at=20)
    with pytest.raises(RedeemCodeExpired):
        redeem_code(code, user_id='expired-owner', redeemed_at=21)
    assert get_wallet('expired-owner').balance_micro == 0


def test_fault_between_credit_and_consume_rolls_back_both(
    tmp_path, monkeypatch,
):
    from lib.storage import StorageError, StorageSupervisor

    monkeypatch.setenv('TOFU_STORAGE_ENABLE_FAULT_INJECTION', '1')
    monkeypatch.setenv(
        'TOFU_STORAGE_FAULT_ONCE', 'billing.redeem_code.before_mark_used')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        client = supervisor.client
        code = _unique_code('ROLLBACK')
        client.command('billing.redeem_codes.mint', {
            'codes': [code], 'amount_micro': 500, 'batch': 'rollback',
            'created_by': 'admin', 'created_at': 10, 'expires_at': 0,
            'note': '',
        }, 'mint-rollback-code')
        payload = {
            'code': code, 'user_id': 'rollback-owner', 'redeemed_at': 20,
            'ledger_id': 'rollback-ledger',
        }
        with pytest.raises(StorageError):
            client.command(
                'billing.redeem_code.apply', payload, 'redeem-rollback-code')
        assert client.query(
            'billing.wallet.get', {'user_id': 'rollback-owner'}
        )['balance_micro'] == 0
        code_row = client.query('billing.redeem_codes.list', {
            'batch': 'rollback', 'status': 'unredeemed',
            'limit': 10, 'offset': 0,
        })
        assert [row['code'] for row in code_row] == [code]

        result = client.command(
            'billing.redeem_code.apply', payload, 'redeem-rollback-code')
        assert result['status'] == 'redeemed'
        assert result['wallet']['balance_micro'] == 500
    finally:
        supervisor.stop()
