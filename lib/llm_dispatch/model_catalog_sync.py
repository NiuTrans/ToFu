"""Keep configured remote-provider model catalogues current.

Provider templates are intentionally only a bootstrap: they contain useful
capability/pricing metadata, but a release-bundled list cannot know which
models a particular account can use today.  This worker periodically treats a
successful provider ``/models`` response as the availability authority:

* newly advertised models are added automatically;
* models missing from consecutive successful snapshots are retired;
* a failed/empty fetch never changes the last-good configured list;
* hand-added ``catalog_pinned`` entries are never retired.

The consecutive-snapshot rule matters.  A transiently incomplete gateway
response must not empty a user's picker, while an actually retired SKU should
still disappear without somebody editing JavaScript or JSON templates.

State lives beside each provider under ``model_catalog_sync`` so Settings can
show what happened and config export/import remains self-contained.  All
read-modify-writes use :func:`lib.json_store.update_json_atomic`; a short lease
also prevents multiple server processes from polling the same account at the
same time.
"""

from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from typing import Callable

from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger

from .discovery import discover_models, is_local_endpoint
from .model_entry import routing_group

logger = get_logger(__name__)

__all__ = [
    'reconcile_catalog_models',
    'start_model_catalog_sync',
    'stop_model_catalog_sync',
    'sync_once',
    'trigger_model_catalog_sync',
]


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError) as exc:
        logger.debug('[ModelCatalog] invalid %s; using %d: %s',
                     name, default, exc)
        return default


# Remote provider catalogues change much less often than local serving boxes.
# Six hours keeps a deployment current without turning /models into a noisy
# health check. A Settings save wakes the worker immediately.
SYNC_INTERVAL_S = _env_int('TOFU_MODEL_CATALOG_SYNC_INTERVAL', 6 * 3600, 60)
BOOT_DELAY_S = _env_int('TOFU_MODEL_CATALOG_SYNC_DELAY', 20, 0)
LEASE_S = _env_int('TOFU_MODEL_CATALOG_SYNC_LEASE', 120, 30)
REMOVE_AFTER = _env_int('TOFU_MODEL_CATALOG_REMOVE_AFTER', 2, 2)

_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_wake_event = threading.Event()
_stop_event = threading.Event()
_pending_lock = threading.Lock()
_pending_ids: set[str] = set()
_pending_all = False


def _disabled() -> bool:
    return os.environ.get('TOFU_MODEL_CATALOG_SYNC', '1').strip().lower() in (
        '0', 'false', 'no', 'off')


def _server_config_path() -> str:
    # Resolve lazily: tests and portable deployments patch lib's active config
    # path after this module is imported.
    import lib as _lib
    return _lib._SERVER_CONFIG_PATH


def _sync_state(provider: dict) -> dict:
    raw = provider.get('model_catalog_sync')
    if raw is False:
        return {'mode': 'manual'}
    return dict(raw) if isinstance(raw, dict) else {'mode': 'auto'}


def _is_managed_provider(provider: dict) -> bool:
    return bool(
        provider.get('oauth') in ('claude', 'codex')
        or provider.get('managed_oauth') is True
        or isinstance(provider.get('adapter'), dict)
        or str(provider.get('id') or '').startswith('adapter_')
    )


def _eligible(provider: dict) -> bool:
    """Whether *provider* opts into the remote catalogue worker.

    Auto is the compatibility default, including for configurations created
    before ``model_catalog_sync`` existed. Local providers already have the
    richer multi-endpoint reconciler in ``health_local``; managed subscription
    catalogues are owned by their OAuth/adapter lifecycle.
    """
    if not isinstance(provider, dict) or provider.get('enabled', True) is False:
        return False
    if _sync_state(provider).get('mode', 'auto') != 'auto':
        return False
    if _is_managed_provider(provider) or provider.get('brand') == 'local':
        return False
    base_url = str(provider.get('base_url') or '').strip()
    if not base_url or is_local_endpoint(base_url):
        return False
    keys = provider.get('api_keys') or []
    return any(isinstance(k, str) and k.strip() for k in keys)


def _provider_signature(provider: dict) -> tuple:
    keys = provider.get('api_keys') or []
    first_key = next((k.strip() for k in keys
                      if isinstance(k, str) and k.strip()), '')
    # The key itself never enters status/logs. It only participates in this
    # in-memory stale-result fence.
    return (
        str(provider.get('base_url') or '').strip().rstrip('/'),
        str(provider.get('models_path') or '').strip(),
        first_key,
    )


