"""lib/llm_dispatch/provider_catalog_sync.py — Remote provider catalog sync.

Periodically re-queries every configured remote provider's OpenAI-compatible
``/models`` listing with the stored credential and folds NEW wire identities
into the owner-scoped model-routing v2 aggregate as pending offerings.
Template JSON files remain the curated onboarding recipe; this worker keeps
an already-configured provider current when the vendor ships a new model
(the "Gemini 3.8 Flash is already out" gap) without waiting for a template
edit or an app restart.

Safety rails
------------
* **Add-only.** Existing rows are never modified — a user-disabled offering
  stays disabled, a confirmed identity stays confirmed — and nothing is
  ever deleted.
 A disabled provider ACCESS still gets its catalog
  maintained: the access switch gates routing, not freshness, so
  re-enabling a provider never reveals a stale model list. A disabled
  connection or credential stays untouched — that is a per-component
  revocation, not a routing switch.
* **No zombies.** A synced model whose offering the user deletes moves to a
  per-owner ``dismissed`` ledger and is never re-added.
* **Bounded.** Aggregator-scale catalogs (more than ``_MAX_CATALOG_MODELS``
  rows, e.g. OpenRouter) are skipped wholesale: flooding the aggregate with
  hundreds of relay identities is never correct. One pass adds at most
  ``_MAX_ADDITIONS_PER_PROVIDER`` models to one provider access.
* **Fail-closed per provider.** One failing endpoint never masks the
  others; repeated failures of the same access log at DEBUG after the
  first WARNING so a dead key does not spam the log every pass.
* **Explicit owner.** Runs only in personal mode; the sweep accepts an
  explicit owner/repository seam so a future distributed scheduler can
  enumerate owners without a module-global user.
* **Opt-out.** ``TOFU_MODEL_CATALOG_SYNC=0`` disables the worker and
  makes :func:`sweep_once` a no-op.
* Rides health_local's shared monitor thread — no second resident worker.
"""

import hashlib
import os
import threading
import time

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger
from lib.model_routing.offering_metadata import discovery_context_budget
from lib.model_routing import (
    ModelRoutingError,
    ModelRoutingRepository,
    OwnerBoundary,
)

from .discovery import discover_models, is_local_endpoint

logger = get_logger(__name__)

__all__ = [
    'poll_if_due',
    'sweep_once',
    'start_provider_catalog_sync',
    'stop_provider_catalog_sync',
    'trigger_provider_catalog_sync',
]


_DISCOVER_TIMEOUT = 10  # remote network, unlike the loopback sweep
# A /models listing larger than this belongs to a relay/aggregator, not to a
# vendor's own catalog — auto-importing it would flood the aggregate.
_MAX_CATALOG_MODELS = 200
# Bound on one pass's writes so additions stay reviewable and the aggregate
# budget is consumed gradually across passes.
_MAX_ADDITIONS_PER_PROVIDER = 32


def _env_seconds(name: str, default: float, minimum: float,
                 maximum: float) -> float:
    """Read one bounded polling value without making import env-fragile."""
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    return min(maximum, max(minimum, value))


_BOOT_DELAY = _env_seconds(
    'TOFU_MODEL_CATALOG_SYNC_DELAY', 600.0, 0.0, 3600.0)
_SYNC_INTERVAL = _env_seconds(
    'TOFU_MODEL_CATALOG_SYNC_INTERVAL', 6.0 * 3600.0, 900.0, 86400.0)

_STATE_PATH = config_path('model_catalog_sync.json')

# Cooperative-job scheduling state on health_local's monitor thread. Bounded
# in-process bookkeeping only; durable per-owner added/dismissed ledgers live
# in _STATE_PATH, provider authority in model-routing v2.
_schedule_lock = threading.Lock()
_runtime_enabled = True
_next_sync_at: float | None = None
_force_sync = False
_schedule_generation = 0
_failure_counts: dict[str, int] = {}


def _disabled() -> bool:
    return os.environ.get('TOFU_MODEL_CATALOG_SYNC', '1').strip().lower() in (
        '0', 'false', 'no', 'off')


def _runtime_context() -> tuple[OwnerBoundary, ModelRoutingRepository] | None:
    """Return the personal monitor owner, or refuse implicit enumeration."""
    from .autodiscover_local import _runtime_context as _shared_runtime_context

    return _shared_runtime_context()


