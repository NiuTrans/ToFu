"""lib/llm_dispatch/ephemeral.py — Ephemeral, request-scoped Slot injection.

External callers (headless API, ``/api/v1/agent/run``) supply a
``(base_url, api_key, model_id)`` tuple at request time. We mint a
:class:`~lib.llm_dispatch.slot.Slot` scoped to a single task / request,
inject it into the dispatcher pool, and tear it back out when the task
finishes — so the dispatcher's selection, retry, and metrics machinery
all just work, but the slot never load-balances against a different
caller's request and never persists across the process.

Lifecycle::

    handle = mint_ephemeral_slot(base_url='http://...', api_key='sk-...',
                                 model_id='deepseek-v4-pro',
                                 owner='task_abc123')
    try:
        # dispatch_chat(prefer_model='deepseek-v4-pro', strict_model=True)
        # picks the ephemeral slot because no other deepseek-v4-pro slot
        # exists in this process — or, if one does, the random jitter +
        # cold latency_ema steers traffic toward whichever slot is best.
        ...
    finally:
        dispose_ephemeral_slot(handle)

The handle returned by :func:`mint_ephemeral_slot` is opaque; callers
hand it to :func:`dispose_ephemeral_slot` exactly once. Disposal is
idempotent — a double-dispose logs at debug and returns False.

Security
--------
The ``api_key`` is held in process memory only, never persisted, never
logged in full, and never included in task snapshots / `/tasks/{id}`
responses. The slot's ``key_name`` carries an ``ephemeral_<8hex>``
prefix so logs / metrics / debug dumps can identify ephemeral usage
without exposing the key.

Proxy bypass
------------
If ``base_url`` resolves to a private / pseudo-private IP (per
:func:`lib.llm_dispatch.discovery.is_local_endpoint`) we register the
host with :func:`lib.proxy.register_no_proxy_url` so the very first
request bypasses any corporate ``https_proxy`` — same treatment given
to ``brand=='local'`` providers in
``dispatcher._build_slots_from_providers``.
"""

from __future__ import annotations

import os
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from lib.log import audit_log, get_logger

from .config import DEFAULT_SLOT_CONFIGS
from .factory import get_dispatcher
from .slot import Slot

logger = get_logger(__name__)

__all__ = [
    'EphemeralSlotHandle',
    'mint_ephemeral_slot',
    'dispose_ephemeral_slot',
    'count_ephemeral_slots',
]


# ── Pre-flight reachability probe ──
# A self-hosted / raw-IP BYO endpoint that is down would otherwise only be
# discovered on the FIRST chat request, which then eats the full connect
# timeout before the dispatcher can react. A cheap TCP-connect probe at mint
# time surfaces "endpoint unreachable" immediately as a clean 400, so the
# caller gets a clear error instead of a 60s stall. Disabled with
# TOFU_EPHEMERAL_PREFLIGHT=0; timeout via TOFU_EPHEMERAL_PREFLIGHT_TIMEOUT.
def _preflight_enabled() -> bool:
    return os.environ.get('TOFU_EPHEMERAL_PREFLIGHT', '1').strip().lower() \
        not in ('0', 'false', 'no', 'off', '')


def _preflight_timeout() -> float:
    try:
        t = float(os.environ.get('TOFU_EPHEMERAL_PREFLIGHT_TIMEOUT', '3'))
        return t if t > 0 else 3.0
    except (ValueError, TypeError):
        return 3.0


def _probe_reachable(base_url: str) -> tuple[bool, str]:
    """Best-effort TCP-connect probe to *base_url*'s host:port.

    Returns ``(ok, detail)``. ``ok=True`` when the socket connects within
    the timeout (the server is listening). We deliberately use a raw TCP
    connect rather than an HTTP GET so the probe is independent of auth,
    the ``/models`` path, and HTTP semantics — it answers exactly one
    question: "is something accepting connections on that port?".
    """
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname or ''
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except Exception as e:
        return False, 'unparseable url: %s' % e
    if not host:
        return False, 'no host in url'
    timeout = _preflight_timeout()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, 'ok'
    except (OSError, socket.timeout) as e:
        return False, '%s: %s' % (type(e).__name__, e)


# Per-process ceiling. Each ephemeral slot is cheap (a Slot dataclass +
# the dispatcher's list slot), but a misbehaving caller leaking handles
# would still grow the slot pool unboundedly. 1024 is well above any
# realistic concurrent-request count and well below memory pressure.
_MAX_EPHEMERAL_SLOTS = 1024

_lock = threading.Lock()
_handles: dict[str, 'EphemeralSlotHandle'] = {}


