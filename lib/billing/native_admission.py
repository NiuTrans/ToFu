"""Protect original native-execution holds through the existing wallet ledger.

Entry: protect_before_dispatch, called on the joined model-admission worker;
is_protected, used by cold recovery. One immutable zero-value admission entry
shares the wallet transaction with release exclusion. It is neither an invoice
nor a quote; exact terminal settlement remains native_settlement's responsibility.
"""
import threading

from lib.identity import require_user_id
from lib.storage_authority_sql.generated_billing_contract import NATIVE_ADMISSION_KIND


def is_protected(client, *, wallet_user_id, task_id):
    entry = client.query('billing.ledger.find', dict(user_id=wallet_user_id,
        kind=NATIVE_ADMISSION_KIND, ref_type='reserve', ref_id=task_id), deadline=2.0)
    if entry is None:
        return False
    _validate_entry(entry, wallet_user_id, task_id)
    return True


def _validate_entry(entry, wallet_user_id, task_id):
    if (not isinstance(entry, dict) or entry.get('user_id') != wallet_user_id
            or entry.get('kind') != NATIVE_ADMISSION_KIND or entry.get('ref_type') != 'reserve'
            or entry.get('ref_id') != task_id or type(entry.get('amount_micro')) is not int
            or entry['amount_micro'] != 0):
        raise ValueError('Native reservation admission evidence differs')


def protect_before_dispatch(participant, *, budget, owner):
    """Acknowledge the original root hold before any paid provider may enter.

    The cache lives only on that root and cannot survive/rebind to another run.
    Concurrent children share one admission; a lost ACK leaves no cached permit.
    Wallet ownership, original reservation and release exclusion are independent
    of best-effort task-result snapshots and survive their deletion.
    """
    owner = require_user_id(owner, context='native billing admission')
    root = budget.owned_execution_root(participant, owner=owner)
    wallet = root.get('_billingUserId')
    held = root.get('_billing_reservation_micro', 0)
    if type(held) is not int or held < 0:
        raise ValueError('Native reservation requires an exact nonnegative amount')
    identity = (root['id'], owner, wallet, held)
    prior = root.get('_nativeBillingAdmission')
    if prior is not None and prior != identity:
        raise ValueError('Native reservation admission identity changed')
    if held and (not isinstance(wallet, str) or not wallet):
        raise ValueError('Native reservation has no original wallet')
    if not wallet or held == 0:
        return
    if root.get('_nativePricingRequired') is not True:
        raise ValueError('Native billed root has no exact fee owner')
    lock = root.get('_nativeBillingAdmissionLock')
    if lock is None:
        lock = root.setdefault('_nativeBillingAdmissionLock', threading.Lock())
    with lock:
        budget.owned_execution_root(participant, owner=owner)
        prior = root.get('_nativeBillingAdmission')
        if prior is not None:
            if prior != identity:
                raise ValueError('Native reservation admission identity changed')
            return
        from lib.billing.reservation_admission import acknowledge
        acknowledge(owner=owner, wallet=wallet, request_id=root['id'], held=held,
                    kind=NATIVE_ADMISSION_KIND)
        # Do not keep the account lock across wallet I/O. Recheck live ownership
        # after ACK; the model callback also rechecks cancellation before entry.
        budget.owned_execution_root(participant, owner=owner)
        if identity != (root['id'], root.get('_userId'), root.get('_billingUserId'), root.get('_billing_reservation_micro')):
            raise ValueError('Native reservation changed during admission')
        root['_nativeBillingAdmission'] = identity
