"""routes/api_v1/billing.py — Billing surface for the relay.

Routes
------

Customer-facing (any authenticated user can call these for their OWN
wallet; admins can pass ``?user_id=`` to inspect any user):

  GET  /api/v1/billing/wallet         — balance + alert threshold
  GET  /api/v1/billing/ledger         — paginated history
  POST /api/v1/billing/redeem         — consume a redemption code
  GET  /api/v1/billing/pricing        — public price table (read-only)

Admin-only:

  POST /api/v1/billing/deposit        — top up by hand (audit-logged)
  POST /api/v1/billing/debit          — adjust by hand (audit-logged)
  POST /api/v1/billing/redeem-codes   — mint a batch of codes
  GET  /api/v1/billing/redeem-codes   — list codes (filter by batch)

The ``/api/v1/billing/pricing`` GET is public (no auth) so the
customer dashboard can render before the user has logged in.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from quart import Blueprint, request

from lib.api_response import (
    api_bad_request, api_created, api_forbidden, api_not_found, api_ok,
)
from lib.billing import (
    cost as _cost,
    InsufficientFunds,
    RedeemCodeAlreadyUsed,
    RedeemCodeExpired,
    RedeemCodeNotFound,
    deposit, debit, get_wallet, list_entries,
    list_redeem_codes, mint_redeem_codes, redeem_code,
)
from lib.billing.users import get_user
from lib.ids import short_id
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import (
    async_parse_body, optional_int, optional_str, require_int, require_str,
)

from .auth import current_auth, require_scope

logger = get_logger(__name__)

api_v1_billing_bp = Blueprint('api_v1_billing', __name__)


def _billing_disabled_response():
    """Return a 404-style envelope when the relay runs in agent-only mode.

    Agent-only relays (``relay.json: billing_enabled=false``) don't move
    money — users bring their own model keys. The money-moving endpoints
    (redeem / deposit / debit / mint-codes / checkout) therefore return a
    disabled marker instead of silently succeeding. Read-only endpoints
    (wallet / ledger / pricing) stay reachable and just report empties.

    Returns ``None`` when billing is enabled (caller proceeds).
    """
    from lib.relay_config import billing_enabled
    if billing_enabled():
        return None
    return api_not_found(
        'Billing is disabled on this relay (agent-only mode). Users '
        'supply their own model endpoint; no credits are charged.',
        error_kind='billing_disabled')


def _resolve_target_user() -> str:
    """Pick the user_id this call should target.

    * Admin + ``?user_id=`` → that user.
    * Otherwise → the caller's ``user_id`` (from their AuthContext.metadata).

    Returns '' if no user can be resolved (personal install / open mode).
    """
    ctx = current_auth()
    requested = (request.args.get('user_id') or '').strip()
    if requested:
        if ctx is None or not ctx.has_scope('admin'):
            raise PermissionError('admin scope required to inspect '
                                   'another user')
        return requested
    if ctx is None:
        return ''
    return ctx.account_user_id or ''


def _wallet_snapshot_payload(snap) -> dict:
    return {
        'user_id': snap.user_id,
        'balance_micro': snap.balance_micro,
        'balance_credits': _cost.micro_to_credits(snap.balance_micro),
        'currency': snap.currency,
        'low_balance_alert_micro': snap.low_balance_alert_micro,
        'updated_at': snap.updated_at,
    }


def _wallet_payload(user_id: str) -> dict:
    return _wallet_snapshot_payload(get_wallet(user_id))


# ── Pricing (public) ────────────────────────────────────────────────

@api_v1_billing_bp.route('/api/v1/billing/pricing', methods=['GET'])
@api_meta(summary='Public price table',
          description='Per-model token prices in micro-credits per Mtok. '
                       'Public so customer dashboards can render the rate '
                       'card before login.',
          tags=['billing'], public=True)
async def get_pricing():
    from lib.relay_config import billing_enabled
    from lib.billing.pricing import get_default_margin
    from lib.pricing import build_rate_card, get_pricing_data
    card = build_rate_card()
    return api_ok(
        billing_enabled=billing_enabled(),
        currency='mixed',
        usd_to_cny=(get_pricing_data().get('usdToCny')),
        default_margin=get_default_margin(),
        models=card['models'], providers=card['providers'],
        version=2,
        unit='currency_per_million_tokens',
        notes=('A tier is selected once from complete prompt tokens; input, '
               'output, cache write and cache read share that tier. Final '
               'debit applies the configured relay margin.'),
    )


@api_v1_billing_bp.route('/api/v1/billing/pricing', methods=['PUT'])
@require_scope('admin')
@api_meta(summary='Admin: set the relay margin',
          description='Persist ONLY the relay profit margin '
                       '(``{"default_margin": 0.20}``) and hot-reload. '
                       'Per-model RATES are NOT editable here: they are '
                       'authoritative in ``lib/pricing.py`` (the single cost '
                       'engine) — a second writable rate table would just '
                       'drift. Billing-gated: 404 on agent-only relays.',
          tags=['billing'], scope='admin')
async def put_pricing_route():
    _disabled = _billing_disabled_response()
    if _disabled is not None:
        return _disabled
    from lib.billing import save_margin, PricingError
    body = await async_parse_body()
    if 'default_margin' not in (body or {}):
        return api_bad_request('default_margin required',
                                field='default_margin')
    try:
        saved = save_margin(body['default_margin'])
    except PricingError as e:
        return api_bad_request(str(e), error_kind='invalid_margin')
    audit_log('pricing_margin_updated',
              default_margin=saved.get('default_margin'),
              by=(current_auth().key_id if current_auth() else ''))
    return api_ok(
        currency=saved.get('currency', 'USD'),
        default_margin=saved.get('default_margin', 0.0),
        default_model=saved.get('default_model', {}),
        models=saved.get('models', {}),
        version=saved.get('version', 1),
        unit='micro_credits_per_mtok',
        note=('Per-model rates are read-only here; they are authoritative '
              'in lib/pricing.py. Only default_margin is editable.'),
    )


# ── Wallet (self / admin) ───────────────────────────────────────────

@api_v1_billing_bp.route('/api/v1/billing/wallet', methods=['GET'])
@api_meta(summary='Read wallet balance',
          description='Returns the cached wallet balance for the current '
                       'user. Admins may pass ``?user_id=…`` to inspect '
                       'any user.',
          tags=['billing'])
async def get_wallet_route():
    try:
        user_id = _resolve_target_user()
    except PermissionError as e:
        return api_forbidden(str(e))
    if not user_id:
        return api_ok({
            'user_id': '',
            'balance_micro': 0,
            'balance_credits': 0.0,
            'currency': 'CREDIT',
            'low_balance_alert_micro': 0,
            'updated_at': 0,
            'note': ('No wallet associated with this principal — '
                      'this deployment is not in multi-user mode.'),
        })
    return api_ok(_wallet_payload(user_id))


@api_v1_billing_bp.route('/api/v1/billing/ledger', methods=['GET'])
@api_meta(summary='Read ledger entries',
          description='Paginated, newest-first. Admins may pass '
                       '``?user_id=…``; everyone else gets their own.',
          tags=['billing'])
async def get_ledger_route():
    try:
        user_id = _resolve_target_user()
    except PermissionError as e:
        return api_forbidden(str(e))
    if not user_id:
        return api_ok(entries=[], total=0)
    try:
        limit = max(1, min(int(request.args.get('limit') or 100), 500))
        offset = max(0, int(request.args.get('offset') or 0))
    except (ValueError, TypeError):
        return api_bad_request('limit/offset must be integers', field='limit')
    kinds_raw = request.args.get('kinds') or ''
    kinds = [k.strip() for k in kinds_raw.split(',') if k.strip()] or None
    entries = list_entries(user_id, limit=limit, offset=offset, kinds=kinds)
    return api_ok(
        entries=[{
            'id': e.id, 'ts': e.ts,
            'amount_micro': e.amount_micro,
            'amount_credits': _cost.micro_to_credits(e.amount_micro),
            'kind': e.kind, 'ref_type': e.ref_type, 'ref_id': e.ref_id,
            'balance_after_micro': e.balance_after_micro,
            'balance_after_credits':
                _cost.micro_to_credits(e.balance_after_micro),
            'note': e.note,
        } for e in entries],
        limit=limit, offset=offset,
    )


# ── Redeem codes (customer) ─────────────────────────────────────────

@api_v1_billing_bp.route('/api/v1/billing/redeem', methods=['POST'])
@api_meta(summary='Redeem a top-up code',
          description='Adds the code\'s amount to the caller\'s wallet. '
                       'Codes are single-use; expired or already-redeemed '
                       'codes return 400.',
          tags=['billing'])
async def redeem_route():
    _disabled = _billing_disabled_response()
    if _disabled is not None:
        return _disabled
    try:
        user_id = _resolve_target_user()
    except PermissionError as e:
        return api_forbidden(str(e))
    if not user_id:
        return api_bad_request(
            'No wallet for this principal — log in as a multi-user '
            'account first.', error_kind='no_wallet')
    body = await async_parse_body()
    code = require_str(body, 'code', max_len=64).strip()
    if not code:
        return api_bad_request('code required', field='code')
    try:
        redemption = await asyncio.to_thread(
            redeem_code, code, user_id=user_id, redeemed_at=int(time.time()))
    except RedeemCodeNotFound:
        return api_not_found('No such code', error_kind='code_not_found')
    except RedeemCodeAlreadyUsed:
        return api_bad_request(
            'Code already redeemed',
            error_kind='already_redeemed')
    except RedeemCodeExpired:
        return api_bad_request('Code expired', error_kind='expired')
    amount = redemption.code.amount_micro
    audit_log('redeem_code_used', user_id=user_id, code=code,
              amount_micro=amount)
    return api_ok(
        wallet=_wallet_snapshot_payload(redemption.wallet),
        redeemed={'code': code, 'amount_micro': amount,
                   'amount_credits': _cost.micro_to_credits(amount)},
    )


# ── Admin: manual deposit / debit ──────────────────────────────────

@api_v1_billing_bp.route('/api/v1/billing/deposit', methods=['POST'])
@require_scope('admin')
@api_meta(summary='Admin: deposit credits',
          description='Adds credits to a target user manually (audit-logged). '
                       'Use for refunds, promotional bonuses, etc.',
          tags=['billing'], scope='admin')
async def deposit_route():
    _disabled = _billing_disabled_response()
    if _disabled is not None:
        return _disabled
    body = await async_parse_body()
    user_id = require_str(body, 'user_id', max_len=64)
    amount_micro = require_int(body, 'amount_micro', min=1,
                                max=10_000_000_000_000)
    note = optional_str(body, 'note', default='', max_len=200)
    kind = optional_str(body, 'kind', default='bonus', max_len=20)
    if kind not in ('topup', 'bonus', 'refund', 'adjust_credit'):
        return api_bad_request(f'Bad kind: {kind!r}', field='kind')
    if get_user(user_id) is None:
        return api_not_found('user not found', field='user_id')
    deposit(user_id, amount_micro, kind=kind,
            ref_type='admin', ref_id=short_id(n=24),
            note=note)
    return api_created(_wallet_payload(user_id))


@api_v1_billing_bp.route('/api/v1/billing/debit', methods=['POST'])
@require_scope('admin')
@api_meta(summary='Admin: debit credits',
          description='Subtracts credits manually (audit-logged). Set '
                       '``allow_negative=true`` to push the balance below 0.',
          tags=['billing'], scope='admin')
async def debit_route():
    _disabled = _billing_disabled_response()
    if _disabled is not None:
        return _disabled
    body = await async_parse_body()
    user_id = require_str(body, 'user_id', max_len=64)
    amount_micro = require_int(body, 'amount_micro', min=1,
                                max=10_000_000_000_000)
    note = optional_str(body, 'note', default='', max_len=200)
    allow_negative = bool(body.get('allow_negative'))
    if get_user(user_id) is None:
        return api_not_found('user not found', field='user_id')
    try:
        debit(user_id, amount_micro, kind='adjust_debit',
              ref_type='admin', ref_id=short_id(n=24),
              note=note, allow_negative=allow_negative)
    except InsufficientFunds as e:
        return api_bad_request(
            'insufficient funds (set allow_negative=true to override)',
            error_kind='insufficient_funds',
            balance_micro=e.balance_micro, needed_micro=e.needed_micro)
    return api_created(_wallet_payload(user_id))


# ── Admin: redeem-code minting ─────────────────────────────────────

def _gen_code(prefix: str = 'TOFU', length: int = 16) -> str:
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # no I/O/0/1
    rng = uuid.uuid4().bytes
    return f'{prefix}-' + ''.join(alphabet[b % len(alphabet)] for b in rng[:length])


@api_v1_billing_bp.route('/api/v1/billing/redeem-codes', methods=['POST'])
@require_scope('admin')
@api_meta(summary='Admin: mint redemption codes',
          description='Creates ``count`` single-use codes worth '
                       '``amount_micro`` credits each. Returns the codes '
                       'as plaintext exactly once.',
          tags=['billing'], scope='admin')
async def mint_codes_route():
    _disabled = _billing_disabled_response()
    if _disabled is not None:
        return _disabled
    body = await async_parse_body()
    count = require_int(body, 'count', min=1, max=10_000)
    amount_micro = require_int(body, 'amount_micro', min=1,
                                max=10_000_000_000_000)
    expires_in_days = optional_int(body, 'expires_in_days',
                                     default=0, min=0, max=3650) or 0
    batch = optional_str(body, 'batch', default='',
                          max_len=80) or f'batch_{int(time.time())}'
    note = optional_str(body, 'note', default='', max_len=200)
    ctx = current_auth()
    created_by = (ctx.key_id if ctx else '') or ''
    now = int(time.time())
    expires_at = now + expires_in_days * 86400 if expires_in_days else 0
    codes: list[str] = []
    seen: set[str] = set()
    while len(codes) < count:
        code = _gen_code()
        if code not in seen:
            seen.add(code)
            codes.append(code)
    codes = await asyncio.to_thread(
        mint_redeem_codes,
        codes,
        amount_micro=amount_micro,
        batch=batch,
        created_by=created_by,
        created_at=now,
        expires_at=expires_at,
        note=note,
    )
    audit_log('redeem_codes_minted', batch=batch, count=count,
              amount_micro=amount_micro, by=created_by)
    return api_created(
        codes=codes, batch=batch,
        amount_micro=amount_micro,
        amount_credits=_cost.micro_to_credits(amount_micro),
        expires_at=expires_at,
    )


@api_v1_billing_bp.route('/api/v1/billing/redeem-codes', methods=['GET'])
@require_scope('admin')
@api_meta(summary='Admin: list redemption codes',
          description='Filter by ``?batch=…`` and/or ``?status=`` '
                       '(``unredeemed`` / ``redeemed`` / ``all``).',
          tags=['billing'], scope='admin')
async def list_codes_route():
    batch = (request.args.get('batch') or '').strip()
    status = (request.args.get('status') or 'all').strip().lower()
    if status not in {'all', 'redeemed', 'unredeemed'}:
        return api_bad_request(
            'status must be all, redeemed, or unredeemed', field='status')
    try:
        limit = max(1, min(int(request.args.get('limit') or 100), 1000))
        offset = max(0, int(request.args.get('offset') or 0))
    except (ValueError, TypeError):
        return api_bad_request('limit/offset must be integers', field='limit')
    rows = await asyncio.to_thread(
        list_redeem_codes,
        batch=batch,
        status=status,
        limit=limit,
        offset=offset,
    )
    return api_ok(codes=[{
        'code': row.code,
        'amount_micro': row.amount_micro,
        'batch': row.batch,
        'created_by': row.created_by,
        'created_at': row.created_at,
        'expires_at': row.expires_at,
        'redeemed_by': row.redeemed_by,
        'redeemed_at': row.redeemed_at,
        'note': row.note,
    } for row in rows], limit=limit, offset=offset)


# ── Payment webhooks ────────────────────────────────────────────────

@api_v1_billing_bp.route('/api/v1/billing/webhooks/stripe', methods=['POST'])
@api_meta(summary='Stripe webhook receiver',
          description='Public endpoint that Stripe POSTs ``payment_intent.'
                       'succeeded`` events to. Signature verified against '
                       '``data/config/payments.json:stripe.webhook_secret``. '
                       'Idempotent on the Stripe event id.',
          tags=['billing'], public=True)
async def stripe_webhook_route():
    from lib.billing.payments import handle_stripe_webhook
    payload = await request.get_data() or b''
    sig = request.headers.get('Stripe-Signature', '')
    status, body = handle_stripe_webhook(payload, sig)
    if status >= 400:
        return api_bad_request(body.get('error', 'webhook_failed'),
                                error_kind=body.get('error', 'webhook_failed'))
    return api_ok(body)


@api_v1_billing_bp.route('/api/v1/billing/webhooks/alipay', methods=['POST'])
@api_meta(summary='Alipay async-notify receiver',
          description='Public endpoint that Alipay POSTs '
                       '``alipay.trade.notify`` events to. Returns the '
                       'literal string ``success`` on accept (Alipay '
                       'protocol convention).',
          tags=['billing'], public=True)
async def alipay_notify_route():
    from lib.billing.payments import handle_alipay_notify
    from quart import Response as _Resp
    submitted = await request.form
    form = {k: submitted.get(k, '') for k in submitted.keys()}
    status, text = handle_alipay_notify(form)
    return _Resp(text, status=status, content_type='text/plain; charset=utf-8')


@api_v1_billing_bp.route('/api/v1/billing/checkout', methods=['POST'])
@api_meta(summary='Create a checkout session',
          description='Issue a payment URL for the authenticated user '
                       'to redirect to. Supports ``provider="alipay"`` and '
                       '``provider="stripe"`` (the latter creates a Stripe '
                       'Checkout Session; requires ``stripe.secret_key`` in '
                       'payments.json).',
          tags=['billing'])
async def create_checkout_route():
    _disabled = _billing_disabled_response()
    if _disabled is not None:
        return _disabled
    try:
        user_id = _resolve_target_user()
    except PermissionError as e:
        return api_forbidden(str(e))
    if not user_id:
        return api_bad_request('No wallet for this principal',
                                error_kind='no_wallet')
    body = await async_parse_body()
    provider = require_str(body, 'provider', max_len=20)
    amount_minor = require_int(body, 'amount_minor', min=1, max=10_000_000_000)
    notify_url = optional_str(body, 'notify_url', default='', max_len=500)
    return_url = optional_str(body, 'return_url', default='', max_len=500)
    if provider == 'alipay':
        from lib.billing.payments import create_alipay_order
        if not notify_url:
            host = request.host_url.rstrip('/')
            notify_url = f'{host}/api/v1/billing/webhooks/alipay'
        try:
            out_trade_no, url = create_alipay_order(
                user_id=user_id,
                amount_yuan=amount_minor / 100.0,
                notify_url=notify_url,
                return_url=return_url)
        except RuntimeError as e:
            return api_bad_request(str(e), error_kind='not_configured')
        return api_ok(provider='alipay', redirect_url=url,
                       out_trade_no=out_trade_no)
    if provider == 'stripe':
        from lib.billing.payments import create_stripe_checkout
        currency = optional_str(body, 'currency', default='usd', max_len=8)
        success_url = return_url or (
            request.host_url.rstrip('/') + '/?topup=success')
        cancel_url = optional_str(body, 'cancel_url', default='', max_len=500)
        try:
            session_id, url = await create_stripe_checkout(
                user_id=user_id,
                amount_minor=amount_minor,
                currency=currency,
                success_url=success_url,
                cancel_url=cancel_url)
        except RuntimeError as e:
            return api_bad_request(str(e), error_kind='stripe_checkout_failed')
        return api_ok(provider='stripe', redirect_url=url,
                       session_id=session_id)
    return api_bad_request(f'Unknown provider: {provider!r}',
                            field='provider')


@api_v1_billing_bp.route('/api/v1/billing/payments', methods=['GET'])
@api_meta(summary='List payments',
          description='Self-only by default; admins may pass '
                       '``?user_id=…`` to inspect any user.',
          tags=['billing'])
async def list_payments_route():
    try:
        user_id = _resolve_target_user()
    except PermissionError as e:
        return api_forbidden(str(e))
    from lib.billing.payments import list_payments
    try:
        limit = max(1, min(int(request.args.get('limit') or 50), 500))
        offset = max(0, int(request.args.get('offset') or 0))
    except (ValueError, TypeError):
        return api_bad_request('limit/offset must be integers', field='limit')
    status = (request.args.get('status') or '').strip()
    rows = list_payments(user_id=user_id, status=status,
                          limit=limit, offset=offset)
    return api_ok(payments=[{
        'id': r.id, 'user_id': r.user_id,
        'provider': r.provider, 'provider_id': r.provider_id,
        'amount_minor': r.amount_minor, 'currency': r.currency,
        'credit_micro': r.credit_micro,
        'credit_credits': _cost.micro_to_credits(r.credit_micro),
        'status': r.status,
        'created_at': r.created_at, 'settled_at': r.settled_at,
    } for r in rows], limit=limit, offset=offset)


__all__ = ['api_v1_billing_bp']
