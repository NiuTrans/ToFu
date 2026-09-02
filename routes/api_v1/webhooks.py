"""routes/api_v1/webhooks.py — Outbound event delivery.

Lets a serverless caller subscribe a URL to a (channel, task_id?) and
receive HMAC-signed POSTs whenever events fire on the underlying
``PushHub``. Mirror of the WebSocket ``/api/push`` contract for
clients that prefer pull-via-callback.

Storage: ``data/config/webhooks.json`` via ``lib.json_store``. The
backing worker thread is started lazily on first registration.
"""

from __future__ import annotations

import atexit
import hmac
import hashlib
import heapq
import itertools
import json
import secrets
import threading
import time
from queue import Empty, Queue

from quart import Blueprint

from lib.api_response import api_bad_request, api_created, api_not_found, api_ok
from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import (
    BadRequest, optional_list, optional_str, parse_body, require_str,
)

from .auth import current_auth, require_scope

logger = get_logger(__name__)

api_v1_webhooks_bp = Blueprint('api_v1_webhooks', __name__)

_STORE = config_path('webhooks.json')
_QUEUE: Queue = Queue(maxsize=10_000)
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_WORKER_THREAD = None
_WORKER_STOP = threading.Event()
_MAX_DELAYED_RETRIES = 10_000


class _WebhookUrlError(ValueError):
    """A webhook URL failed the public-egress policy."""


def http_post(*args, **kwargs):
    """Request-loaded HTTP seam retained for focused delivery tests."""
    from lib.http_client import http_post as _http_post

    return _http_post(*args, **kwargs)


# ── Persistence ────────────────────────────────────────────────────

def _load() -> list:
    data = read_json(_STORE, default={'version': 1, 'subs': []})
    if isinstance(data, dict) and isinstance(data.get('subs'), list):
        return [s for s in data['subs'] if isinstance(s, dict)]
    return []


def _save(subs: list) -> None:
    update_json_atomic(_STORE, lambda _: {'version': 1, 'subs': subs},
                       default={'version': 1, 'subs': []})


def _public(sub: dict) -> dict:
    out = dict(sub)
    out.pop('secret', None)
    out.pop('owner_user_id', None)
    return out


def _current_owner_user_id() -> str:
    context = current_auth()
    return str(context.owner_user_id or '') if context else ''


# ── Worker ─────────────────────────────────────────────────────────

def _ensure_worker_started() -> None:
    global _WORKER_STARTED, _WORKER_THREAD
    if (_WORKER_STARTED and _WORKER_THREAD is not None
            and _WORKER_THREAD.is_alive()):
        return
    with _WORKER_LOCK:
        if (_WORKER_STARTED and _WORKER_THREAD is not None
                and _WORKER_THREAD.is_alive()):
            return
        _WORKER_STOP.clear()
        _WORKER_STARTED = True
        _WORKER_THREAD = threading.Thread(
            target=_worker_loop, name='webhook-worker', daemon=True)
        _WORKER_THREAD.start()
        # Hook the PushHub once: every event the hub processes is fanned
        # out to ``_on_push_event``, which enqueues a delivery for each
        # matching subscription. Listener exceptions are isolated by the
        # hub itself, so a delivery bug can never break in-browser push.
        from lib.agent_core.push import hub
        hub.add_listener(_on_push_event)


def stop_webhook_worker(timeout: float = 2.0) -> bool:
    """Stop the delivery daemon without making process shutdown unbounded."""
    global _WORKER_STARTED
    _WORKER_STOP.set()
    try:
        _QUEUE.put_nowait(None)
    except Exception as exc:
        logger.debug('[Webhooks] worker wake-up skipped during shutdown: %s', exc)
    thread = _WORKER_THREAD
    if thread is None or thread is threading.current_thread():
        _WORKER_STARTED = False
        return True
    try:
        wait_s = max(0.0, float(timeout))
    except (TypeError, ValueError):
        logger.debug('[Webhooks] invalid worker shutdown timeout %r; using 2s',
                     timeout)
        wait_s = 2.0
    thread.join(wait_s)
    stopped = not thread.is_alive()
    if stopped:
        _WORKER_STARTED = False
    else:
        logger.warning('[Webhooks] worker did not stop within %.1fs', wait_s)
    return stopped


atexit.register(stop_webhook_worker)


