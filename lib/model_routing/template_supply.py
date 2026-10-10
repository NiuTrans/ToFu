"""Converge installed provider accesses with their source templates.

Provider templates are curated supply lists (for the YourProvider gateway:
ModelPlaza ∩ live probe), but they only seed NEW installs — a card added
last month never sees newly listed models, and gateways without a usable
``/models`` listing (your-llm-gateway.example.com returns an empty list) give
``provider_catalog_sync`` nothing to discover. This sync closes the gap:
riding health_local's shared monitor, it periodically converges every
installed access that fingerprint-matches a template toward a fresh
install of that template. Opt out with ``TOFU_TEMPLATE_SUPPLY_SYNC=0``.

Semantics:

* **Fingerprint match** — every template base URL (top-level + protocol
  faces) must appear among the access's connections, AND the access must
  already relate to the template (provider brand match or ≥1 shared
  recipe model), so a hand-built minimal access to the same host is never
  flooded.
* **Probe-gated** — a missing recipe is added only when a live probe on
  the access's own credential proves the gateway serves it (``ok`` /
  ``rate_limited``, the same served verdicts template curation uses),
  routed per recipe through ``build_probe_work`` so a Claude candidate is
  probed on the Anthropic face exactly like dispatch would. Ids the owner
  cleaned up because they probed dead never resurrect; transient probe
  failures simply retry on the next boot.
* **Insert-only** — existing offerings, deployments, credentials and
  owner edits are never modified or removed. New rows reuse the compiled
  template bundle verbatim (identity, capabilities, context window) with
  ids re-derived for the target access and deployments re-pointed at the
  access's own connections. Confirmed model references are appended to
  the access credentials' authorization so dispatch can actually route
  them.
  Registry model rows converge upward (capabilities union, maximum
  context window) — the same convergence the domain alias merge applies
  to survivor rows — so a transplanted offering never out-claims the
  official row it validates against.
"""

from __future__ import annotations

import copy
import os
import threading
from typing import Any

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

_SERVED_VERDICTS = frozenset({'ok', 'rate_limited'})
_PROBE_WORKERS = 8
_PROBE_TIMEOUT = 20

__all__ = [
    'poll_if_due',
    'start_template_supply_sync',
    'stop_template_supply_sync',
    'sync_template_supply',
    'trigger_template_supply_sync',
]