def _stable_id(kind: str, *parts: object) -> str:
    digest = hashlib.sha256(
        '\x00'.join(str(part) for part in parts).encode('utf-8')
    ).hexdigest()[:24]
    return f'csync-{kind}-{digest}'


# ── Per-owner added/dismissed ledger ─────────────────────────────────────

def _state_owner_key(boundary: OwnerBoundary) -> str:
    return '%s:%d' % (boundary.tenant_id, boundary.owner_user_id)


def _normalized_state(value) -> dict:
    def _ledger(raw) -> dict:
        if not isinstance(raw, dict):
            return {}
        return {
            str(access): sorted({
                str(mid) for mid in mids if isinstance(mid, str) and mid
            })
            for access, mids in raw.items()
            if isinstance(access, str) and isinstance(mids, list)
        }

    if not isinstance(value, dict):
        return {'added': {}, 'dismissed': {}}
    return {
        'added': _ledger(value.get('added')),
        'dismissed': _ledger(value.get('dismissed')),
    }


def _load_state(boundary: OwnerBoundary) -> dict:
    st = read_json(_STATE_PATH, default=None)
    if not isinstance(st, dict):
        return {'added': {}, 'dismissed': {}}
    owners = st.get('owners')
    if isinstance(owners, dict):
        return _normalized_state(owners.get(_state_owner_key(boundary)))
    return {'added': {}, 'dismissed': {}}


def _save_state(boundary: OwnerBoundary, state: dict) -> None:
    try:
        owner_key = _state_owner_key(boundary)

        def _mutate(current):
            current = current if isinstance(current, dict) else {}
            owners = current.get('owners')
            if not isinstance(owners, dict):
                owners = {}
            owners[owner_key] = _normalized_state(state)
            return {'version': 2, 'owners': owners}

        update_json_atomic(_STATE_PATH, _mutate, default={})
    except Exception as e:
        logger.warning('[CatalogSync] state persist failed: %s', e)


# ── Target enumeration ───────────────────────────────────────────────────

def _eligible_targets(document: dict) -> list:
    """Remote OpenAI-compatible (connection × credential) pairs to re-list.

    Loopback/private endpoints are the local sweep's domain; ``local`` and
    non-OpenAI protocols expose no compatible ``/models`` listing here.
    """
    # Disabled accesses stay eligible: their switch gates routing, and the
    # add-only sync neither routes traffic nor flips any row back on.
    accesses = {
        row.get('provider_access_id'): row
        for row in document.get('provider_accesses') or []
        if isinstance(row, dict)
    }
    credentials_by_access: dict[str, list] = {}
    for row in document.get('credentials') or []:
        if not isinstance(row, dict) or not row.get('enabled', True):
            continue
        if str(row.get('kind') or '') != 'api_key':
            continue
        if not str(row.get('secret_reference') or '').strip():
            continue
        credentials_by_access.setdefault(
            row.get('provider_access_id'), []).append(row)

    targets = []
    for row in document.get('connections') or []:
        if not isinstance(row, dict) or not row.get('enabled', True):
            continue
        access_id = row.get('provider_access_id')
        access = accesses.get(access_id)
        if access is None:
            continue
        if str(row.get('protocol') or 'openai') not in ('openai', 'openai_responses', 'anthropic'):
            continue
        base_url = str(row.get('base_url') or '').strip().rstrip('/')
        if not base_url or is_local_endpoint(base_url):
            continue
        connection_id = row.get('connection_id')
        credential = None
        for cand in credentials_by_access.get(access_id) or []:
            authorized = (cand.get('authorization') or {}).get('connection_ids')
            if isinstance(authorized, list) and authorized \
                    and connection_id not in authorized:
                continue
            credential = cand
            break
        if credential is None:
            continue
        targets.append({
            'access_id': access_id,
            'provider_id': access.get('provider_id'),
            'display_name': str(access.get('display_name')
                                or access.get('provider_id') or access_id),
            'connection_id': connection_id,
            'base_url': base_url,

            'protocol': row.get('protocol') or 'openai',
            'extra_headers': dict(row.get('extra_headers') or {}),
            'secret_reference': str(credential['secret_reference']),
        })
    return targets


