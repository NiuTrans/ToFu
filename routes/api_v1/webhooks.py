"""routes/api_v1/webhooks.py — Outbound event delivery.

Lets a serverless caller subscribe a URL to a (channel, task_id?) and
receive HMAC-signed POSTs whenever events fire on the underlying
``PushHub``. Mirror of the WebSocket ``/api/push`` contract for
clients that prefer pull-via-callback.

Storage: ``data/config/webhooks.json`` via ``lib.json_store``. The
backing worker thread is started lazily on first registration.
"""

from __future__ import annotations
from queue import Empty as Empty

import atexit
from concurrent.futures import CancelledError as FrameCancelled
import hmac
import hashlib
import heapq
import itertools
import json
import secrets
import threading
import time
from queue import Queue

from quart import Blueprint

from lib.api_response import (
    api_bad_request,
    api_created,
    api_not_found,
    api_ok,
)
from lib.browser.log_safety import text_for_log, url_for_log
from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import (
    BadRequest, optional_list, optional_str, parse_body, require_str,
)
from lib.webhook_policy import resolve_webhook_budget
from tofu_workers.json_frames import JsonFrameStore

from .auth import current_auth, require_scope

logger = get_logger(__name__)

api_v1_webhooks_bp = Blueprint('api_v1_webhooks', __name__)

_STORE = config_path('webhooks.json')
_WEBHOOK_BUDGET = resolve_webhook_budget()