def reconcile_catalog_models(existing: list, discovered: list,
                             pending_removals: dict | None = None,
                             *, remove_after: int = REMOVE_AFTER) -> dict:
    """Pure model-list reconciliation used by the worker and unit tests.

    Existing entries win for metadata and user overrides. A live wire ID also
    keeps its logical entry alive (``routing_group``), which is essential for
    gateways where ``model_id`` is a friendly logical name and only
    ``request_ids`` appear in ``/models``.
    """
    existing = [copy.deepcopy(m) for m in (existing or [])
                if isinstance(m, dict) and m.get('model_id')]
    discovered = [copy.deepcopy(m) for m in (discovered or [])
                  if isinstance(m, dict) and m.get('model_id')]
    misses = dict(pending_removals or {})
    live_by_id = {str(m['model_id']): m for m in discovered}
    uncovered = set(live_by_id)
    kept = []
    removed = []
    updated = []
    next_misses = {}

    for model in existing:
        mid = str(model['model_id'])
        live_matches = routing_group(model) & set(live_by_id)
        if live_matches:
            # One authenticated snapshot proves the logical entry is alive,
            # but cannot safely reshape a multi-key request_ids/key_access
            # pool: other keys may expose different concrete deployments.
            uncovered -= live_matches
            model['catalog_managed'] = True
            model.setdefault('catalog_source', 'provider')
            live = live_by_id[sorted(live_matches)[0]]
            incoming_profile = live.get('capability_profile')
            current_profile = model.get('capability_profile')
            operator_pinned = (
                isinstance(current_profile, dict)
                and current_profile.get('evidence') == 'operator')
            incoming_semantics = {
                k: v for k, v in (incoming_profile or {}).items()
                if k != 'updated_at'}
            current_semantics = {
                k: v for k, v in (current_profile or {}).items()
                if k != 'updated_at'}
            if (isinstance(incoming_profile, dict) and not operator_pinned
                    and incoming_semantics != current_semantics):
                model['capability_profile'] = copy.deepcopy(incoming_profile)
                updated.append(mid)
            kept.append(model)
            continue

        # Explicitly hand-managed rows survive even when the provider omits
        # them from its catalogue (private deployments commonly behave so).
        if model.get('catalog_pinned') is True:
            kept.append(model)
            continue

        count = int(misses.get(mid, 0) or 0) + 1
        if count >= max(2, int(remove_after)):
            removed.append(mid)
            continue
        next_misses[mid] = count
        kept.append(model)

    added = []
    for mid in sorted(uncovered, key=str.casefold):
        model = live_by_id[mid]
        model.setdefault('aliases', [])
        model['catalog_managed'] = True
        model['catalog_source'] = 'provider'
        kept.append(model)
        added.append(mid)

    return {
        'models': kept,
        'pending_removals': next_misses,
        'added': added,
        'removed': removed,
        'updated': updated,
    }


def _claim(provider_id: str, *, force: bool, now: float,
           config_path: str) -> dict | None:
    """Acquire one persisted per-provider polling lease."""
    claimed: dict = {}
    token = uuid.uuid4().hex

    def _mutate(cfg):
        if not isinstance(cfg, dict):
            return None
        for provider in (cfg.get('providers') or []):
            if not isinstance(provider, dict) or provider.get('id') != provider_id:
                continue
            if not _eligible(provider):
                return None
            state = _sync_state(provider)
            lease_until = float(state.get('lease_until') or 0)
            if lease_until > now:
                return None
            last_success = float(state.get('last_success_at') or 0)
            if not force and last_success and now - last_success < SYNC_INTERVAL_S:
                return None
            state.update({
                'mode': 'auto',
                'lease_until': now + LEASE_S,
                'claim_token': token,
                'last_attempt_at': now,
            })
            provider['model_catalog_sync'] = state
            claimed['provider'] = copy.deepcopy(provider)
            claimed['signature'] = _provider_signature(provider)
            claimed['token'] = token
            return cfg
        return None

    update_json_atomic(config_path, _mutate, default={})
    return claimed or None


def _finish_failure(provider_id: str, claim: dict, error: str, *, now: float,
                    config_path: str) -> None:
    def _mutate(cfg):
        if not isinstance(cfg, dict):
            return None
        for provider in (cfg.get('providers') or []):
            if provider.get('id') != provider_id:
                continue
            state = _sync_state(provider)
            if state.get('claim_token') != claim['token']:
                return None
            if _provider_signature(provider) != claim['signature']:
                return None
            state.update({
                'mode': 'auto',
                'last_error': str(error)[:300],
                'consecutive_failures': int(state.get('consecutive_failures') or 0) + 1,
                'last_finished_at': now,
                'lease_until': 0,
            })
            state.pop('claim_token', None)
            provider['model_catalog_sync'] = state
            return cfg
        return None

    update_json_atomic(config_path, _mutate, default={})


def _chat_replacement(models: list) -> str:
    for model in models or []:
        caps = set(model.get('capabilities') or ['text'])
        if model.get('enabled') is not False and 'text' in caps:
            return str(model.get('model_id') or '')
    return ''


