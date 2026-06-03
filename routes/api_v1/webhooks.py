"""routes/api_v1/webhooks.py — Outbound event delivery.

Lets a serverless caller subscribe a URL to a (channel, task_id?) and
receive HMAC-signed POSTs whenever events fire on the underlying
``PushHub``. Mirror of the WebSocket ``/api/push`` contract for
clients that prefer pull-via-callback.

Storage: ``data/config/webhooks.json`` via ``lib.json_store``. The
backing worker thread is started lazily on first registration.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import secrets
import threading
import time
from queue import Empty, Queue

from flask import Blueprint

from lib.api_response import api_bad_request, api_created, api_not_found, api_ok
from lib.config_dir import config_path
from lib.http_client import http_post
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
    return out


# ── Worker ─────────────────────────────────────────────────────────

def _ensure_worker_started() -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
        threading.Thread(target=_worker_loop,
                          name='webhook-worker', daemon=True).start()
        # Hook the PushHub once: every event the hub processes is fanned
        # out to ``_on_push_event``, which enqueues a delivery for each
        # matching subscription. Listener exceptions are isolated by the
        # hub itself, so a delivery bug can never break in-browser push.
        from lib.push import hub
        hub.add_listener(_on_push_event)


def _on_push_event(channel: str, task_id: str, payload: dict) -> None:
    """Fan-out: enqueue a delivery for every matching subscription."""
    subs = _load()
    if not subs:
        return
    now = time.time()
    for sub in subs:
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


def _deliver(item: dict) -> bool:
    sub = item['sub']
    url = sub.get('url')
    secret = sub.get('secret') or ''
    body = json.dumps({
        'channel': item['channel'],
        'task_id': item['task_id'],
        'event': item['payload'],
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
        resp = http_post(url, data=body.encode('utf-8'), headers=headers,
                         timeout=15)
        if 200 <= resp.status_code < 300:
            return True
        logger.warning('[Webhooks] delivery %s %s → %d',
                       sub.get('id', ''), url, resp.status_code)
        return False
    except Exception as e:
        logger.warning('[Webhooks] delivery %s %s failed: %s',
                       sub.get('id', ''), url, e)
        return False


def _worker_loop():
    logger.info('[Webhooks] worker started')
    while True:
        try:
            item = _QUEUE.get(timeout=5)
        except Empty:
            continue
        try:
            ok = _deliver(item)
            if not ok:
                item['attempt'] = item.get('attempt', 0) + 1
                if item['attempt'] < 5:
                    backoff = min(60, 2 ** item['attempt'])
                    threading.Timer(backoff,
                                     lambda: _QUEUE.put(item)).start()
                else:
                    logger.warning('[Webhooks] giving up on %s after %d '
                                   'attempts',
                                   item['sub'].get('id', ''), item['attempt'])
        except Exception as e:
            logger.error('[Webhooks] worker cycle failed: %s', e,
                         exc_info=True)


# ── Routes ─────────────────────────────────────────────────────────

@api_v1_webhooks_bp.route('/api/v1/webhooks', methods=['GET'])
@require_scope('webhooks')
@api_meta(summary='List webhook subscriptions', tags=['webhooks'],
          scope='webhooks')
def list_subs():
    return api_ok(subs=[_public(s) for s in _load()])


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
    if not url.startswith(('http://', 'https://')):
        return api_bad_request('url must be http(s)://', field='url')
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
    new_subs = [s for s in subs if s.get('id') != sub_id]
    if len(new_subs) == len(subs):
        return api_not_found('Subscription not found')
    _save(new_subs)
    audit_log('webhook_deleted', subscription_id=sub_id,
              by=(current_auth().key_id if current_auth() else ''))
    return api_ok({'deleted': sub_id})


__all__ = ['api_v1_webhooks_bp']
