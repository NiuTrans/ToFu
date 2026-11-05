"""Declarative billing operation catalog; SQL lives in operations_pkg._billing_*.

Wallet/ledger, redemption and payment owners share scalar validation and the
wallet transaction boundary. No backend IO or delivery policy is defined here.
"""

from lib.storage_authority_sql import operations as ops
from lib.storage_authority_sql.generated_billing_contract import LEDGER_KINDS
from lib.storage_authority_sql.operations_pkg._billing_wallets import (
    _wallet_get, _wallet_apply, _wallet_settle, _ledger_find, _ledger_append, _ledger_list, _ledger_recompute, _stale_reserves,
)
from lib.storage_authority_sql.operations_pkg._billing_redeem import (
    _redeem_codes_mint, _redeem_code_apply, _redeem_codes_list,
)
from lib.storage_authority_sql.operations_pkg._billing_payments import (
    _payment_bind, _payment_find, _payment_record, _payment_settle, _payment_list,
)


OPERATIONS = {
    'billing.wallet.get': ops.OperationSpec('query', False, _wallet_get),
    'billing.wallet.apply': ops.OperationSpec('command', True, _wallet_apply),
    'billing.wallet.settle': ops.OperationSpec('command', True, _wallet_settle),
    'billing.ledger.find': ops.OperationSpec('query', False, _ledger_find),
    'billing.ledger.append': ops.OperationSpec('command', True, _ledger_append),
    'billing.ledger.list': ops.OperationSpec('query', False, _ledger_list),
    'billing.ledger.recompute': ops.OperationSpec('query', False, _ledger_recompute),
    'billing.reserve.stale': ops.OperationSpec('query', False, _stale_reserves),
    'billing.redeem_codes.mint': ops.OperationSpec(
        'command', True, _redeem_codes_mint),
    'billing.redeem_code.apply': ops.OperationSpec(
        'command', True, _redeem_code_apply),
    'billing.redeem_codes.list': ops.OperationSpec(
        'query', False, _redeem_codes_list),
    'billing.payment.bind': ops.OperationSpec('command', True, _payment_bind),
    'billing.payment.find': ops.OperationSpec('query', False, _payment_find),
    'billing.payment.record': ops.OperationSpec('command', True, _payment_record),
    'billing.payment.settle': ops.OperationSpec('command', True, _payment_settle),
    'billing.payment.list': ops.OperationSpec('query', False, _payment_list),
}

__all__ = ['LEDGER_KINDS', 'OPERATIONS']