def _item_retained_bytes(item) -> int:
    if not isinstance(item, dict):
        return 0
    try:
        return max(0, int(item.get('_retained_bytes') or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


class _DeliveryQueue(Queue):
    """FIFO references to shared file-backed event bodies."""

    def __init__(self) -> None:
        super().__init__()
        self._retained_bytes = 0

    def _put(self, item) -> None:
        retained_bytes = _item_retained_bytes(item)
        super()._put(item)
        self._retained_bytes += retained_bytes

    def _get(self):
        item = super()._get()
        self._retained_bytes = max(
            0, self._retained_bytes - _item_retained_bytes(item))
        return item

    @property
    def retained_bytes(self) -> int:
        with self.mutex:
            return self._retained_bytes


class _RetryHeap:
    """One worker-owned delayed heap retaining every retry reference."""

    def __init__(self) -> None:
        self._heap: list[tuple[float, int, dict]] = []
        self._sequence = itertools.count()
        self.retained_bytes = 0

    def __len__(self) -> int:
        return len(self._heap)

    def next_ready_at(self) -> float | None:
        return self._heap[0][0] if self._heap else None

    def pop_ready(self, now: float) -> dict | None:
        if not self._heap or self._heap[0][0] > now:
            return None
        _ready_at, _sequence, item = heapq.heappop(self._heap)
        self.retained_bytes = max(
            0, self.retained_bytes - _item_retained_bytes(item))
        return item

    def schedule(
        self,
        item: dict,
        *,
        now: float,
        ready_at: float | None = None,
        increment_attempt: bool = True,
    ) -> bool:
        if increment_attempt:
            item['attempt'] = int(item.get('attempt') or 0) + 1
        retained_bytes = _item_retained_bytes(item)
        backoff = min(60, 2 ** min(6, int(item.get('attempt') or 0)))
        heapq.heappush(
            self._heap,
            (
                max(now, ready_at) if ready_at is not None else now + backoff,
                next(self._sequence),
                item,
            ),
        )
        self.retained_bytes += retained_bytes
        return True


_QUEUE: Queue = _DeliveryQueue()
_FRAME_STORE: JsonFrameStore | None = None
_FRAME_STORE_LOCK = threading.Lock()
_LISTENER_REGISTERED = False


def _event_frame_store() -> JsonFrameStore:
    global _FRAME_STORE
    with _FRAME_STORE_LOCK:
        if _FRAME_STORE is None:
            _FRAME_STORE = JsonFrameStore(
                segment_bytes=_WEBHOOK_BUDGET.spool_segment_bytes,
                reuse_workers=_WEBHOOK_BUDGET.worker_reuse_target,
                thread_name_prefix='tofu-webhook-frame')
        return _FRAME_STORE


_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_WORKER_THREAD = None
_WORKER_STOP = threading.Event()
_SUBSCRIPTION_CACHE_LOCK = threading.RLock()
_SUBSCRIPTION_CACHE: tuple[dict, ...] = ()
_SUBSCRIPTION_CACHE_EXPIRES_AT = 0.0
_SUBSCRIPTION_CACHE_STORE = ''
_DROP_LOCK = threading.Lock()
_DROP_COUNTS: dict[str, int] = {}


class _WebhookUrlError(ValueError):
    """A webhook URL failed the public-egress policy."""


def http_post(*args, **kwargs):
    """Request-loaded HTTP seam retained for focused delivery tests."""
    from lib.http_transport.client import http_post as _http_post

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
    _publish_subscription_cache(subs)


def _stored_subscriptions(current) -> list[dict]:
    if not isinstance(current, dict) or not isinstance(current.get('subs'), list):
        return []
    return [item for item in current['subs'] if isinstance(item, dict)]


def _append_subscription(sub: dict) -> None:
    """Persist a complete owner-bound subscription through the atomic store."""
    def mutate(current):
        subscriptions = _stored_subscriptions(current)
        subscriptions.append(dict(sub))
        return {'version': 1, 'subs': subscriptions}

    persisted = update_json_atomic(
        _STORE, mutate, default={'version': 1, 'subs': []})
    if isinstance(persisted, dict):
        _publish_subscription_cache(_stored_subscriptions(persisted))


def _delete_subscription(sub_id: str, owner_user_id: str) -> bool:
    """Atomically delete exactly one owner's subscription."""
    deleted = False

    def mutate(current):
        nonlocal deleted
        subscriptions = _stored_subscriptions(current)
        retained = [
            subscription
            for subscription in subscriptions
            if not (
                subscription.get('id') == sub_id
                and str(subscription.get('owner_user_id') or '')
                == owner_user_id
            )
        ]
        deleted = len(retained) != len(subscriptions)
        return {'version': 1, 'subs': retained} if deleted else None

    persisted = update_json_atomic(
        _STORE,
        mutate,
        default={'version': 1, 'subs': []},
    )
    if isinstance(persisted, dict):
        _publish_subscription_cache(_stored_subscriptions(persisted))
    return deleted


def _delivery_subscription(raw: dict) -> dict | None:
    """Project one stored row into a bounded secret-bearing delivery record."""
    sub_id = raw.get('id')
    url = raw.get('url')
    secret = raw.get('secret') or ''
    owner_user_id = str(raw.get('owner_user_id') or '')
    channel = raw.get('channel') or ''
    task_id = raw.get('task_id') or '*'
    event_types = raw.get('event_types') or []
    if (
        not isinstance(sub_id, str)
        or not sub_id
        or len(sub_id) > 80
        or not isinstance(url, str)
        or not url
        or len(url) > 2_000
        or not owner_user_id
        or len(owner_user_id) > 128
        or not isinstance(secret, str)
        or len(secret) > 128
        or not isinstance(channel, str)
        or len(channel) > 80
        or not isinstance(task_id, str)
        or len(task_id) > 200
        or not isinstance(event_types, list)
        or len(event_types) > 32
        or any(
            not isinstance(event_type, str)
            or not event_type
            or len(event_type) > 80
            for event_type in event_types
        )
    ):
        return None
    return {
        'id': sub_id,
        'url': url,
        'secret': secret,
        'owner_user_id': owner_user_id,
        'channel': channel,
        'task_id': task_id,
        'event_types': list(event_types),
        'disabled': bool(raw.get('disabled')),
    }


def _record_drop(reason: str) -> int:
    """Count genuine transport/invalid-input failures at sparse checkpoints."""
    with _DROP_LOCK:
        count = _DROP_COUNTS.get(reason, 0) + 1
        _DROP_COUNTS[reason] = count
    if count == 1 or count & (count - 1) == 0:
        logger.warning(
            '[Webhooks] failed work reason=%s count=%d queue=%d metadata_bytes=%d',
            reason,
            count,
            _QUEUE.qsize() if hasattr(_QUEUE, 'qsize') else -1,
            int(getattr(_QUEUE, 'retained_bytes', -1)),
        )
    return count


def _build_subscription_snapshot(subs: list) -> tuple[dict, ...]:
    projected = []
    invalid = 0
    for raw in subs:
        delivery_sub = _delivery_subscription(raw)
        if delivery_sub is None:
            invalid += 1
            continue
        projected.append(delivery_sub)
    if invalid:
        _record_drop('invalid_subscription')
    return tuple(projected)


def _publish_subscription_cache(subs: list) -> tuple[dict, ...]:
    global _SUBSCRIPTION_CACHE
    global _SUBSCRIPTION_CACHE_EXPIRES_AT
    global _SUBSCRIPTION_CACHE_STORE
    snapshot = _build_subscription_snapshot(subs)
    with _SUBSCRIPTION_CACHE_LOCK:
        _SUBSCRIPTION_CACHE = snapshot
        _SUBSCRIPTION_CACHE_EXPIRES_AT = (
            time.monotonic() + _WEBHOOK_BUDGET.subscription_cache_seconds)
        _SUBSCRIPTION_CACHE_STORE = str(_STORE)
    return snapshot


def _subscription_snapshot() -> tuple[dict, ...]:
    """Return a short-lived snapshot; local writes publish it immediately."""
    global _SUBSCRIPTION_CACHE
    global _SUBSCRIPTION_CACHE_EXPIRES_AT
    global _SUBSCRIPTION_CACHE_STORE
    now = time.monotonic()
    with _SUBSCRIPTION_CACHE_LOCK:
        if (
            _SUBSCRIPTION_CACHE_STORE == str(_STORE)
            and now < _SUBSCRIPTION_CACHE_EXPIRES_AT
        ):
            return _SUBSCRIPTION_CACHE
        snapshot = _build_subscription_snapshot(_load())
        _SUBSCRIPTION_CACHE = snapshot
        _SUBSCRIPTION_CACHE_EXPIRES_AT = (
            time.monotonic() + _WEBHOOK_BUDGET.subscription_cache_seconds)
        _SUBSCRIPTION_CACHE_STORE = str(_STORE)
        return snapshot


def _reset_subscription_cache() -> None:
    """Clear reconstructible state for tests and explicit lifecycle resets."""
    global _SUBSCRIPTION_CACHE
    global _SUBSCRIPTION_CACHE_EXPIRES_AT
    global _SUBSCRIPTION_CACHE_STORE
    with _SUBSCRIPTION_CACHE_LOCK:
        _SUBSCRIPTION_CACHE = ()
        _SUBSCRIPTION_CACHE_EXPIRES_AT = 0.0
        _SUBSCRIPTION_CACHE_STORE = ''


def webhook_runtime_snapshot() -> dict:
    """Return secret-free evidence for tests, diagnostics, and support."""
    with _DROP_LOCK:
        drops = dict(_DROP_COUNTS)
    return {
        'deliveryAdmission': 'demand',
        'queueDepth': _QUEUE.qsize() if hasattr(_QUEUE, 'qsize') else -1,
        'queueRetainedMetadataBytes': int(getattr(_QUEUE, 'retained_bytes', -1)),
        'subscriptionAdmission': 'demand',
        'drops': drops,
        **(_FRAME_STORE.snapshot() if _FRAME_STORE is not None else {
            'event_spool_files': 0, 'event_spool_bytes': 0}),
    }


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
    global _WORKER_STARTED, _WORKER_THREAD, _FRAME_STORE, _LISTENER_REGISTERED
    if (_WORKER_STARTED and _WORKER_THREAD is not None
            and _WORKER_THREAD.is_alive()):
        return
    with _WORKER_LOCK:
        if (_WORKER_STARTED and _WORKER_THREAD is not None
                and _WORKER_THREAD.is_alive()):
            return
        if _FRAME_STORE is not None and _FRAME_STORE.is_closed:
            if not _FRAME_STORE.close_with_timeout(0):
                raise RuntimeError('Webhook frame owner is still closing')
            _FRAME_STORE = None
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            raise RuntimeError('Webhook worker startup/close has not settled')
        _WORKER_STOP.clear()
        ready, cancelled = threading.Event(), threading.Event()
        def activated():
            ready.wait()
            if not cancelled.is_set():
                _worker_loop()
        _WORKER_THREAD = threading.Thread(target=activated, name='webhook-worker', daemon=True)
        from lib.agent_core.push import hub
        try:
            _WORKER_THREAD.start()
            hub.add_listener(_on_push_event)
            _LISTENER_REGISTERED = True
            _WORKER_STARTED = True
        except BaseException:
            cancelled.set()
            _WORKER_STOP.set()
            hub.remove_listener(_on_push_event)
            _LISTENER_REGISTERED = False
            _WORKER_STARTED = False
            raise
        finally:
            ready.set()


def stop_webhook_worker(timeout: float = 2.0) -> bool:
    """Observe exact send/encoding owners through one bounded shutdown window."""
    global _WORKER_STARTED, _FRAME_STORE, _LISTENER_REGISTERED
    try:
        wait_s = max(0.0, float(timeout))
    except (TypeError, ValueError):
        wait_s = 2.0
    deadline = time.monotonic() + wait_s
    _WORKER_STOP.set()
    if _LISTENER_REGISTERED:
        from lib.agent_core.push import hub
        hub.remove_listener(_on_push_event)
        _LISTENER_REGISTERED = False
    try:
        _QUEUE.put_nowait(None)
    except Exception as exc:
        logger.debug('[Webhooks] worker wake-up skipped during shutdown: %s', exc)
    thread = _WORKER_THREAD
    if (thread is not None and thread is not threading.current_thread()
            and (thread.ident is not None or thread.is_alive())):
        thread.join(max(0.0, deadline - time.monotonic()))
    sender_stopped = thread is None or not thread.is_alive()
    frames_stopped = (_FRAME_STORE is None or _FRAME_STORE.close_with_timeout(
        max(0.0, deadline - time.monotonic())))
    stopped = sender_stopped and frames_stopped
    if stopped:
        _WORKER_STARTED = False
        _FRAME_STORE = None
    else:
        logger.warning('[Webhooks] delivery/encoding owners still live after %.1fs', wait_s)
    return stopped


atexit.register(stop_webhook_worker)


def _on_push_event(channel: str, task_id: str, payload: dict) -> None:
    """Fan-out: enqueue a delivery for every matching subscription."""
    if _WORKER_STOP.is_set():
        return
    subs = _subscription_snapshot()
    if not subs:
        return
    now = time.time()
    event_owner = str(payload.get('_ownerUserId') or '')
    matching_subscriptions = []
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
        matching_subscriptions.append(sub)
    if not matching_subscriptions:
        return
    public_payload = {
        key: value
        for key, value in payload.items()
        if key != '_ownerUserId'
    }
    prepared = _event_frame_store().prepare(public_payload)
    for sub in matching_subscriptions:
        retained_bytes = (
            1_024
            + len(str(channel).encode('utf-8'))
            + len(str(task_id).encode('utf-8'))
            + len(str(sub.get('url') or '').encode('utf-8'))
            + len(str(sub.get('secret') or '').encode('utf-8'))
        )
        try:
            _QUEUE.put_nowait({
                'sub': sub, 'channel': channel, 'task_id': task_id,
                '_event_frame': prepared,
                'ts': now,
                'attempt': 0,
                '_retained_bytes': retained_bytes,
            })
        except Exception:
            # This callback executes synchronously on the push publisher.  It
            # must remain fail-soft even for injected/test queue adapters.
            _record_drop('delivery_queue_failure')


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
    event_json = item.get('event_json')
    try:
        prepared = item.get('_event_frame')
        if prepared is not None:
            # This function runs on the delivery worker. All publisher-side
            # references share one encoded file range, including retries.
            event_json = prepared.result().text()
        if not isinstance(event_json, str):
            public_payload = {
                key: value
                for key, value in item['payload'].items()
                if key != '_ownerUserId'
            }
            event_json = json.dumps(
                public_payload,
                ensure_ascii=False,
                separators=(',', ':'),
            )
        body = ''.join((
            '{"channel":',
            json.dumps(item['channel'], ensure_ascii=False),
            ',"task_id":',
            json.dumps(item['task_id'], ensure_ascii=False),
            ',"event":',
            event_json,
            ',"ts":',
            json.dumps(item['ts'], ensure_ascii=False),
            '}',
        ))
    except (OSError, RuntimeError, FrameCancelled):
        item['_last_status'] = None
        item['_last_failure_kind'] = 'event_storage_failure'
        _record_drop('event_storage_failure')
        return False
    except (KeyError, TypeError, ValueError, OverflowError):
        item['_last_status'] = 400
        _record_drop('invalid_delivery_envelope')
        return False
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
                       sub.get('id', ''), url_for_log(url), resp.status_code)
        return False
    except Exception as e:
        item['_last_status'] = None
        logger.warning('[Webhooks] delivery %s %s failed: %s',
                       sub.get('id', ''), url_for_log(url), text_for_log(e))
        return False


def _delivery_is_retryable(item: dict) -> bool:
    """Retry transient transport/HTTP failures, never permanent client errors."""
    if item.get('_last_failure_kind') == 'event_storage_failure':
        return False
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
        current.get('id') == sub_id
        and str(current.get('owner_user_id') or '') == str(sub.get('owner_user_id') or '')
        and not current.get('disabled')
        for current in _load()
    )


