"""lib.billing.payments — Payment provider integrations.

Each provider is a thin module that:
  1. Validates the incoming webhook (signature / sign / certificate).
  2. Idempotently records the payment in ``billing_payments``.
  3. On successful settlement, calls ``billing.deposit(kind='topup',
     ref_type='payment', ref_id=<provider_id>)`` so the user's
     wallet reflects the new balance.

The wallet API's idempotency on ``(user_id, kind, ref_type, ref_id)``
means a webhook re-delivery is a no-op — critical because both Stripe
and Alipay retry aggressively until they get a 2xx.

Provider-specific config lives at ``data/config/payments.json``::

    {
      "stripe": {
        "secret_key": "sk_live_...",
        "webhook_secret": "whsec_..."
      },
      "alipay": {
        "app_id": "...",
        "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----...",
        "alipay_public_key_pem": "-----BEGIN PUBLIC KEY-----..."
      },
      "credit_per_minor_unit": 1.0
    }

The ``credit_per_minor_unit`` is the conversion factor from the
provider's smallest currency unit (cents / 分) to credits. Default 1.0
means 1 cent → 1 credit (so $1 = 100 credits = 100,000,000 µ at the
1-credit-per-tenth-cent canonical rate). Operators tune this to match
their own pricing.
"""

from __future__ import annotations

from lib.billing.payments.stripe import handle_stripe_webhook
from lib.billing.payments.alipay import (
    create_alipay_order, handle_alipay_notify,
)
from lib.billing.payments._common import (
    PaymentRecord, list_payments, record_payment, mark_payment_settled,
)

__all__ = [
    'PaymentRecord',
    'list_payments', 'record_payment', 'mark_payment_settled',
    'handle_stripe_webhook',
    'create_alipay_order', 'handle_alipay_notify',
]