def _on_push_event(channel: str, task_id: str, payload: dict) -> None:
    """Fan-out: enqueue a delivery for every matching subscription."""
    subs = _load()
    if not subs:
        return
    now = time.time()
    event_owner = str(payload.get('_ownerUserId') or '')
    for sub in subs:
        subscription_owner = str(sub.get('owner_user_id') or '')
        if not subscription_owner:
            continue
        if event_owner and subscription_owner != event_owner:
            continue
        if sub.get('disabled'):
            continue
        if sub.get('channel') and sub['channel'] != channel:
            continue
        if sub.get('task_id') and sub['task_id'] not in ('*', task_id):
            continue
        types = sub.get('event_types') or []
        if types and payload.get('type') not in types:
            continue
        try:
            _QUEUE.put_nowait({
                'sub': sub, 'channel': channel, 'task_id': task_id,
                'payload': payload, 'ts': now, 'attempt': 0,
            })
        except Exception as e:
            logger.warning('[Webhooks] queue full, dropping: %s', e)


def _sign(secret: str, body: str, ts: str) -> str:
    msg = f'{ts}.{body}'.encode('utf-8')
    return hmac.new(secret.encode('utf-8'), msg, hashlib.sha256).hexdigest()


def _validate_webhook_url(url: str, *, allow_unresolved: bool = False) -> None:
    """Webhooks are public egress unless the operator names an exception."""
    from lib.safe_fetch import SafeFetchError, validate_public_url

    try:
        validate_public_url(
            url, allow_hosts_env='TOFU_WEBHOOK_ALLOW_HOSTS',
            allow_unresolved=allow_unresolved)
    except SafeFetchError as exc:
        raise _WebhookUrlError(str(exc)) from exc


def _deliver(item: dict) -> bool:
    sub = item['sub']
    url = sub.get('url')
    secret = sub.get('secret') or ''
    public_payload = {
        key: value
        for key, value in item['payload'].items()
        if key != '_ownerUserId'
    }
    body = json.dumps({
        'channel': item['channel'],
        'task_id': item['task_id'],
        'event': public_payload,
        'ts': item['ts'],
    }, ensure_ascii=False)
    ts = str(int(item['ts']))
    sig = _sign(secret, body, ts) if secret else ''
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Tofu-Webhooks/1.0',
        'X-Tofu-Timestamp': ts,
        'X-Tofu-Signature': f'v1={sig}' if sig else '',
        'X-Tofu-Subscription-Id': sub.get('id', ''),
    }
    try:
        # Re-check at delivery time: a hostname can change after registration.
        # Redirects are intentionally disabled; a webhook endpoint is expected
        # to accept this exact POST URL, and following a 30x would create a
        # second SSRF hop as well as potentially changing POST into GET.
        _validate_webhook_url(url)
        resp = http_post(url, data=body.encode('utf-8'), headers=headers,
                         timeout=15, allow_redirects=False)
        item['_last_status'] = int(resp.status_code)
        if 200 <= resp.status_code < 300:
            return True
        logger.warning('[Webhooks] delivery %s %s → %d',
                       sub.get('id', ''), url, resp.status_code)
        return False
    except Exception as e:
        item['_last_status'] = None
        logger.warning('[Webhooks] delivery %s %s failed: %s',
                       sub.get('id', ''), url, e)
        return False


def _delivery_is_retryable(item: dict) -> bool:
    """Retry transient transport/HTTP failures, never permanent client errors."""
    status = item.get('_last_status')
    if status is None:
        return True
    return status in (408, 409, 425, 429) or status >= 500


def _subscription_is_active(sub: dict) -> bool:
    """False once a queued subscription has been deleted or disabled."""
    sub_id = sub.get('id')
    if not sub_id:
        return False
    return any(
        current.get('id') == sub_id and not current.get('disabled')
        for current in _load()
    )


def _schedule_retry(delayed: list, sequence, item: dict, *, now=None) -> bool:
    """Put one failed delivery on the bounded in-thread delay heap."""
    item['attempt'] = item.get('attempt', 0) + 1
    if item['attempt'] >= 5 or len(delayed) >= _MAX_DELAYED_RETRIES:
        return False
    backoff = min(60, 2 ** item['attempt'])
    heapq.heappush(
        delayed,
        ((time.monotonic() if now is None else now) + backoff,
         next(sequence), item),
    )
    return True


