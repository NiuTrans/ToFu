"""lib.billing.payments — Payment provider integrations.

Providers admit authenticated payment facts and hand off to _common's semantic
storage commands. Alipay registers a local order before exposing its redirect;
Stripe records the confirmed PaymentIntent. The selected storage authority
atomically settles payment, wallet and ledger once per provider payment ID.

Provider-specific config lives at ``data/config/payments.json``::

    {
      "stripe": {
        "secret_key": "sk_live_...",
        "webhook_secret": "whsec_..."
      },
      "alipay": {
        "app_id": "...",
        "seller_id": "2088...",
        "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----...",
        "alipay_public_key_pem": "-----BEGIN PUBLIC KEY-----..."
      },
      "credit_per_minor_unit": 1.0
    }

The ``credit_per_minor_unit`` is the conversion factor from the
provider's smallest currency unit (cents / 分) to credits. Default 1.0
means 1 cent → 1 credit (so $1 = 100 credits = 100,000,000 µ).
Operators tune this to match their own pricing; invalid configured rates refuse
new payments. Alipay app_id, seller_id and both keys are required for checkout.
"""

from __future__ import annotations

from lib.billing.payments.stripe import (
    handle_stripe_webhook, create_stripe_checkout,
)
from lib.billing.payments.alipay import (
    create_alipay_order, handle_alipay_notify,
)
from lib.billing.payments._common import (
    PaymentRecord, list_payments, record_payment, mark_payment_settled,
)

__all__ = [
    'PaymentRecord',
    'list_payments', 'record_payment', 'mark_payment_settled',
    'handle_stripe_webhook', 'create_stripe_checkout',
    'create_alipay_order', 'handle_alipay_notify',
]