def _repair_retired_references(cfg: dict, removed: set[str],
                               provider: dict) -> None:
    """Prevent defaults/presets from pointing at a globally retired ID."""
    if not removed:
        return
    available = {
        model_id
        for p in (cfg.get('providers') or []) if isinstance(p, dict)
        for m in (p.get('models') or []) if isinstance(m, dict) and m.get('model_id')
        for model_id in routing_group(m)
    }
    retired = removed - available
    if not retired:
        return
    replacement = _chat_replacement(provider.get('models') or [])
    if not replacement:
        replacement = next((
            _chat_replacement(p.get('models') or [])
            for p in (cfg.get('providers') or []) if isinstance(p, dict)
            if _chat_replacement(p.get('models') or [])
        ), '')

    presets = cfg.get('presets')
    if isinstance(presets, dict):
        for key, value in list(presets.items()):
            if not isinstance(value, str) or value not in retired:
                continue
            if key == 'opus' and replacement:
                presets[key] = replacement
            else:
                presets.pop(key, None)

    defaults = cfg.get('model_defaults')
    if isinstance(defaults, dict):
        if (isinstance(defaults.get('default_model'), str)
                and defaults.get('default_model') in retired):
            defaults['default_model'] = replacement
        if (isinstance(defaults.get('fallback_model'), str)
                and defaults.get('fallback_model') in retired):
            defaults['fallback_model'] = ''

    model_vars = cfg.get('models')
    if isinstance(model_vars, dict):
        for key, value in list(model_vars.items()):
            if isinstance(value, str):
                if value not in retired:
                    continue
                if key == 'LLM_MODEL' and replacement:
                    model_vars[key] = replacement
                else:
                    model_vars.pop(key, None)
            elif isinstance(value, list):
                filtered = [item for item in value
                            if not (isinstance(item, str) and item in retired)]
                if len(filtered) != len(value):
                    model_vars[key] = filtered


def _finish_success(provider_id: str, claim: dict, discovered: list,
                    *, now: float, remove_after: int,
                    config_path: str) -> dict | None:
    outcome: dict = {}

    def _mutate(cfg):
        if not isinstance(cfg, dict):
            return None
        for provider in (cfg.get('providers') or []):
            if provider.get('id') != provider_id:
                continue
            state = _sync_state(provider)
            if state.get('claim_token') != claim['token']:
                return None
            if _provider_signature(provider) != claim['signature']:
                return None
            reconciled = reconcile_catalog_models(
                provider.get('models') or [], discovered,
                state.get('pending_removals') or {},
                remove_after=remove_after,
            )
            provider['models'] = reconciled['models']
            state.update({
                'mode': 'auto',
                'last_success_at': now,
                'last_finished_at': now,
                'last_error': '',
                'consecutive_failures': 0,
                'lease_until': 0,
                'pending_removals': reconciled['pending_removals'],
                'last_added': reconciled['added'][:50],
                'last_removed': reconciled['removed'][:50],
                'last_updated': reconciled['updated'][:50],
                'catalog_size': len(discovered),
            })
            state.pop('claim_token', None)
            provider['model_catalog_sync'] = state
            _repair_retired_references(
                cfg, set(reconciled['removed']), provider)
            outcome.update(reconciled)
            return cfg
        return None

    update_json_atomic(config_path, _mutate, default={})
    return outcome or None


def _default_discover(provider: dict) -> list:
    keys = provider.get('api_keys') or []
    api_key = next((k.strip() for k in keys
                    if isinstance(k, str) and k.strip()), '')
    return discover_models(
        str(provider.get('base_url') or '').strip(), api_key,
        models_path=str(provider.get('models_path') or '').strip(),
    )