def _worker_loop():
    logger.info('[Webhooks] worker started')
    # Delayed retries live in this one worker, not one Timer thread per event.
    # Entries are (monotonic ready time, stable sequence, delivery item).
    delayed: list[tuple[float, int, dict]] = []
    sequence = itertools.count()
    while not _WORKER_STOP.is_set():
        item = None
        from_immediate_queue = False
        now = time.monotonic()
        if delayed and delayed[0][0] <= now:
            _ready_at, _seq, item = heapq.heappop(delayed)
        timeout = 5.0
        if item is None and delayed:
            timeout = min(timeout, max(0.0, delayed[0][0] - now))
        try:
            if item is None:
                item = _QUEUE.get(timeout=timeout)
                from_immediate_queue = True
        except Empty:
            continue
        try:
            if item is None or _WORKER_STOP.is_set():
                continue
            # Deleting/disabling a subscription revokes work that was already
            # queued, including delayed retries. Otherwise a typo'd endpoint
            # keeps generating DNS/network traffic after the user removes it.
            if not _subscription_is_active(item.get('sub') or {}):
                continue
            ok = _deliver(item)
            if not ok and _delivery_is_retryable(item):
                if not _schedule_retry(delayed, sequence, item):
                    logger.warning('[Webhooks] giving up/drop on %s after %d '
                                   'attempts (delayed=%d)',
                                   item['sub'].get('id', ''),
                                   item.get('attempt', 0), len(delayed))
            elif not ok:
                logger.warning('[Webhooks] permanent failure for %s (HTTP %s), '
                               'not retrying', item['sub'].get('id', ''),
                               item.get('_last_status'))
        except Exception as e:
            logger.error('[Webhooks] worker cycle failed: %s', e,
                         exc_info=True)
        finally:
            if from_immediate_queue:
                try:
                    _QUEUE.task_done()
                except (AttributeError, ValueError) as exc:
                    logger.debug('[Webhooks] queue task_done skipped: %s', exc)


# ── Routes ─────────────────────────────────────────────────────────

@api_v1_webhooks_bp.route('/api/v1/webhooks', methods=['GET'])
@require_scope('webhooks')
@api_meta(summary='List webhook subscriptions', tags=['webhooks'],
          scope='webhooks')
def list_subs():
    owner_user_id = _current_owner_user_id()
    return api_ok(
        subs=[
            _public(subscription)
            for subscription in _load()
            if str(subscription.get('owner_user_id') or '') == owner_user_id
        ]
    )


@api_v1_webhooks_bp.route('/api/v1/webhooks', methods=['POST'])
@require_scope('webhooks')
@api_meta(summary='Subscribe a URL to event delivery',
          tags=['webhooks'], scope='webhooks')
def create_sub():
    body = parse_body()
    try:
        url = require_str(body, 'url', max_len=2000)
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field or 'url')
    try:
        # Saving a subscription must work on an offline fresh install. A
        # currently resolvable private target is still rejected here; a DNS
        # outage is merely deferred because _deliver() revalidates immediately
        # before every network request and follows no redirects.
        _validate_webhook_url(url, allow_unresolved=True)
    except _WebhookUrlError as e:
        return api_bad_request(str(e), field='url')
    channel = optional_str(body, 'channel', default='', max_len=80)
    task_id = optional_str(body, 'task_id', default='*', max_len=200)
    event_types = optional_list(body, 'event_types',
                                  item_type=str, default=[]) or []
    sub = {
        'id': 'wh_' + secrets.token_hex(4),
        'url': url,
        'channel': channel,
        'task_id': task_id,
        'event_types': event_types,
        'secret': secrets.token_hex(32),
        'created_at': time.time(),
        'created_by': (current_auth().key_id if current_auth() else ''),
        'owner_user_id': _current_owner_user_id(),
        'disabled': False,
    }
    subs = _load()
    subs.append(sub)
    _save(subs)
    _ensure_worker_started()
    audit_log('webhook_subscribed', subscription_id=sub['id'],
              url=url, channel=channel)
    out = _public(sub)
    out['secret'] = sub['secret']  # shown ONCE on creation
    return api_created(subscription=out)


@api_v1_webhooks_bp.route('/api/v1/webhooks/<sub_id>', methods=['DELETE'])
@require_scope('webhooks')
@api_meta(summary='Delete a webhook subscription', tags=['webhooks'],
          scope='webhooks')
def delete_sub(sub_id):
    subs = _load()
    owner_user_id = _current_owner_user_id()
    new_subs = [
        subscription
        for subscription in subs
        if not (
            subscription.get('id') == sub_id
            and str(subscription.get('owner_user_id') or '') == owner_user_id
        )
    ]
    if len(new_subs) == len(subs):
        return api_not_found('Subscription not found')
    _save(new_subs)
    audit_log('webhook_deleted', subscription_id=sub_id,
              by=(current_auth().key_id if current_auth() else ''))
    return api_ok({'deleted': sub_id})


__all__ = ['api_v1_webhooks_bp']