def _resolve_api_key(repository, boundary: OwnerBoundary,
                     secret_reference: str) -> str:
    from lib.model_routing.dispatch_adapter import decode_credential_secret


    try:
        plaintext = repository.resolve_secret(boundary, secret_reference)
        api_key, _oauth, _headers = decode_credential_secret(
            plaintext, kind='api_key')
        return api_key or ''
    except Exception as e:
        logger.debug('[CatalogSync] secret resolve failed: %s', e)
        return ''


def _known_model_ids(document: dict, access_id: str) -> set:
    """Every model id the access already serves, in any identity state."""
    known = set()
    connection_ids = {
        row.get('connection_id')
        for row in document.get('connections') or []
        if isinstance(row, dict) and row.get('provider_access_id') == access_id
    }
    offering_ids = set()
    for row in document.get('offerings') or []:
        if not isinstance(row, dict):
            continue
        if row.get('provider_access_id') != access_id:
            continue
        offering_ids.add(row.get('offering_id'))
        pending = str(row.get('pending_model_id') or '').strip()
        if pending:
            known.add(pending)
        model = row.get('model')
        if isinstance(model, dict):
            confirmed = str(model.get('model_id') or '').strip()
            if confirmed:
                known.add(confirmed)
    for row in document.get('deployments') or []:
        if not isinstance(row, dict):
            continue
        if row.get('connection_id') in connection_ids \
                or row.get('offering_id') in offering_ids:
            wire = str(row.get('wire_model_id') or '').strip()
            if wire:
                known.add(wire)
    return known


def _capabilities(raw) -> list:
    caps = {
        item.strip() for item in (raw if isinstance(raw, list) else [])[:64]
        if isinstance(item, str) and item.strip()
        and len(item.strip()) <= 256
        and not any(ord(ch) < 32 for ch in item.strip())
    }
    return sorted(caps if isinstance(raw, list) else {'text'})


def _append_synced_models(repository, boundary: OwnerBoundary, target: dict,
                          models: list) -> tuple[int, int]:
    """CAS-append pending offerings/deployments; returns (added, revision)."""

    access_id = target['access_id']
    connection_id = target['connection_id']

    from lib.model_routing.inference import profile_from_metadata

    def _mutate(working: dict):
        known = _known_model_ids(working, access_id)
        from lib.model_routing.catalog_identity import inherit_discovered_metadata
        inherited = inherit_discovered_metadata(models, working['models'])
        appended = False
        priority_base = len(working.get('offerings') or [])
        for offset, row in enumerate(inherited):
            model_id = str(row.get('model_id') or '').strip()
            if not model_id or model_id in known:
                continue
            offering_id = _stable_id('offering', access_id, model_id)
            working['offerings'].append({
                'offering_id': offering_id,
                'provider_access_id': access_id,
                'identity_state': 'pending_identity',
                'pending_model_id': model_id,
                'enabled': True,
                'stale': False,
                'capabilities': _capabilities(row.get('capabilities')),
                'inherited_model_fields': row['inherited_model_fields'],

                'inference_profile': profile_from_metadata(row, source='provider_catalog'),
                'context_window': discovery_context_budget(row),
                'priority': priority_base + offset,
            })
            working['deployments'].append({
                'deployment_id': _stable_id(
                    'deployment', access_id, connection_id, model_id),
                'offering_id': offering_id,
                'connection_id': connection_id,
                'wire_model_id': model_id,
                'identity_confidence': 'pending',
                'probe_status': 'unprobed',
                'priority': priority_base + offset,
            })
            known.add(model_id)
            appended = True
        from lib.model_routing.catalog_identity import reconcile_catalog_identities
        bound = reconcile_catalog_identities(working, access_id=access_id)
        return working if appended or bound else None

    authority = repository.mutate(boundary, _mutate)
    return len(models), authority.revision


def _log_failure(target: dict, error) -> None:
    access_id = target['access_id']
    count = _failure_counts.get(access_id, 0) + 1
    _failure_counts[access_id] = count
    log = logger.warning if count == 1 else logger.debug
    log('[CatalogSync] %s (%s) catalog listing failed (x%d): %s',
        target['display_name'], target['base_url'], count, error)


