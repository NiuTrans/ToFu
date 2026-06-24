"""tests/test_billing_phase2.py — Phase 2-6 relay billing.

Covers:
  - lib.billing.users (signup, login, password hashing, suspension)
  - lib.billing.payments._common (record + settle idempotency)
  - lib.billing.payments.stripe (signature verify, settle path)
  - lib.billing.payments.alipay (sign string canonical form)
  - lib.billing.janitor (stale reservation sweep)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch


class _BillingPhase2Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        # Fresh per-class SQLite DB via the dedicated test helper. A bare
        # TOFU_DB_PATH env set is a no-op post-import and (under an ambient PG
        # env) would silently share the live database — see the helper's
        # docstring. This makes the suite pass regardless of run order or
        # backend.
        from lib.database import reset_sqlite_for_tests
        cls._db_snapshot = reset_sqlite_for_tests(
            os.path.join(cls._tmp.name, 'tofu.db'))
        cls._pricing_path = os.path.join(cls._tmp.name, 'pricing.json')
        cls._payments_path = os.path.join(cls._tmp.name, 'payments.json')
        cls._pricing_patch = patch('lib.billing.pricing._PRICING_PATH',
                                    cls._pricing_path)
        cls._pricing_patch.start()
        from lib.billing import pricing as _p
        _p.reload_pricing()

    @classmethod
    def tearDownClass(cls):
        cls._pricing_patch.stop()
        from lib.database import restore_db_state
        restore_db_state(getattr(cls, '_db_snapshot', None))
        cls._tmp.cleanup()


# ── users ───────────────────────────────────────────────────────────

class UsersTest(_BillingPhase2Base):

    def test_signup_creates_user_with_active_status(self):
        from lib.billing.users import create_user, get_user
        user = create_user('alice@example.com', password='supersecret',
                           display_name='Alice')
        self.assertEqual(user.email, 'alice@example.com')
        self.assertEqual(user.role, 'user')
        self.assertEqual(user.status, 'active')
        # Round-trip
        again = get_user(user.id)
        self.assertEqual(again.email, 'alice@example.com')

    def test_duplicate_email_rejected(self):
        from lib.billing.users import create_user
        create_user('bob@example.com', password='whatever1')
        with self.assertRaises(ValueError):
            create_user('bob@example.com', password='other')

    def test_invalid_email_rejected(self):
        from lib.billing.users import create_user
        with self.assertRaises(ValueError):
            create_user('not-an-email', password='whatever1')

    def test_authenticate_correct_password(self):
        from lib.billing.users import create_user, authenticate
        create_user('carol@example.com', password='Correct-Horse-Battery-Staple')
        user = authenticate('carol@example.com',
                             'Correct-Horse-Battery-Staple')
        self.assertIsNotNone(user)
        self.assertEqual(user.email, 'carol@example.com')

    def test_authenticate_wrong_password_returns_none(self):
        from lib.billing.users import create_user, authenticate
        create_user('dave@example.com', password='right-password')
        self.assertIsNone(authenticate('dave@example.com', 'wrong'))

    def test_authenticate_suspended_user_returns_none(self):
        from lib.billing.users import (
            create_user, authenticate, set_user_status,
        )
        u = create_user('eve@example.com', password='right-password')
        set_user_status(u.id, 'suspended')
        self.assertIsNone(authenticate('eve@example.com', 'right-password'))


# ── payments ────────────────────────────────────────────────────────

class PaymentsCommonTest(_BillingPhase2Base):

    def test_record_payment_idempotent_on_provider_id(self):
        from lib.billing.users import create_user
        from lib.billing.payments import record_payment
        u = create_user('frank@example.com', password='password1')
        r1 = record_payment(user_id=u.id, provider='stripe',
                             provider_id='pi_test_123',
                             amount_minor=500, currency='USD')
        r2 = record_payment(user_id=u.id, provider='stripe',
                             provider_id='pi_test_123',
                             amount_minor=999, currency='USD')
        self.assertEqual(r1.id, r2.id)
        # The first call's amount wins — this is idempotency, not "last write".
        self.assertEqual(r2.amount_minor, 500)

    def test_settle_credits_wallet_idempotently(self):
        from lib.billing import get_balance
        from lib.billing.users import create_user
        from lib.billing.payments import record_payment, mark_payment_settled
        u = create_user('greta@example.com', password='password1')
        rec = record_payment(user_id=u.id, provider='stripe',
                              provider_id='pi_test_456',
                              amount_minor=1000, currency='USD')
        before = get_balance(u.id)
        mark_payment_settled(rec.id)
        after = get_balance(u.id)
        self.assertGreater(after, before)
        # Re-call: balance must NOT change.
        mark_payment_settled(rec.id)
        self.assertEqual(get_balance(u.id), after)


# ── stripe webhook ──────────────────────────────────────────────────

class StripeWebhookTest(_BillingPhase2Base):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Seed a payments.json so stripe.webhook_secret resolves.
        from lib.json_store import write_json_atomic
        write_json_atomic(cls._payments_path, {
            'stripe': {'webhook_secret': 'whsec_testsecret'},
            'alipay': {},
            'credit_per_minor_unit': 1.0,
        })
        cls._pay_patch = patch(
            'lib.billing.payments._common.config_path',
            lambda *p: cls._payments_path)
        cls._pay_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._pay_patch.stop()
        super().tearDownClass()

    @staticmethod
    def _sign(payload: bytes, secret: str = 'whsec_testsecret', ts: int = None):
        ts = ts or int(time.time())
        signed = f'{ts}.{payload.decode()}'.encode()
        v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return f't={ts},v1={v1}'

    def test_unknown_event_returns_200(self):
        from lib.billing.payments.stripe import handle_stripe_webhook
        payload = json.dumps({'type': 'charge.refunded',
                              'data': {'object': {}}}).encode()
        sig = self._sign(payload)
        status, body = handle_stripe_webhook(payload, sig)
        self.assertEqual(status, 200)
        self.assertEqual(body['note'], 'unhandled_event')

    def test_bad_signature_rejected(self):
        from lib.billing.payments.stripe import handle_stripe_webhook
        payload = b'{}'
        status, body = handle_stripe_webhook(payload, 't=0,v1=DEADBEEF')
        self.assertEqual(status, 400)

    def test_payment_intent_succeeded_credits_user(self):
        from lib.billing import get_balance
        from lib.billing.users import create_user
        from lib.billing.payments.stripe import handle_stripe_webhook
        u = create_user('helen@example.com', password='password1')
        before = get_balance(u.id)
        payload = json.dumps({
            'id': 'evt_test_1',
            'type': 'payment_intent.succeeded',
            'data': {'object': {
                'id': 'pi_helen_1',
                'amount_received': 2000,
                'currency': 'usd',
                'metadata': {'user_id': u.id},
            }},
        }).encode()
        status, body = handle_stripe_webhook(payload, self._sign(payload))
        self.assertEqual(status, 200)
        self.assertGreater(body['credit_micro'], 0)
        self.assertGreater(get_balance(u.id), before)
        # Replay protection.
        bal_after = get_balance(u.id)
        handle_stripe_webhook(payload, self._sign(payload))
        self.assertEqual(get_balance(u.id), bal_after)


# ── stripe checkout-session creation ────────────────────────────────

class StripeCheckoutTest(_BillingPhase2Base):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from lib.json_store import write_json_atomic
        write_json_atomic(cls._payments_path, {
            'stripe': {'secret_key': 'sk_test_xyz'},
            'alipay': {},
            'credit_per_minor_unit': 1.0,
        })
        cls._pay_patch = patch(
            'lib.billing.payments._common.config_path',
            lambda *p: cls._payments_path)
        cls._pay_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._pay_patch.stop()
        super().tearDownClass()

    def test_create_checkout_session_stamps_user_and_records_pending(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from lib.billing.users import create_user
        from lib.billing.payments import create_stripe_checkout, list_payments

        u = create_user('kim@example.com', password='password1')

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            'id': 'cs_test_abc', 'url': 'https://checkout.stripe.com/c/pay/cs_test_abc'}

        captured = {}

        async def _fake_post(url, **kw):
            captured['url'] = url
            captured['kw'] = kw
            return fake_resp

        with patch('lib.http_client.async_http_post',
                    new=AsyncMock(side_effect=_fake_post)):
            session_id, url = asyncio.run(create_stripe_checkout(
                user_id=u.id, amount_minor=1500, currency='usd',
                success_url='https://example.com/ok'))

        self.assertEqual(session_id, 'cs_test_abc')
        self.assertTrue(url.startswith('https://checkout.stripe.com/'))
        # The Stripe API was called with the secret key as basic-auth user.
        self.assertTrue(captured['url'].endswith('/checkout/sessions'))
        self.assertEqual(captured['kw']['auth'], ('sk_test_xyz', ''))
        form = captured['kw']['data']
        self.assertEqual(form['metadata[user_id]'], u.id)
        self.assertEqual(form['line_items[0][price_data][unit_amount]'], '1500')
        self.assertEqual(form['success_url'], 'https://example.com/ok')
        # A pending payment row keyed on the session id is recorded.
        rows = list_payments(user_id=u.id, provider='stripe')
        self.assertTrue(any(r.provider_id == 'cs_test_abc'
                            and r.status == 'pending' for r in rows))

    def test_create_checkout_requires_secret_key(self):
        import asyncio
        from lib.json_store import write_json_atomic
        from lib.billing.payments import create_stripe_checkout
        # Blank out the secret key.
        write_json_atomic(self._payments_path, {
            'stripe': {}, 'alipay': {}, 'credit_per_minor_unit': 1.0})
        try:
            with self.assertRaises(RuntimeError):
                asyncio.run(create_stripe_checkout(
                    user_id='u1', amount_minor=500, currency='usd',
                    success_url='https://example.com/ok'))
        finally:
            write_json_atomic(self._payments_path, {
                'stripe': {'secret_key': 'sk_test_xyz'},
                'alipay': {}, 'credit_per_minor_unit': 1.0})

    def test_create_checkout_surfaces_stripe_error(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from lib.billing.payments import create_stripe_checkout

        fake_resp = MagicMock()
        fake_resp.status_code = 400
        fake_resp.json.return_value = {'error': {'message': 'amount too small'}}
        fake_resp.text = '{"error":{"message":"amount too small"}}'

        async def _fake_post(url, **kw):
            return fake_resp

        with patch('lib.http_client.async_http_post',
                    new=AsyncMock(side_effect=_fake_post)):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(create_stripe_checkout(
                    user_id='u1', amount_minor=1, currency='usd',
                    success_url='https://example.com/ok'))
        self.assertIn('amount too small', str(ctx.exception))


# ── alipay sign-string canonicalisation ─────────────────────────────

class AlipaySignStringTest(unittest.TestCase):
    """Pure-function test: the canonical string must drop empty fields,
    drop `sign`/`sign_type`, and alpha-sort the remainder."""

    def test_canonical_form(self):
        from lib.billing.payments.alipay import _build_sign_string
        s = _build_sign_string({
            'app_id': '2021000000',
            'method': 'alipay.trade.page.pay',
            'sign': 'X',
            'sign_type': 'RSA2',
            'biz_content': '{"a":1}',
            'empty_field': '',
        })
        # Alpha-sorted, no empties, no sign/sign_type.
        self.assertEqual(
            s, 'app_id=2021000000&biz_content={"a":1}&'
               'method=alipay.trade.page.pay')


# ── janitor ─────────────────────────────────────────────────────────

class JanitorTest(_BillingPhase2Base):

    def test_sweep_releases_old_reservation(self):
        from lib.billing import deposit, get_balance
        from lib.billing.users import create_user
        from lib.billing.ledger import append_entry
        from lib.billing.janitor import sweep_once
        u = create_user('ivan@example.com', password='password1')
        deposit(u.id, 10_000_000, kind='topup', ref_id='ivan_init')
        baseline = get_balance(u.id)
        # Manually post a reserve from 2 hours ago without a settle.
        # (Simulates a crashed task.)
        old_ts = int(time.time()) - 7200
        append_entry(user_id=u.id, amount_micro=-1_500_000,
                     kind='reserve', ref_type='reserve',
                     ref_id='task_orphan_1',
                     balance_after_micro=baseline - 1_500_000,
                     ts=old_ts)
        # Don't update the wallet cache — the sweep should still
        # succeed even if cache is in sync.
        with patch.dict(os.environ, {'TOFU_BILLING_JANITOR_TTL': '60'}):
            stats = sweep_once()
        self.assertGreaterEqual(stats['released'], 1)
        # A second sweep must NOT double-release (the release entry
        # itself satisfies the NOT EXISTS clause).
        with patch.dict(os.environ, {'TOFU_BILLING_JANITOR_TTL': '60'}):
            stats2 = sweep_once()
        self.assertEqual(stats2['released'], 0)

    def test_sweep_skips_running_task(self):
        # A reserve whose ref_id matches a still-running task in the
        # in-memory registry must NOT be released.
        from lib.billing import deposit
        from lib.billing.users import create_user
        from lib.billing.ledger import append_entry
        from lib.billing.janitor import sweep_once
        u = create_user('john@example.com', password='password1')
        deposit(u.id, 10_000_000, kind='topup', ref_id='john_init')
        old_ts = int(time.time()) - 7200
        ref = 'task_running_xyz'
        append_entry(user_id=u.id, amount_micro=-1_000_000,
                     kind='reserve', ref_type='reserve', ref_id=ref,
                     balance_after_micro=9_000_000, ts=old_ts)
        # Pretend the task is still in flight.
        with patch('lib.billing.janitor._is_task_still_running',
                    return_value=True), \
             patch.dict(os.environ, {'TOFU_BILLING_JANITOR_TTL': '60'}):
            stats = sweep_once()
        self.assertEqual(stats['released'], 0)
        self.assertGreaterEqual(stats['skipped_running'], 1)


if __name__ == '__main__':
    unittest.main()