def sync_once(*, provider_ids: set[str] | None = None, force: bool = False,
              discover: Callable[[dict], list] | None = None,
              now: float | None = None, remove_after: int = REMOVE_AFTER,
              config_path: str | None = None,
              rebuild: Callable[[], None] | None = None) -> dict:
    """Run one remote-provider catalogue reconciliation pass.

    Dependency injection keeps unit tests off the network and away from the
    live dispatcher. ``provider_ids=None`` scans all eligible providers.
    """
    stats = {'providers': 0, 'succeeded': 0, 'failed': 0,
             'changed': 0, 'added': [], 'removed': [], 'updated': [],
             'errors': {}}
    if _disabled():
        stats['disabled'] = True
        return stats
    now = time.time() if now is None else float(now)
    config_path = config_path or _server_config_path()
    discover = discover or _default_discover
    cfg = read_json(config_path, default={})
    providers = cfg.get('providers') if isinstance(cfg, dict) else []
    candidates = [p for p in (providers or [])
                  if isinstance(p, dict) and _eligible(p)
                  and (provider_ids is None or p.get('id') in provider_ids)]
    stats['providers'] = len(candidates)

    for snapshot in candidates:
        provider_id = str(snapshot.get('id') or '')
        if not provider_id:
            continue
        claim = _claim(provider_id, force=force, now=now,
                       config_path=config_path)
        if not claim:
            continue
        try:
            models = discover(claim['provider'])
            if not isinstance(models, list) or not any(
                    isinstance(m, dict) and m.get('model_id') for m in models):
                raise RuntimeError('provider returned no usable models; keeping last-good catalogue')
            result = _finish_success(
                provider_id, claim, models, now=now,
                remove_after=remove_after, config_path=config_path)
            if result is None:
                continue  # provider changed/deleted while the request ran
            stats['succeeded'] += 1
            stats['added'].extend(result['added'])
            stats['removed'].extend(result['removed'])
            stats['updated'].extend(result['updated'])
            if result['added'] or result['removed'] or result['updated']:
                stats['changed'] += 1
                audit_log(
                    'provider_model_catalog_updated', provider_id=provider_id,
                    added=result['added'], removed=result['removed'],
                    updated=result['updated'])
                logger.info('[ModelCatalog] %s reconciled: +%d / -%d / ~%d',
                            provider_id, len(result['added']),
                            len(result['removed']), len(result['updated']))
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            _finish_failure(provider_id, claim, message, now=now,
                            config_path=config_path)
            stats['failed'] += 1
            stats['errors'][provider_id] = message[:300]
            logger.warning('[ModelCatalog] %s refresh failed: %s',
                           provider_id, message)

    if stats['changed']:
        try:
            if rebuild is not None:
                rebuild()
            else:
                # Catalogue retirement may also repair LLM_MODEL/presets.
                # Refresh lib's module-level defaults before rebuilding slots,
                # otherwise the dispatcher could keep routing the retired ID
                # until an unrelated Settings save or process restart.
                import lib as _lib
                from lib.llm_dispatch import reset_dispatcher
                _lib.reload_config()
                reset_dispatcher()
        except Exception as exc:
            logger.error('[ModelCatalog] runtime config/dispatcher reload failed: %s',
                         exc, exc_info=True)
    return stats


def trigger_model_catalog_sync(provider_ids: list[str] | set[str] | None = None) -> bool:
    """Wake the worker for an immediate forced pass after a Settings save."""
    global _pending_all
    if _disabled():
        return False
    # The real serving lifecycle owns worker startup. Do not make a config
    # write launch network threads in import-only tools or Flask unit tests.
    if _thread is None or not _thread.is_alive():
        return False
    with _pending_lock:
        if provider_ids is None:
            _pending_all = True
            _pending_ids.clear()
        elif not _pending_all:
            _pending_ids.update(str(x) for x in provider_ids if x)
    _wake_event.set()
    return True


def _take_pending() -> tuple[set[str] | None, bool]:
    global _pending_all
    with _pending_lock:
        if _pending_all:
            ids = None
            forced = True
        else:
            ids = set(_pending_ids) if _pending_ids else None
            forced = bool(_pending_ids)
        _pending_all = False
        _pending_ids.clear()
    return ids, forced


def _loop() -> None:
    logger.info('[ModelCatalog] worker started (interval=%ds, remove_after=%d)',
                SYNC_INTERVAL_S, REMOVE_AFTER)
    _wake_event.wait(BOOT_DELAY_S)
    _wake_event.clear()
    while not _stop_event.is_set():
        provider_ids, force = _take_pending()
        try:
            sync_once(provider_ids=provider_ids, force=force)
        except Exception as exc:
            logger.error('[ModelCatalog] sweep failed: %s', exc, exc_info=True)
        _wake_event.wait(SYNC_INTERVAL_S)
        _wake_event.clear()
    logger.info('[ModelCatalog] worker stopped')


def start_model_catalog_sync() -> bool:
    """Idempotently start the remote-provider catalogue worker."""
    global _thread
    if _disabled():
        logger.info('[ModelCatalog] disabled via TOFU_MODEL_CATALOG_SYNC=0')
        return False
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop_event.clear()
        _wake_event.clear()
        _thread = threading.Thread(
            target=_loop, name='model-catalog-sync', daemon=True)
        _thread.start()
    return True


def stop_model_catalog_sync(timeout: float = 2.0) -> bool:
    """Wake, signal and bounded-join the provider catalogue worker."""
    global _thread
    _stop_event.set()
    _wake_event.set()
    with _thread_lock:
        thread = _thread
    if thread is None:
        return True
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[ModelCatalog] invalid stop timeout; using 2.0: %s', exc)
        wait_seconds = 2.0
    if thread is not threading.current_thread():
        thread.join(timeout=wait_seconds)
    if thread.is_alive():
        return False
    with _thread_lock:
        if _thread is thread:
            _thread = None
    return True
