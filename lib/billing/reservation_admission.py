"""Shared wallet barrier for native execution and direct provider requests.

The existing wallet transaction serializes admission against refund. This
module validates the original payer/hold and acknowledges immutable evidence;
it neither prices requests nor decides whether their effects may be retried.
"""
from lib.identity import require_user_id
from lib.storage_authority_sql.generated_billing_contract import (
    NATIVE_ADMISSION_KIND, REQUEST_ADMISSION_KIND,
)


def validate_entry(entry, wallet, request_id, kind):
    if (not isinstance(entry, dict) or entry.get('user_id') != wallet
            or entry.get('kind') != kind or entry.get('ref_type') != 'reserve'
            or entry.get('ref_id') != request_id or type(entry.get('amount_micro')) is not int
            or entry['amount_micro'] != 0):
        raise ValueError('Reservation admission evidence differs')


def acknowledge(*, owner, wallet, request_id, held, kind):
    from lib.billing.users import get_user
    from lib.billing.wallet import _stable_id
    from lib.storage import get_storage_client
    from lib.ids import short_id
    owner = require_user_id(owner, context='billing reservation admission')
    if kind not in {NATIVE_ADMISSION_KIND, REQUEST_ADMISSION_KIND}:
        raise ValueError('Unknown reservation admission kind')
    if type(held) is not int or held <= 0:
        raise ValueError('Reservation requires an exact positive original hold')
    user = get_user(wallet)
    if user is None or user.owner_user_id != owner:
        raise ValueError('Reservation wallet belongs to another owner')
    client = get_storage_client(write=True)
    reservation = client.query('billing.ledger.find', dict(user_id=wallet,
        kind='reserve', ref_type='reserve', ref_id=request_id), deadline=2.0)
    if (not isinstance(reservation, dict) or reservation.get('user_id') != wallet
            or reservation.get('ref_id') != request_id
            or type(reservation.get('amount_micro')) is not int
            or reservation['amount_micro'] != -held):
        raise ValueError('Reservation differs from its original durable hold')
    # A fresh command rechecks the live hold; an old delivery receipt cannot
    # authorize dispatch after settlement. Ledger identity deduplicates writes.
    result = client.command('billing.wallet.apply', dict(user_id=wallet,
        amount_micro=0, kind=kind, ref_type='reserve', ref_id=request_id,
        note='Provider usage requires complete terminal settlement', allow_negative=True,
        ledger_id=_stable_id('led_', wallet, kind, 'reserve', request_id)),
        short_id('cmd_'), deadline=5.0)
    if not isinstance(result, dict) or not (result.get('applied') is True or result.get('duplicate') is True):
        raise ValueError('Reservation admission was not acknowledged')
    validate_entry(result.get('entry'), wallet, request_id, kind)
