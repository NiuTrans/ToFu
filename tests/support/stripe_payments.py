"""Synthetic signed Stripe messages shared by admission and real-storage specs."""

import hashlib
import hmac

NOW = 1_800_000_000
SECRET = 'whsec_synthetic'


def signature(payload, *, timestamp=NOW, secret=SECRET):
    signed = str(timestamp).encode('ascii') + b'.' + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f't={timestamp},v1={digest}'


def checkout(*, event_type='checkout.session.completed', **overrides):
    obj = {
        'id': 'cs_synthetic', 'mode': 'payment', 'payment_status': 'paid',
        'payment_intent': 'pi_synthetic', 'amount_total': 200,
        'currency': 'usd', 'metadata': {'user_id': 'usr_synthetic'},
    }
    obj.update(overrides)
    return {'type': event_type, 'data': {'object': obj}}


def intent(**overrides):
    obj = {
        'id': 'pi_synthetic', 'amount_received': 200, 'amount': 250,
        'currency': 'usd', 'metadata': {'user_id': 'usr_synthetic'},
    }
    obj.update(overrides)
    return {'type': 'payment_intent.succeeded', 'data': {'object': obj}}