@dataclass
class EphemeralSlotHandle:
    """Opaque handle for an injected ephemeral Slot.

    Returned by :func:`mint_ephemeral_slot`; consumed by
    :func:`dispose_ephemeral_slot`. Callers do NOT introspect the
    fields — they exist for logging and the disposal lookup only.
    """
    handle_id: str
    slot: Slot
    owner: str           # opaque tag (task_id / request_id) for audit
    minted_at: float
    disposed: bool = False


def _normalise_base_url(url: str) -> str:
    """Strip trailing whitespace + slash; enforce http/https.

    Mirrors the lightweight normalisation in
    :func:`lib.llm_dispatch.discovery.normalize_base_url` without
    pulling in the full discovery module (which has its own retries,
    pricing enrichment, etc.).
    """
    if not isinstance(url, str):
        raise ValueError('base_url must be a string')
    url = url.strip().rstrip('/')
    if not url:
        raise ValueError('base_url is empty')
    if not (url.startswith('http://') or url.startswith('https://')):
        raise ValueError('base_url must start with http:// or https://')
    # Use-time SSRF egress guard. Every BYO completion (native chat,
    # agent/run, OpenAI/Anthropic compat, inline-provider blocks) mints a
    # slot through here before any request, so this single check covers
    # the whole proxy surface — and runs at USE time, defeating a DNS
    # rebind that passed the registration-time check in byo_providers.
    from lib.byo_egress import EgressDenied, validate_egress_url
    try:
        validate_egress_url(url)
    except EgressDenied as e:
        raise ValueError(str(e)) from e
    return url


def _seed_caps_and_pricing(model_id: str) -> tuple[set, float, float, float]:
    """Resolve (capabilities, rpm, latency_ms, cost_per_1k) for an ephemeral slot.

    Falls back to ``{'text'}`` / 30 rpm / 3000 ms / $0.01 when the model
    isn't in :data:`DEFAULT_SLOT_CONFIGS`. We deliberately do NOT call
    ``discover_models`` here — that's a synchronous network round-trip
    and the caller has already declared the model_id.
    """
    cfg = DEFAULT_SLOT_CONFIGS.get(model_id, {})
    caps = set(cfg.get('caps') or {'text'})
    rpm = float(cfg.get('rpm') or 30)
    latency = float(cfg.get('latency') or 3000)
    cost = float(cfg.get('cost') or 0.01)
    return caps, rpm, latency, cost