def sweep_once(discover=None, *, boundary: OwnerBoundary | None = None,
               repository=None) -> dict:
    """One catalog-sync pass over every eligible remote provider access.

    ``discover(base_url, api_key)`` is the dependency-injected seam keeping
    tests off the network. Returns a stats dict.
    """
    stats = {
        'scanned': 0,
        'skipped_no_key': [],
        'skipped_aggregator': [],
        'empty': [],
        'failed': [],
        'unchanged': [],
        'added': [],
        'truncated': [],
        'authority_loaded': False,
    }
    if _disabled():
        stats['disabled'] = True
        return stats
    # Keep the historical two-argument injected seam; production carries the
    # connection protocol and owner headers to the shared discovery transport.
    if boundary is None or repository is None:
        runtime_context = _runtime_context()
        if runtime_context is None:
            stats['owner_enumeration_required'] = True
            return stats
        runtime_boundary, runtime_repository = runtime_context
        boundary = boundary or runtime_boundary
        repository = repository or runtime_repository
    try:
        authority = repository.get(boundary)
    except ModelRoutingError as e:
        logger.warning('[CatalogSync] cannot load model-routing authority: %s', e)
        return stats
    if authority.revision <= 0:
        stats['authority_inactive'] = True
        return stats
    stats['authority_loaded'] = True
    document = authority.document
    state = _load_state(boundary)

    # ── Reconcile deletions: a synced model whose identity vanished from
    # the aggregate in EVERY form (pending, confirmed, wire) was deleted by
    # the user — dismiss it so it never resurrects. Identity confirmation
    # (pending → confirmed) keeps the id known and is not a deletion.
    state_changed = False
    for access_id, mids in list(state['added'].items()):
        known = _known_model_ids(document, access_id)
        gone = [mid for mid in mids if mid not in known]
        if not gone:
            continue
        remaining = [mid for mid in mids if mid in known]
        if remaining:
            state['added'][access_id] = remaining
        else:
            state['added'].pop(access_id, None)
        dismissed = state['dismissed'].setdefault(access_id, [])
        for mid in gone:
            if mid not in dismissed:
                dismissed.append(mid)
        state_changed = True
        logger.info('[CatalogSync] %d synced model(s) deleted by user on '
                    '%s — dismissed: %s', len(gone), access_id, sorted(gone))
    if state_changed:
        _save_state(boundary, state)

    for target in _eligible_targets(document):
        stats['scanned'] += 1
        access_id = target['access_id']
        api_key = _resolve_api_key(
            repository, boundary, target['secret_reference'])
        if not api_key:
            stats['skipped_no_key'].append(access_id)
            continue
        try:
            models = (discover(target['base_url'], api_key) if discover else
                      discover_models(target['base_url'], api_key,
                                      timeout=_DISCOVER_TIMEOUT,
                                      quiet_not_found=True,
                                      protocol=target['protocol'],
                                      extra_headers=target['extra_headers']))
        except Exception as e:
            _log_failure(target, e)
            stats['failed'].append(access_id)
            continue
        _failure_counts.pop(access_id, None)
        if not models:
            stats['empty'].append(access_id)
            continue
        if len(models) > _MAX_CATALOG_MODELS:
            stats['skipped_aggregator'].append(access_id)
            logger.info('[CatalogSync] %s exposes %d models (>%d) — '
                        'aggregator-scale catalog, skipping auto-import',
                        target['display_name'], len(models),
                        _MAX_CATALOG_MODELS)
            continue
        known = _known_model_ids(document, access_id)
        dismissed = set(state['dismissed'].get(access_id) or [])
        new_models = [
            row for row in models
            if isinstance(row, dict)
            and str(row.get('model_id') or '').strip()
            and str(row.get('model_id')).strip() not in known
            and str(row.get('model_id')).strip() not in dismissed
        ]
        if not new_models:
            stats['unchanged'].append(access_id)
            continue
        if len(new_models) > _MAX_ADDITIONS_PER_PROVIDER:
            stats['truncated'].append(access_id)
            logger.info('[CatalogSync] %s has %d new models — importing the '
                        'first %d this pass', target['display_name'],
                        len(new_models), _MAX_ADDITIONS_PER_PROVIDER)
            new_models = new_models[:_MAX_ADDITIONS_PER_PROVIDER]
        try:
            _count, revision = _append_synced_models(
                repository, boundary, target, new_models)
        except ModelRoutingError as e:
            logger.error('[CatalogSync] model-routing persist failed for '
                         '%s: %s', access_id, e, exc_info=True)
            stats['failed'].append(access_id)
            continue
        added_ids = sorted(str(row['model_id']).strip() for row in new_models)
        state['added'].setdefault(access_id, [])
        for mid in added_ids:
            if mid not in state['added'][access_id]:
                state['added'][access_id].append(mid)
        _save_state(boundary, state)
        stats['added'].append({
            'access_id': access_id,
            'provider_id': target['provider_id'],
            'models': added_ids,
            'model_routing_revision': revision,
        })
        logger.info('[CatalogSync] %s gained %d model(s): %s',
                    target['display_name'], len(added_ids), added_ids)
        audit_log('provider_catalog_synced',
                  provider_access_id=access_id,
                  provider_id=target['provider_id'],
                  models=added_ids,
                  owner_user_id=boundary.owner_user_id,
                  model_routing_revision=revision)
        # Fold the fresh rows into the diff basis so a second connection of
        # the same access within this pass does not duplicate the write.
        document = repository.get(boundary).document

    if stats['added']:
        try:
            from .health_local import _rebuild_dispatcher_slots
            _rebuild_dispatcher_slots()
        except Exception as e:
            logger.error('[CatalogSync] slot rebuild failed: %s', e,
                         exc_info=True)
    return stats


