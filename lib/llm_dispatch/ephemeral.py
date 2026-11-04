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
        with provider_pin(handle.slot.provider_id):
            # Only a context pinned to the slot's provider_id selects it;
            # an unpinned dispatch sees configured slots only.
            dispatch_chat(..., prefer_model='deepseek-v4-pro', strict_model=True)
    finally:
        dispose_ephemeral_slot(handle)

The handle returned by :func:`mint_ephemeral_slot` is opaque; callers
hand it to :func:`dispose_ephemeral_slot` exactly once. Disposal is
idempotent — a double-dispose returns False.

Route groups
------------
A model-routing v2 group mints one slot per eligible Deployment. Its owner
calls :func:`prepare_ephemeral_slot` per candidate (no lock; a shared
``endpoint_verdicts`` map checks each distinct base URL once), then
:func:`publish_ephemeral_slots` once: the whole group is registered and
published in one dispatcher-lock critical section, in rank order.
:func:`dispose_ephemeral_slots` withdraws it again by indexed identity lookup.
A task's group holds every authorized failover candidate (often dozens of
slots), so mint/dispose write one audit record and INFO line per group
(owner + provider pin, primary candidate, slot count); per-slot detail is
DEBUG only.

Security
--------
The ``api_key`` is held in process memory only, never persisted, never
logged in full, and never included in task snapshots / `/tasks/{id}`
responses. The slot's ``key_name`` carries an ``ephemeral_<8hex>``
prefix so logs / metrics / debug dumps can identify ephemeral usage
without exposing the key.

Proxy bypass
------------
If ``base_url`` is self-hosted (private / pseudo-private IP or bare IP, per
:func:`lib.llm_dispatch.discovery.should_bypass_proxy`) the handle leases
the host's proxy exemption with :func:`lib.proxy.lease_no_proxy_url`, so
the very first request bypasses any corporate ``https_proxy``. Disposal
returns the lease: like the slot itself, the exemption never outlives the
request, and a mint the pre-flight probe rejects takes none.
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
from .slot import Slot

# get_dispatcher is imported lazily inside the functions below: a top-level
# import would pull factory → dispatcher (and the HTTP transport stack) into
# every server boot, breaking the startup-dormancy contract pinned by
# tests/llm/test_llm_dispatch_startup_boundary.py.

logger = get_logger(__name__)

