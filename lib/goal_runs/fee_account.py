"""Freeze trusted launch fee authority; recovery never infers it from absence.

The ordinary billing admission supplies task fields. This module only describes
that admission; native claim/consumption recheck immutable launches and ledgers.
"""
from lib.orchestration.execution_journal import RecoveryRefused


def from_task(task):
    wallet = task.get('_billingUserId')
    held = task.get('_billing_reservation_micro', 0)
    if type(held) is not int or held < 0 or held > 9223372036854775807:
        raise RecoveryRefused('Goal launch has an invalid fee reservation')
    if wallet is None or wallet == '':
        if held:
            raise RecoveryRefused('Goal launch reservation has no owned wallet')
        return {'mode': 'cost'}
    if not isinstance(wallet, str):
        raise RecoveryRefused('Goal launch requires an owned wallet')
    return {'mode': 'wallet', 'wallet_user_id': wallet, 'reserved_micro': held}


def original_reservation(task, metadata, *, storage, deadline):
    """Resolve original authority before the native claim; legacy is wallet only."""
    target = from_task(task)
    source = metadata.get('fee_account')
    if source == {'mode': 'cost'}:
        if target != source:
            raise RecoveryRefused('Goal recovery fee mode changed')
        return None, 0
    if target['mode'] != 'wallet':
        raise RecoveryRefused('Pending Goal requires its original owned billing wallet')
    if not target['reserved_micro']:
        raise RecoveryRefused('Pending Goal requires a fresh positive wallet reservation')
    wallet = target['wallet_user_id']
    reservation = storage.query('billing.ledger.find', dict(user_id=wallet, kind='reserve',
        ref_type='reserve', ref_id=metadata['task_id']), deadline=deadline)
    if (not isinstance(reservation, dict) or reservation.get('user_id') != wallet
            or reservation.get('ref_id') != metadata['task_id']
            or type(reservation.get('amount_micro')) is not int or reservation['amount_micro'] >= 0):
        raise RecoveryRefused('Pending Goal has no exact original reservation')
    held = -reservation['amount_micro']
    if 'fee_account' in metadata and source != dict(mode='wallet', wallet_user_id=wallet, reserved_micro=held):
        raise RecoveryRefused('Goal recovery original fee authority changed')
    return wallet, held
