"""lib.billing — Multi-tenant credit accounting for the Tofu relay.

The billing layer is OFF for personal/private installs (no users table
rows, middleware short-circuits). It activates when ``auth_mode`` is
``multi-user`` AND a user account exists for the request's API key.

Unit
----
All amounts are stored in **micro-credits**: 1 credit = 1,000,000 µ.
Conversion to display currency happens only at presentation time, via
:func:`format_credits`. This keeps every accounting value an integer
(no float rounding bugs) and gives ~10⁹ resolution per US dollar at
the default $0.001/credit conversion. Payment providers' minor units
(cents/分) are converted on ingest.

Layered API
-----------

  ``pricing``  — model → unit prices, hot-reloadable from
                  ``data/config/pricing.json``.
  ``cost``     — pure: tokens × unit price → micro-credits.
  ``ledger``   — append-only log; every credit movement lives here.
  ``wallet``   — denormalized balance cache + atomic debit/credit.
  ``users``    — user row CRUD; binds API keys to a wallet.

Hot path::

    from lib.billing import wallet, cost, pricing
    estimate = cost.estimate_request_cost(model, prompt_tokens=prompt_tokens)
    wallet.reserve(user_id, estimate, ref_id=task_id)
    # ... call upstream LLM ...
    actual = cost.compute_request_cost(
        model, input_tokens=prompt_tokens, output_tokens=completion_tokens)
    wallet.settle(user_id, reserved=estimate, actual=actual, ref_id=task_id)

Reservations are themselves ledger entries (kind=``reserve``), released
on settle (kind=``reserve_release``). A debit (kind=``debit``) records
the actual usage. This makes every step recoverable: a crash mid-stream
leaves a dangling reserve which a janitor sweep cleans up after the
configured timeout (default 30 minutes).
"""

from __future__ import annotations

from lib.billing.cost import (
    compute_request_cost,
    estimate_request_cost,
    format_credits,
    micro_to_credits,
    credits_to_micro,
)
from lib.billing.ledger import (
    append_entry,
    list_entries,
    LedgerEntry,
    LEDGER_KINDS,
)
from lib.billing.pricing import (
    ModelPrice,
    get_price,
    list_prices,
    reload_pricing,
    save_margin,
    PricingError,
)
from lib.billing.users import (
    create_user,
    find_user,
    get_user,
    list_users,
    set_user_status,
    update_user_role,
)
from lib.billing.wallet import (
    BillingError,
    InsufficientFunds,
    debit,
    deposit,
    get_balance,
    get_wallet,
    reserve,
    reserve_release,
    settle,
)
from lib.billing.wallet_janitor import sweep_stale_reserves

__all__ = [
    # pricing
    'ModelPrice', 'get_price', 'list_prices', 'reload_pricing',
    'save_margin', 'PricingError',
    # cost
    'compute_request_cost', 'estimate_request_cost',
    'format_credits', 'micro_to_credits', 'credits_to_micro',
    # ledger
    'LedgerEntry', 'LEDGER_KINDS', 'append_entry', 'list_entries',
    # wallet
    'BillingError', 'InsufficientFunds',
    'debit', 'deposit', 'get_balance', 'get_wallet',
    'reserve', 'reserve_release', 'settle',
    'sweep_stale_reserves',
    # users
    'create_user', 'find_user', 'get_user', 'list_users',
    'set_user_status', 'update_user_role',
]