# ── Scheduling on the shared local-endpoint monitor ──────────────────────

def _inactive_poll_stats(reason: str) -> dict:
    return {reason: True, 'scheduled': False, 'next_poll_s': None}


def poll_if_due(*, now: float | None = None, discover=None,
                boundary=None, repository=None) -> dict:
    """Run one due catalog-sync pass from the shared monitor thread."""
    global _next_sync_at, _force_sync

    if _disabled():
        return _inactive_poll_stats('disabled')
    clock_now = time.monotonic() if now is None else float(now)
    with _schedule_lock:
        if not _runtime_enabled:
            return _inactive_poll_stats('stopped')
        if _next_sync_at is None:
            _next_sync_at = clock_now + _BOOT_DELAY
        if clock_now < _next_sync_at and not _force_sync:
            return {
                'scheduled': False,
                'next_poll_s': _next_sync_at - clock_now,
            }
        # Claim this pass before any network I/O so a concurrent caller
        # joins the next cadence instead of duplicating it.
        _force_sync = False
        _next_sync_at = clock_now + _SYNC_INTERVAL

    stats = sweep_once(discover=discover, boundary=boundary,
                       repository=repository)
    stats['scheduled'] = True
    stats['next_poll_s'] = _SYNC_INTERVAL
    return stats


def trigger_provider_catalog_sync() -> bool:
    """Request an immediate pass after an explicit provider/config change."""
    global _next_sync_at, _force_sync, _schedule_generation
    if _disabled():
        return False
    with _schedule_lock:
        if not _runtime_enabled:
            return False
        _schedule_generation += 1
        _force_sync = True
        _next_sync_at = 0.0
    try:
        from .health_local import wake_local_health_checker
        return wake_local_health_checker()
    except Exception as exc:
        logger.debug('[CatalogSync] shared-monitor wake failed: %s', exc)
        return False


def start_provider_catalog_sync() -> bool:
    """Enable catalog sync on the shared monitor (starts it if needed)."""
    global _runtime_enabled, _next_sync_at, _force_sync, _schedule_generation
    if _disabled():
        logger.info('[CatalogSync] disabled via TOFU_MODEL_CATALOG_SYNC=0')
        return False
    if _runtime_context() is None:
        logger.info(
            '[CatalogSync] disabled outside personal mode; distributed mode '
            'must enumerate owner boundaries explicitly')
        return False
    with _schedule_lock:
        was_enabled = _runtime_enabled
        _runtime_enabled = True
        _schedule_generation += 1
        _force_sync = False
        _next_sync_at = time.monotonic() + _BOOT_DELAY
    from .health_local import (
        start_local_health_checker,
        wake_local_health_checker,
    )
    started = start_local_health_checker()
    wake_local_health_checker()
    return bool(started or not was_enabled)


def stop_provider_catalog_sync() -> bool:
    """Disable catalog sync; the shared monitor retains its own lifecycle."""
    global _runtime_enabled, _next_sync_at, _force_sync, _schedule_generation
    with _schedule_lock:
        _runtime_enabled = False
        _schedule_generation += 1
        _force_sync = False
        _next_sync_at = None
    _failure_counts.clear()
    try:
        from .health_local import wake_local_health_checker
        wake_local_health_checker()
    except Exception as exc:
        logger.debug('[CatalogSync] shared-monitor wake failed: %s', exc)
    return True