def _schedule_retry(
    delayed: _RetryHeap,
    item: dict,
    *,
    now=None,
    ready_at: float | None = None,
) -> bool:
    """Retain one failed delivery on the worker-owned delayed heap."""
    scheduled = delayed.schedule(
        item,
        now=time.monotonic() if now is None else now,
        ready_at=ready_at,
    )
    return scheduled


def _worker_loop():
    """Coordinate subscriptions while demand workers own independent HTTP I/O."""
    from lib.webhook_delivery import DeliveryCoordinator
    def failed(item, error):
        item['_last_status'] = None
        item['_last_failure_kind'] = 'delivery_worker_failure'
        logger.warning('[Webhooks] delivery worker failed: %s', text_for_log(error))
    store = _event_frame_store()
    DeliveryCoordinator(
        queue=_QUEUE, stop=_WORKER_STOP, submit=store.submit_transport,
        is_active=_subscription_is_active, deliver=_deliver,
        retryable=_delivery_is_retryable, delayed=_RetryHeap(),
        schedule_retry=_schedule_retry, clock=time.monotonic,
        on_error=failed,
    ).run()


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
    body = parse_body(exact=('channel', 'event_types', 'task_id', 'url',))
    try:
        url = require_str(body, 'url', max_len=2000)
        channel = optional_str(body, 'channel', default='', max_len=80)
        task_id = optional_str(body, 'task_id', default='*', max_len=200)
        event_types = optional_list(
            body,
            'event_types',
            item_type=str,
            max_len=32,
            default=[],
        ) or []
        normalized_event_types = []
        for event_type in event_types:
            normalized_event_type = event_type.strip()
            if not normalized_event_type:
                raise BadRequest(
                    'event_types entries must not be empty',
                    field='event_types',
                )
            if len(normalized_event_type) > 80:
                raise BadRequest(
                    'event_types entry too long (max 80 chars)',
                    field='event_types',
                )
            if normalized_event_type not in normalized_event_types:
                normalized_event_types.append(normalized_event_type)
        event_types = normalized_event_types
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
    owner_user_id = _current_owner_user_id()
    sub = {
        'id': 'wh_' + secrets.token_hex(16),
        'url': url,
        'channel': channel,
        'task_id': task_id,
        'event_types': event_types,
        'secret': secrets.token_hex(32),
        'created_at': time.time(),
        'created_by': (current_auth().key_id if current_auth() else ''),
        'owner_user_id': owner_user_id,
        'disabled': False,
    }
    _append_subscription(sub)
    _ensure_worker_started()
    audit_log('webhook_subscribed', subscription_id=sub['id'],
              url=url_for_log(url), channel=channel)
    out = _public(sub)
    out['secret'] = sub['secret']  # shown ONCE on creation
    return api_created(subscription=out)


@api_v1_webhooks_bp.route('/api/v1/webhooks/<sub_id>', methods=['DELETE'])
@require_scope('webhooks')
@api_meta(summary='Delete a webhook subscription', tags=['webhooks'],
          scope='webhooks')
def delete_sub(sub_id):
    owner_user_id = _current_owner_user_id()
    if not _delete_subscription(sub_id, owner_user_id):
        return api_not_found('Subscription not found')
    audit_log('webhook_deleted', subscription_id=sub_id,
              by=(current_auth().key_id if current_auth() else ''))
    return api_ok({'deleted': sub_id})


__all__ = ['api_v1_webhooks_bp']