def _env_seconds(name: str, default: float, minimum: float,
                 maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


_BOOT_DELAY = _env_seconds(
    'TOFU_TEMPLATE_SUPPLY_SYNC_BOOT_DELAY', 240.0, 30.0, 3600.0)
_SYNC_INTERVAL = _env_seconds(
    'TOFU_TEMPLATE_SUPPLY_SYNC_INTERVAL', 6.0 * 3600.0, 900.0, 86400.0)

_schedule_lock = threading.Lock()
_runtime_enabled = True
_next_sync_at: float | None = None
_force_sync = False
_schedule_generation = 0


def _disabled() -> bool:
    return os.environ.get('TOFU_TEMPLATE_SUPPLY_SYNC', '').strip() == '0'


def _norm_url(value: Any) -> str:
    return str(value or '').strip().rstrip('/')


def _template_urls(template: dict) -> set[str]:
    urls = {_norm_url(template.get('base_url'))}
    faces = template.get('faces') or {}
    if isinstance(faces, dict):
        for face in faces.values():
            if isinstance(face, dict):
                urls.add(_norm_url(face.get('base_url')))
    return {url for url in urls if url}


def _access_connections(document: dict, access_id: str) -> list[dict]:
    return [
        row for row in document.get('connections') or []
        if isinstance(row, dict)
        and row.get('provider_access_id') == access_id
    ]


def _recipe_keys(recipe: dict) -> frozenset:
    from lib.model_catalog._creator_identity import merge_keys

    keys = set(merge_keys(recipe.get('model_id')))
    for wire in recipe.get('request_ids') or []:
        keys.update(merge_keys(wire))
    return frozenset(keys)


def _known_keys(document: dict, access_id: str) -> frozenset:
    """Every model identity the access already serves, in any state."""
    from lib.model_catalog._creator_identity import merge_keys

    keys: set = set()
    for row in document.get('offerings') or []:
        if not isinstance(row, dict):
            continue
        if row.get('provider_access_id') != access_id:
            continue
        model = row.get('model')
        if isinstance(model, dict):
            keys.update(merge_keys(model.get('model_id')))
        pending = str(row.get('pending_model_id') or '').strip()
        if pending:
            keys.update(merge_keys(pending))
    return frozenset(keys)


def _known_wires(document: dict, access_id: str) -> set[str]:
    connection_ids = {
        row.get('connection_id') for row in _access_connections(document, access_id)
    }
    return {
        str(row.get('wire_model_id') or '').strip()
        for row in document.get('deployments') or []
        if isinstance(row, dict) and row.get('connection_id') in connection_ids
    } - {''}


def _find_template(document: dict, access: dict,
                   templates: list[dict]) -> dict | None:
    """First template whose URLs the access fully hosts and which the
    access provably relates to (brand or ≥1 shared recipe identity)."""
    access_id = access['provider_access_id']
    access_urls = {
        _norm_url(row.get('base_url'))
        for row in _access_connections(document, access_id)
    } - {''}
    if not access_urls:
        return None
    provider = next(
        (row for row in document.get('providers') or []
         if row.get('provider_id') == access.get('provider_id')),
        {},
    )
    brand = str(provider.get('brand') or '').strip()
    known = _known_keys(document, access_id)
    for template in templates:
        template_urls = _template_urls(template)
        if not template_urls or not template_urls <= access_urls:
            continue
        recipes = template.get('offering_recipes') or []
        if not recipes:
            continue
        if brand and brand == str(template.get('brand') or '').strip():
            return template
        if any(_recipe_keys(recipe) & known for recipe in recipes):
            return template
    return None


def _resolve_api_key(repository, boundary, document: dict,
                     access_id: str) -> tuple[str, dict]:
    """First enabled credential secret of the access → (api_key, headers)."""
    from lib.model_routing.dispatch_adapter import decode_credential_secret

    for credential in document.get('credentials') or []:
        if not isinstance(credential, dict):
            continue
        if credential.get('provider_access_id') != access_id:
            continue
        if not credential.get('enabled'):
            continue
        reference = str(credential.get('secret_reference') or '')
        if not reference:
            continue
        plaintext = repository.resolve_secret(boundary, reference)
        api_key, _oauth, headers = decode_credential_secret(
            plaintext, kind=str(credential.get('kind') or 'api_key'))
        if api_key:
            return api_key, dict(headers or {})
    return '', {}


def _live_prober(*, base_url: str, protocol: str, faces: dict,
                 api_key: str, extra_headers: dict,
                 recipes: list[dict]) -> dict[str, str]:
    """Probe each recipe's wire pool; return model_id → verdict."""
    from lib.provider_probe import build_probe_work, probe_one_cell

    provider = {'base_url': base_url, 'protocol': protocol, 'faces': faces}
    models = [
        {
            'model_id': recipe['model_id'],
            'capabilities': [],
            **({'request_ids': list(recipe['request_ids'])}
               if recipe.get('request_ids') else {}),
        }
        for recipe in recipes
    ]
    work = build_probe_work(provider, models, [api_key])
    by_root: dict[str, str] = {}

    def _run(item):
        _idx, key, root, wire, _caps, url, proto = item
        return root, probe_one_cell(
            url, key, wire, extra_headers, _PROBE_TIMEOUT, protocol=proto)

    from lib.observability import InstrumentedThreadPoolExecutor
    with InstrumentedThreadPoolExecutor(max_workers=_PROBE_WORKERS,
            metric_pool='template-supply', thread_name_prefix='template-supply') as pool:
        futures = []
        for item in work:
            try:
                futures.append((item[2], pool.submit(_run, item)))
            except Exception:
                # This cell never started. No serving evidence exists, but
                # accepted sibling probes retain ownership and still finish.
                by_root.setdefault(item[2], 'unknown')
        for root, future in futures:
            try:
                _root, (verdict, _detail) = future.result()
            except Exception:
                verdict = 'unknown'
            previous = by_root.get(root)
            if previous in _SERVED_VERDICTS:
                continue
            if verdict in _SERVED_VERDICTS or previous is None:
                by_root[root] = verdict
    return by_root


def _probe_plan(document: dict, access: dict,
                template: dict) -> tuple[str, str, dict, dict]:
    """(base_url, protocol, faces, headers) for probing this access."""
    access_id = access['provider_access_id']
    connections = _access_connections(document, access_id)
    by_url = {_norm_url(row.get('base_url')): row for row in connections}
    template_base = _norm_url(template.get('base_url'))
    primary = by_url.get(template_base) or (connections[0] if connections else {})

    faces: dict[str, dict] = {}
    raw_faces = template.get('faces') or {}
    if isinstance(raw_faces, dict):
        for name, face in raw_faces.items():
            if not isinstance(face, dict):
                continue
            matched = by_url.get(_norm_url(face.get('base_url')))
            faces[name] = {
                **face,
                'base_url': (matched or face).get('base_url'),
            }

    headers: dict = {}
    for row in connections:
        headers.update(row.get('extra_headers') or {})
    return (
        _norm_url(primary.get('base_url')),
        str(primary.get('protocol') or 'openai'),
        faces,
        headers,
    )


def _transplant(working: dict, access: dict, bundle: dict,
                verdicts: dict, stats: dict) -> bool:
    """Append the bundle's offerings/deployments for one access.

    Recomputes dedupe against the working copy so CAS retries stay
    idempotent. Returns True when anything was appended.
    """
    from lib.model_catalog._creator_identity import merge_keys
    from lib.model_routing.migration import _stable_id

    access_id = access['provider_access_id']
    known = set(_known_keys(working, access_id))
    wires = _known_wires(working, access_id)
    offering_ids = {
        row.get('offering_id')
        for row in working.get('offerings') or []
    }
    deployment_ids = {
        row.get('deployment_id')
        for row in working.get('deployments') or []
    }
    connections = _access_connections(working, access_id)
    by_url = {_norm_url(row.get('base_url')): row for row in connections}
    bundle_connections = {
        row['connection_id']: row for row in bundle['connections']
    }
    model_rows = {
        (row.get('creator_id'), row.get('model_id')): row
        for row in bundle['models']
    }
    creator_rows = {row['creator_id']: row for row in bundle['creators']}
    appended = False

    for source in bundle['offerings']:
        model = source.get('model') if isinstance(source.get('model'), dict) else None
        pending = str(source.get('pending_model_id') or '').strip()
        identity = (
            f"{model['creator_id']}/{model['model_id']}" if model
            else f'pending:{pending}')
        identity_keys = merge_keys(
            model.get('model_id') if model else pending)
        if identity_keys & known:
            continue
        offering_id = _stable_id('off', access_id, identity)
        if offering_id in offering_ids:
            continue
        verdict = verdicts.get(model['model_id'] if model else pending, '')

        deployments: list[dict] = []
        for dep in bundle['deployments']:
            if dep.get('offering_id') != source['offering_id']:
                continue
            wire = str(dep.get('wire_model_id') or '').strip()
            if not wire or wire in wires:
                continue
            bundle_conn = bundle_connections.get(dep.get('connection_id')) or {}
            target = by_url.get(_norm_url(bundle_conn.get('base_url')))
            if target is None:
                target = next(
                    (row for row in connections
                     if row.get('protocol') == bundle_conn.get('protocol')
                     and row.get('enabled')),
                    None,
                )
            if target is None:
                continue
            deployment_id = _stable_id(
                'dep', access_id, target['connection_id'], wire)
            if deployment_id in deployment_ids:
                continue
            deployments.append({
                **copy.deepcopy(dep),
                'deployment_id': deployment_id,
                'offering_id': offering_id,
                'connection_id': target['connection_id'],
                'wire_model_id': wire,
                # Only a measured successful inference is passed evidence.
                # Diagnostics do not control route enablement.
                'probe_status': 'passed' if verdict == 'ok' else 'unprobed',
            })
        if not deployments:
            logger.warning(
                'template supply: %s has no routable connection for %s — skipped',
                access_id, identity)
            continue

        offering = {
            **copy.deepcopy(source),
            'offering_id': offering_id,
            'provider_access_id': access_id,
        }
        working['offerings'].append(offering)
        working['deployments'].extend(deployments)
        offering_ids.add(offering_id)
        deployment_ids.update(row['deployment_id'] for row in deployments)
        wires.update(row['wire_model_id'] for row in deployments)
        known.update(identity_keys)
        appended = True
        stats['added_offerings'] += 1
        stats['added_deployments'] += len(deployments)
        stats['accesses'].setdefault(access_id, []).append(
            model['model_id'] if model else pending)

        if model is None:
            continue
        row = model_rows.get((model['creator_id'], model['model_id']))
        if row is not None:
            existing = next(
                (entry for entry in working.get('models') or []
                 if entry.get('creator_id') == row.get('creator_id')
                 and entry.get('model_id') == row.get('model_id')),
                None)
            if existing is None:
                # Append even when the row respells an already-registered
                # trained model (dated snapshot, relay SKU): normalize's
                # alias merge absorbs it and rewrites offering/credential
                # refs onto the survivor row, so every appended ref stays
                # resolvable.
                working['models'].append(copy.deepcopy(row))
            else:
                # Recheck the current owner baseline on every CAS attempt;
                # a stale compiled template may not expand an owner edit.
                from lib.llm_dispatch.config._pricing import MANAGED_TIER_TAGS
                offering['capabilities'] = sorted(set(offering['capabilities']) & (
                    set(existing['capabilities']) | MANAGED_TIER_TAGS))
                offering['context_window'] = min(offering['context_window'], existing['context_window'])
        creator = creator_rows.get(model['creator_id'])
        if creator is not None and not any(
                row.get('creator_id') == model['creator_id']
                for row in working.get('creators') or []):
            working['creators'].append(copy.deepcopy(creator))
        ref = {'creator_id': model['creator_id'], 'model_id': model['model_id']}
        for credential in working.get('credentials') or []:
            if credential.get('provider_access_id') != access_id:
                continue
            authorized = credential.setdefault('authorization', {}).setdefault(
                'models', [])
            if not any(all(grant[key] == value for key, value in ref.items()) for grant in authorized):
                authorized.append(dict(ref))
    return appended


def sync_template_supply(*, boundary=None, repository=None,
                         prober=None, templates=None) -> dict:
    """One convergence pass over every template-matched installed access."""
    if boundary is None or repository is None:
        from lib.llm_dispatch.autodiscover_local import _runtime_context
        context = _runtime_context()
        if context is None:
            return {'added_offerings': 0, 'added_deployments': 0,
                    'accesses': {}, 'skipped': 'no_owner'}
        boundary, repository = context

    from lib.provider_template_recipes import (
        compile_provider_template_bundle, load_provider_templates)

    if templates is None:
        templates = [
            row for row in load_provider_templates()
            if row.get('category') != 'local'
            and (row.get('offering_recipes') or [])
        ]
    prober = prober or _live_prober

    document = repository.get(boundary).document
    stats: dict[str, Any] = {
        'added_offerings': 0,
        'added_deployments': 0,
        'accesses': {},
    }
    plans: list[tuple[dict, dict, dict]] = []
    for access in document.get('provider_accesses') or []:
        if not isinstance(access, dict):
            continue
        template = _find_template(document, access, templates)
        if template is None:
            continue
        known = _known_keys(document, access['provider_access_id'])
        wires = _known_wires(document, access['provider_access_id'])
        missing = [
            recipe for recipe in template['offering_recipes']
            if not (_recipe_keys(recipe) & known)
            and not (set(recipe.get('request_ids') or ()) & wires)
        ]
        if not missing:
            continue
        try:
            api_key, secret_headers = _resolve_api_key(
                repository, boundary, document, access['provider_access_id'])
        except Exception as exc:
            logger.warning(
                'template supply: credential resolve failed for %s: %s',
                access['provider_access_id'], exc)
            continue
        if not api_key:
            logger.info(
                'template supply: %s has no usable api-key credential — skipped',
                access['provider_access_id'])
            continue
        base_url, protocol, faces, headers = _probe_plan(
            document, access, template)
        headers.update(secret_headers)
        try:
            verdicts = prober(
                base_url=base_url, protocol=protocol, faces=faces,
                api_key=api_key, extra_headers=headers, recipes=missing)
        except Exception as exc:
            logger.warning(
                'template supply: probe failed for %s: %s',
                access['provider_access_id'], exc)
            continue
        served = sorted(
            recipe['model_id'] for recipe in missing
            if verdicts.get(recipe['model_id']) in _SERVED_VERDICTS)
        if not served:
            logger.info(
                'template supply: %s — %d candidate recipes probed dead',
                access['provider_access_id'], len(missing))
            continue
        try:
            bundle = compile_provider_template_bundle(
                template['key'], selected_model_ids=served,
                official_directory=document['models'])
        except Exception as exc:
            logger.warning(
                'template supply: compile failed for %s: %s',
                template['key'], exc)
            continue
        plans.append((access, bundle, verdicts))

    if not plans:
        stats['revision'] = repository.get(boundary).revision
        return stats

    def _mutate(working: dict):
        appended = False
        for access, bundle, verdicts in plans:
            current = next(
                (row for row in working.get('provider_accesses') or []
                 if row.get('provider_access_id')
                 == access['provider_access_id']),
                None,
            )
            if current is None:
                continue
            appended = (
                _transplant(working, current, bundle, verdicts, stats)
                or appended)
        return working if appended else None

    authority = repository.mutate(boundary, _mutate)
    stats['revision'] = authority.revision
    logger.info('template supply sync: %s', stats)
    audit_log('provider_template_supply_synced',
              accesses={key: sorted(value)
                        for key, value in stats['accesses'].items()},
              added_offerings=stats['added_offerings'],
              added_deployments=stats['added_deployments'],
              owner_user_id=boundary.owner_user_id,
              model_routing_revision=authority.revision)
    try:
        from lib.llm_dispatch import reset_dispatcher
        reset_dispatcher()
    except Exception as exc:
        logger.error('template supply: dispatcher reset failed: %s', exc)
    return stats


# ── Scheduling on the shared local-endpoint monitor ──────────────────────

def _inactive_poll_stats(reason: str) -> dict:
    return {reason: True, 'scheduled': False, 'next_poll_s': None}


def poll_if_due(*, now: float | None = None, prober=None,
                boundary=None, repository=None) -> dict:
    """Run one due template-supply pass from the shared monitor thread."""
    global _next_sync_at, _force_sync
    import time

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

    stats = sync_template_supply(
        boundary=boundary, repository=repository, prober=prober)
    stats['scheduled'] = True
    stats['next_poll_s'] = _SYNC_INTERVAL
    return stats


def trigger_template_supply_sync() -> bool:
    """Request an immediate pass after a template install/update."""
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
        from lib.llm_dispatch.health_local import wake_local_health_checker
        return wake_local_health_checker()
    except Exception as exc:
        logger.debug('template supply: shared-monitor wake failed: %s', exc)
        return False


def start_template_supply_sync() -> bool:
    """Enable template-supply sync on the shared monitor."""
    global _runtime_enabled, _next_sync_at, _force_sync, _schedule_generation
    import time

    if _disabled():
        logger.info('template supply sync disabled via '
                    'TOFU_TEMPLATE_SUPPLY_SYNC=0')
        return False
    with _schedule_lock:
        was_enabled = _runtime_enabled
        _runtime_enabled = True
        _schedule_generation += 1
        _force_sync = False
        _next_sync_at = time.monotonic() + _BOOT_DELAY
    from lib.llm_dispatch.health_local import (
        start_local_health_checker,
        wake_local_health_checker,
    )
    started = start_local_health_checker()
    wake_local_health_checker()
    return bool(started or not was_enabled)


def stop_template_supply_sync() -> bool:
    """Disable template-supply sync; the shared monitor keeps running."""
    global _runtime_enabled, _next_sync_at, _force_sync, _schedule_generation
    with _schedule_lock:
        _runtime_enabled = False
        _schedule_generation += 1
        _force_sync = False
        _next_sync_at = None
    try:
        from lib.llm_dispatch.health_local import wake_local_health_checker
        wake_local_health_checker()
    except Exception as exc:
        logger.debug('template supply: shared-monitor wake failed: %s', exc)
    return True