__all__ = [
    'EphemeralSlotHandle',
    'prepare_ephemeral_slot',
    'publish_ephemeral_slots',
    'mint_ephemeral_slot',
    'dispose_ephemeral_slots',
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
def _benchmark_mode() -> bool:
    """True when operator slots are disabled (TOFU_DISABLE_CONFIGURED_SLOTS).

    In this mode the ephemeral slot is the ONLY route to dispatch — there
    is no operator pool to fall back to (that's the whole point of the
    fail-loud benchmark mode). So a hard mint-time 400 on a transient TCP
    blip would discard the entire request / benchmark arm for a reason
    that has nothing to do with the model. Spinning up isolation must not
    hinge on a single socket connect landing; a genuinely-down endpoint is
    better surfaced by the dispatch retry loop (abortable, with backoff).
    """
    return os.environ.get('TOFU_DISABLE_CONFIGURED_SLOTS', '').strip().lower() \
        in ('1', 'true', 'yes', 'on')


def _preflight_enabled() -> bool:
    """Whether to TCP-probe a self-hosted endpoint at mint time.

    Precedence:
      1. Explicit ``TOFU_EPHEMERAL_PREFLIGHT`` (1/0) ALWAYS wins — an
         operator who sets it means it, even in benchmark mode.
      2. Unset + benchmark mode (``TOFU_DISABLE_CONFIGURED_SLOTS``) →
         OFF, so a connect blip can't 400 a whole arm.
      3. Unset + normal mode → ON (the interactive default: a user who
         typed a bad BYO URL gets an instant clear error, not a 60s hang).
    """
    raw = os.environ.get('TOFU_EPHEMERAL_PREFLIGHT', '').strip().lower()
    if raw != '':
        return raw not in ('0', 'false', 'no', 'off')
    if _benchmark_mode():
        return False
    return True


def _preflight_timeout() -> float:
    try:
        t = float(os.environ.get('TOFU_EPHEMERAL_PREFLIGHT_TIMEOUT', '3'))
        return t if t > 0 else 3.0
    except (ValueError, TypeError) as e:
        logger.debug('[Ephemeral] TOFU_EPHEMERAL_PREFLIGHT_TIMEOUT parse failed, using default: %s', e)
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
        logger.debug('[Ephemeral] reachability probe url parse failed: %s', e)
        return False, 'unparseable url: %s' % e
    if not host:
        return False, 'no host in url'
    timeout = _preflight_timeout()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, 'ok'
    except (OSError, socket.timeout) as e:
        logger.debug('[Ephemeral] reachability probe connect failed: %s', e)
        return False, '%s: %s' % (type(e).__name__, e)


def _audit_slot(event: str, **details) -> None:
    # An observer cannot strand a newly registered handle or interrupt release.
    try:
        audit_log(event, **details)
    except Exception as exc:
        logger.warning('[Ephemeral] audit event=%s failed (%s)',
                       event, type(exc).__name__)


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
    proxy_bypass_host: str = ''  # leased exemption, returned on disposal


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
    # Use-time SSRF egress guard. Every routed completion (native chat,
    # agent/run, and OpenAI/Anthropic compatibility) mints a
    # slot through here before any request, so this single check covers
    # the whole proxy surface — and runs at USE time, defeating a DNS
    # rebind that passed the registration-time check in byo_providers.
    from lib.byo.byo_egress import EgressDenied, validate_egress_url
    try:
        validate_egress_url(url)
    except EgressDenied as e:
        raise ValueError(str(e)) from e
    return url


def _release_proxy_bypass(handle: EphemeralSlotHandle) -> None:
    host, handle.proxy_bypass_host = handle.proxy_bypass_host, ''
    if host:
        from lib.proxy import release_no_proxy_host
        release_no_proxy_host(host)


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


@dataclass(frozen=True)
class _EndpointVerdict:
    """One endpoint checked for use now: normalized URL and proxy class."""

    base_url: str
    self_hosted: bool


def _check_endpoint(base_url) -> _EndpointVerdict:
    """Validate one endpoint for use now; ValueError when it must not be used."""
    base_url = _normalise_base_url(base_url)
    # Self-hosted hosts bypass the corporate proxy (the local-endpoints
    # memory documents why this is critical for hosts like 33.x.x.x). We use
    # should_bypass_proxy (not just is_local_endpoint) so bare-IP endpoints
    # on internal-but-publicly-routable corp ranges (33.x) are covered
    # without TOFU_LOCAL_CIDRS. The exemption is leased after the probe.
    self_hosted = False
    try:
        from lib.llm_dispatch.discovery import should_bypass_proxy
        self_hosted = should_bypass_proxy(base_url)
    except Exception as e:
        logger.debug('[Ephemeral] self-hosted classification failed: %s', e)

    # Pre-flight reachability probe. Only for self-hosted / raw-IP
    #   endpoints — those are the boxes that go down without an SLA and
    #   whose first request would otherwise stall on the connect timeout.
    #   Cloud BYO endpoints (addressed by domain) are assumed reachable;
    #   probing them just adds a round-trip. A failed probe raises
    #   ValueError → the HTTP callers (byo_resolve / api_v1.chat) already
    #   map mint ValueError to a clean 400, so the user sees "endpoint
    #   unreachable" instead of a 60s hang.
    if self_hosted and _preflight_enabled():
        ok, detail = _probe_reachable(base_url)
        if not ok:
            logger.warning('[Ephemeral] pre-flight probe FAILED for %s: %s',
                           base_url, detail)
            raise ValueError(
                'endpoint unreachable at %s (%s); the model server may be '
                'down or the port not listening' % (base_url, detail))
        logger.debug('[Ephemeral] pre-flight probe ok for %s', base_url)
    elif self_hosted and _benchmark_mode():
        # Skipped on purpose — see _preflight_enabled. The dispatch retry
        # loop is the safety net for a genuinely-down endpoint here.
        logger.debug('[Ephemeral] pre-flight probe skipped for %s '
                     '(benchmark mode: TOFU_DISABLE_CONFIGURED_SLOTS)', base_url)
    return _EndpointVerdict(base_url, self_hosted)


def _endpoint_verdict(base_url, endpoint_verdicts) -> _EndpointVerdict:
    """Check *base_url* at most once per caller-owned verdict map."""
    if endpoint_verdicts is None:
        return _check_endpoint(base_url)
    verdict = endpoint_verdicts.get(base_url)
    if verdict is None:
        try:
            verdict = _check_endpoint(base_url)
        except ValueError as exc:
            verdict = exc
        endpoint_verdicts[base_url] = verdict
    if isinstance(verdict, ValueError):
        raise ValueError(*verdict.args)
    return verdict


def prepare_ephemeral_slot(*, base_url: str, api_key: str, model_id: str,
                           owner: str = '',
                           extra_headers: Optional[dict] = None,
                           thinking_format: str = '',
                           inference_profile: Optional[dict] = None,
                           context_window: int = 0,
                           capabilities: Optional[set] = None,
                           wire_model_id: str = '',
                           protocol: str = '',
                           responses_profile: str = '',
                           oauth: str = '',
                           adapter: Optional[dict] = None,
                           routing_owner_user_id: int = 0,
                           routing_tenant_id: str = '',
                           provider_pin_id: str = '',
                           endpoint_verdicts: Optional[dict] = None,
                           ) -> EphemeralSlotHandle:
    """Validate and build one request-scoped Slot without publishing it.

    Takes no lock. Publish the handle with :func:`publish_ephemeral_slots`
    (a route group publishes all its handles together) or retire it with
    :func:`dispose_ephemeral_slots`.

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
        adapter: Owner-authorized desktop loopback relay marker from a v2
            Connection. Empty means the endpoint is reached normally.
        routing_owner_user_id: Explicit v2 owner used only to namespace
            credential-health state; zero retains legacy transient behavior.
        routing_tenant_id: The same owner boundary's tenant; it scopes
            learned credential health together with the owner.
        endpoint_verdicts: Caller-owned map memoizing the endpoint check
            (normalization, egress validation, proxy classification and
            reachability probe) per raw ``base_url``: one route group checks
            each distinct endpoint once.

    Raises:
        ValueError: ``base_url`` malformed, denied or unreachable, or
            ``model_id`` empty.
    """
    endpoint = _endpoint_verdict(base_url, endpoint_verdicts)
    base_url = endpoint.base_url
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError('model_id is required')
    model_id = model_id.strip()
    wire_model_id = str(wire_model_id or model_id).strip()
    if not wire_model_id:
        raise ValueError('wire_model_id is required')
    api_key = (api_key or '').strip()

    caps, rpm, latency, cost = _seed_caps_and_pricing(model_id)
    if capabilities is not None:
        caps = set(capabilities)

    handle_id = secrets.token_hex(8)
    key_name = f'ephemeral_{handle_id}'
    slot = Slot(
        key_name=key_name,
        api_key=api_key,
        model=wire_model_id,
        logical_model=model_id,
        capabilities=caps,
        base_url=base_url,
        # The dispatcher provider_id is what the thread-scoped hard pin
        #   matches. Model-routing v2 supplies one unique group ID for all
        #   candidates in a request; legacy one-slot callers fall back to the
        #   handle ID. ``owner`` is an audit/debug tag, never identity.
        provider_id=(provider_pin_id or (
            f'ephemeral:{owner}:{handle_id}' if owner
            else f'ephemeral:{handle_id}')),
        routing_owner_user_id=int(routing_owner_user_id or 0),
        routing_tenant_id=str(routing_tenant_id or ''),
        request_scoped=True,
        extra_headers=dict(extra_headers or {}),
        thinking_format=thinking_format or '',
        inference_profile=inference_profile or {'reasoning': {'support': 'unknown'}},
        context_window=context_window,
        protocol=protocol,
        responses_profile=responses_profile or '',
        oauth=oauth or '',
        adapter=dict(adapter or {}),
        rpm_limit=rpm,
        latency_ema=latency,
        cost_per_1k_tokens=cost,
        stream_only=False,
    )
    handle = EphemeralSlotHandle(
        handle_id=handle_id, slot=slot, owner=str(owner or ''),
        minted_at=time.time(),
    )
    if endpoint.self_hosted:
        from lib.proxy import lease_no_proxy_url
        handle.proxy_bypass_host = lease_no_proxy_url(base_url)
    return handle


def publish_ephemeral_slots(handles) -> None:
    """Register prepared handles and publish their slots in one critical section.

    One dispatcher-lock acquisition publishes a whole route group in rank
    order, so a concurrent pick sees none or all of it. On failure nothing
    stays registered or published and every proxy exemption is returned.
    """
    handles = list(handles)
    if not handles:
        return
    from lib.resource_control.adapters import bind_ephemeral
    bind_ephemeral()
    from .factory import get_dispatcher

    # Request authority is already supplied by the caller. Loading the unrelated
    # configured pool here couples standalone/native routes to application storage.
    # A later configured dispatch still initializes normally and retains these slots.
    dispatcher = get_dispatcher()
    slots = [handle.slot for handle in handles]
    # Validation and probes ran outside the locks. Registration has no count
    # ceiling. Both owners publish together, with one lock order also used by
    # disposal.
    try:
        with _lock, dispatcher._lock:
            registered = []
            try:
                for handle in handles:
                    _handles[handle.handle_id] = handle
                    registered.append(handle)
                dispatcher.slots.publish(slots)
            except BaseException:
                for handle in registered:
                    _handles.pop(handle.handle_id, None)
                dispatcher.slots.withdraw(slots)
                raise
    except BaseException:
        for handle in handles:
            handle.disposed = True
            _release_proxy_bypass(handle)
        raise

    try:
        for (owner, provider_id), group in _route_groups(handles).items():
            primary = group[0].slot
            # Record credential presence, never credential characters. Even a
            # short prefix can disclose most of a private gateway token and
            # handle/provider identifiers already provide the correlation
            # needed for diagnostics.
            api_key_hint = '<configured>' if primary.api_key else '<empty>'
            _audit_slot('ephemeral_slot_mint', handle=group[0].handle_id,
                        owner=owner, provider_id=provider_id, slots=len(group),
                        model=primary.model, base_url=primary.base_url,
                        api_key_hint=api_key_hint)
            logger.info('[Ephemeral] mint provider=%s owner=%s slots=%d primary=%s url=%s key=%s',
                        provider_id, owner or '?', len(group), primary.model,
                        primary.base_url, api_key_hint)
            for handle in group:
                logger.debug('[Ephemeral] slot handle=%s model=%s url=%s caps=%s',
                             handle.handle_id, handle.slot.model, handle.slot.base_url,
                             ','.join(sorted(handle.slot.capabilities)))
    except BaseException:
        dispose_ephemeral_slots(handles)
        raise


def _route_groups(handles) -> dict[tuple[str, str], list[EphemeralSlotHandle]]:
    """Handles keyed by (owner, provider pin), in rank order.

    A route group shares one pin, so it is audited as one record rather than
    one per failover candidate.
    """
    groups: dict[tuple[str, str], list[EphemeralSlotHandle]] = {}
    for handle in handles:
        groups.setdefault((handle.owner, handle.slot.provider_id), []).append(handle)
    return groups


def mint_ephemeral_slot(**slot_spec) -> EphemeralSlotHandle:
    """Prepare and publish one request-scoped Slot.

    Accepts the keyword arguments of :func:`prepare_ephemeral_slot`. Returns
    the :class:`EphemeralSlotHandle` to pass to :func:`dispose_ephemeral_slot`
    when done; raises ``ValueError`` for an unusable endpoint or model id.
    """
    handle = prepare_ephemeral_slot(**slot_spec)
    publish_ephemeral_slots([handle])
    return handle


def dispose_ephemeral_slots(handles) -> list[bool]:
    """Withdraw the handles' slots from the pool in one critical section.

    Removal uses indexed identities, leaving every other slot in order.
    Idempotent per handle; returns, per handle, whether this call removed its
    slot from the pool. A prepared handle that was never published is
    retired as well. Every exemption release is attempted; the first failure
    is re-raised afterwards.
    """
    handles = list(handles)
    owned = []
    for handle in handles:
        if isinstance(handle, EphemeralSlotHandle):
            owned.append(handle)
        elif handle is not None:
            logger.debug('[Ephemeral] dispose called with non-handle: %r', handle)
    if not owned:
        return [False] * len(handles)

    from .factory import get_dispatcher

    dispatcher = get_dispatcher()
    retired: list[EphemeralSlotHandle] = []
    published: list[EphemeralSlotHandle] = []
    with _lock, dispatcher._lock:
        for handle in owned:
            if handle.disposed:
                continue
            handle.disposed = True
            retired.append(handle)
            if _handles.pop(handle.handle_id, None) is not None:
                published.append(handle)
        # Configuration reload preserves active slots; a slot already removed
        # by another owner still retires its registry entry.
        removed_slots = dispatcher.slots.withdraw(
            [handle.slot for handle in published])
    removed = {id(handle) for handle in published
               if id(handle.slot) in removed_slots}
    published_ids = {id(handle) for handle in published}
    first_error = None
    for handle in retired:
        try:
            _release_proxy_bypass(handle)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    for (owner, provider_id), group in _route_groups(
            handle for handle in retired if id(handle) in published_ids).items():
        removed_count = sum(id(handle) in removed for handle in group)
        lifetime = time.time() - group[0].minted_at
        _audit_slot('ephemeral_slot_dispose', handle=group[0].handle_id,
                    owner=owner, provider_id=provider_id, slots=len(group),
                    removed=removed_count, lifetime_ms=int(lifetime * 1000))
        logger.info('[Ephemeral] dispose provider=%s owner=%s slots=%d removed=%d lifetime=%.1fs',
                    provider_id, owner or '?', len(group), removed_count, lifetime)
    if first_error is not None:
        raise first_error
    return [id(handle) in removed for handle in handles]


def dispose_ephemeral_slot(handle: EphemeralSlotHandle) -> bool:
    """Remove one ephemeral slot from the dispatcher pool.

    Idempotent: calling twice on the same handle returns False the
    second time. Returns True on the first successful removal.
    """
    return dispose_ephemeral_slots([handle])[0]


def count_ephemeral_slots() -> int:
    """Number of currently-injected ephemeral slots (for /capabilities + tests)."""
    with _lock:
        return len(_handles)