def mint_ephemeral_slot(*, base_url: str, api_key: str, model_id: str,
                        owner: str = '',
                        extra_headers: Optional[dict] = None,
                        thinking_format: str = '',
                        capabilities: Optional[set] = None,
                        ) -> EphemeralSlotHandle:
    """Inject a request-scoped Slot into the dispatcher.

    Args:
        base_url: Provider base URL (must include scheme).
        api_key: Bearer credential. Empty string is allowed for
            unauthenticated local engines (vLLM / Ollama default).
        model_id: Logical model name. The dispatcher's
            ``prefer_model=`` argument matches against this.
        owner: Opaque tag for audit / debugging — typically the
            calling task_id. Logged on mint and dispose.
        extra_headers: Optional dict of provider-specific headers
            (e.g. ``{'X-Internal-Auth': '...'}``).
        thinking_format: Override ``Slot.thinking_format`` when the
            caller knows the engine's thinking-arg dialect (rare —
            usually best left empty so the body builder auto-detects).
        capabilities: Override capability set. Defaults to whatever
            :data:`DEFAULT_SLOT_CONFIGS` says for ``model_id``, falling
            back to ``{'text'}``.

    Returns:
        :class:`EphemeralSlotHandle` — pass to
        :func:`dispose_ephemeral_slot` when done.

    Raises:
        ValueError: ``base_url`` malformed or ``model_id`` empty.
        RuntimeError: Per-process ephemeral-slot ceiling reached
            (caller leaking handles).
    """
    base_url = _normalise_base_url(base_url)
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError('model_id is required')
    model_id = model_id.strip()
    api_key = (api_key or '').strip()

    with _lock:
        if len(_handles) >= _MAX_EPHEMERAL_SLOTS:
            raise RuntimeError(
                f'Ephemeral slot pool full ({_MAX_EPHEMERAL_SLOTS}); '
                'a caller is likely leaking handles')

    # Pre-register no_proxy for self-hosted hosts. Mirrors the treatment
    # given to brand=='local' providers in _build_slots_from_providers
    # (the local-endpoints memory documents why this is critical for
    # hosts like 33.x.x.x). We use should_bypass_proxy (not just
    # is_local_endpoint) so bare-IP endpoints on internal-but-publicly-
    # routable corp ranges (33.x) are covered without TOFU_LOCAL_CIDRS.
    _is_self_hosted = False
    try:
        from lib.llm_dispatch.discovery import should_bypass_proxy
        from lib.proxy import register_no_proxy_url
        _is_self_hosted = should_bypass_proxy(base_url)
        if _is_self_hosted:
            register_no_proxy_url(base_url)
    except Exception as e:
        logger.debug('[Ephemeral] no_proxy registration probe failed: %s', e)

    # ★ Pre-flight reachability probe. Only for self-hosted / raw-IP
    #   endpoints — those are the boxes that go down without an SLA and
    #   whose first request would otherwise stall on the connect timeout.
    #   Cloud BYO endpoints (addressed by domain) are assumed reachable;
    #   probing them just adds a round-trip. A failed probe raises
    #   ValueError → the HTTP callers (byo_resolve / api_v1.chat) already
    #   map mint ValueError to a clean 400, so the user sees "endpoint
    #   unreachable" instead of a 60s hang.
    if _is_self_hosted and _preflight_enabled():
        ok, detail = _probe_reachable(base_url)
        if not ok:
            logger.warning('[Ephemeral] pre-flight probe FAILED for %s: %s',
                           base_url, detail)
            raise ValueError(
                'endpoint unreachable at %s (%s); the model server may be '
                'down or the port not listening' % (base_url, detail))
        logger.debug('[Ephemeral] pre-flight probe ok for %s', base_url)

    caps, rpm, latency, cost = _seed_caps_and_pricing(model_id)
    if capabilities:
        caps = set(capabilities)

    handle_id = secrets.token_hex(8)
    key_name = f'ephemeral_{handle_id}'
    slot = Slot(
        key_name=key_name,
        api_key=api_key,
        model=model_id,
        capabilities=caps,
        base_url=base_url,
        provider_id=f'ephemeral:{owner or handle_id}',
        extra_headers=dict(extra_headers or {}),
        thinking_format=thinking_format or '',
        rpm_limit=rpm,
        latency_ema=latency,
        cost_per_1k_tokens=cost,
        stream_only=False,
    )

    dispatcher = get_dispatcher()
    dispatcher.initialize()
    with dispatcher._lock:
        dispatcher.slots.append(slot)
    handle = EphemeralSlotHandle(
        handle_id=handle_id, slot=slot, owner=str(owner or ''),
        minted_at=time.time(),
    )
    with _lock:
        _handles[handle_id] = handle

    # Audit + log WITHOUT the api_key (the prefix is enough for diagnostics).
    api_key_hint = (api_key[:6] + '…') if len(api_key) > 8 else ('<empty>' if not api_key else '<short>')
    audit_log('ephemeral_slot_mint', handle=handle_id, owner=str(owner or ''),
              model=model_id, base_url=base_url, api_key_hint=api_key_hint)
    logger.info('[Ephemeral] mint handle=%s owner=%s model=%s url=%s key=%s caps=%s',
                handle_id, owner or '?', model_id, base_url, api_key_hint,
                ','.join(sorted(caps)))
    return handle


def dispose_ephemeral_slot(handle: EphemeralSlotHandle) -> bool:
    """Remove an ephemeral slot from the dispatcher pool.

    Idempotent: calling twice on the same handle returns False the
    second time and emits a debug log. Returns True on the first
    successful disposal.
    """
    if handle is None:
        return False
    if not isinstance(handle, EphemeralSlotHandle):
        logger.debug('[Ephemeral] dispose called with non-handle: %r', handle)
        return False

    with _lock:
        if handle.disposed or handle.handle_id not in _handles:
            logger.debug('[Ephemeral] dispose noop handle=%s already_disposed=%s',
                         handle.handle_id, handle.disposed)
            handle.disposed = True
            _handles.pop(handle.handle_id, None)
            return False
        handle.disposed = True
        _handles.pop(handle.handle_id, None)

    dispatcher = get_dispatcher()
    removed = False
    with dispatcher._lock:
        try:
            dispatcher.slots.remove(handle.slot)
            removed = True
        except ValueError as e:
            # Slot already gone — possible if a future code path nukes
            # the slot pool (reset_dispatcher / hot-reload). Treat as
            # successful disposal.
            logger.debug('[Ephemeral] slot already absent from pool: %s', e)
    audit_log('ephemeral_slot_dispose', handle=handle.handle_id,
              owner=handle.owner, removed=removed,
              lifetime_ms=int((time.time() - handle.minted_at) * 1000))
    logger.info('[Ephemeral] dispose handle=%s owner=%s removed=%s lifetime=%.1fs',
                handle.handle_id, handle.owner or '?', removed,
                time.time() - handle.minted_at)
    return removed


def count_ephemeral_slots() -> int:
    """Number of currently-injected ephemeral slots (for /capabilities + tests)."""
    with _lock:
        return len(_handles)
