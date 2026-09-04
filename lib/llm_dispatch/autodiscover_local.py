"""lib/llm_dispatch/autodiscover_local.py — Auto-discovery of well-known local engine ports.

The shared local-endpoint monitor probes canonical loopback ports shortly after
startup and periodically. When an endpoint exposes models, this module sends
the observed facts to the owner-scoped model-routing v2 registration service;
it never constructs or writes a legacy ``server_config.providers`` row.

Safety rails
------------
* **Loopback only, three ports.** No subnet scanning, ever. The only
  extension is ``$OLLAMA_HOST`` (the operator's own declaration of where
  Ollama listens).
* **Cheap when absent.** A closed loopback port is a ~1 ms
  ``ECONNREFUSED``; absence is logged at DEBUG, never WARNING. Open endpoints
  with no models keep the cheap two-minute TCP check but exponentially back
  off full HTTP discovery to a bounded fifteen minutes.
* **Idempotent.** A port already covered by ANY provider's
  ``endpoints``/``base_url`` (any spelling of localhost) is skipped.
* **No zombies.** If the user deletes an auto-created Provider, the port moves
  to a per-owner ``dismissed`` list and is never re-added.
* **Explicit owner.** The production monitor runs only in personal mode. The
  callable sweep accepts an explicit owner/repository seam so a future
  distributed scheduler can enumerate owners without a module-global user.
* **Opt-out.** ``TOFU_LOCAL_AUTODISCOVER=0`` disables the worker and makes
  :func:`sweep_once` a no-op.
"""

import os
import socket
import threading
import time
from urllib.parse import urlparse

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger
from lib.model_routing import (
    ModelRoutingError,
    ModelRoutingRepository,
    OwnerBoundary,
    connection_urls,
    upsert_local_provider,
)
from lib.proxy import register_no_proxy_url

from .discovery import discover_models, normalize_base_url

logger = get_logger(__name__)

__all__ = [
    'WELL_KNOWN_ENGINES',
    'poll_if_due',
    'sweep_once',
    'start_local_autodiscovery',
    'stop_local_autodiscovery',
    'trigger_local_autodiscovery',
]


# Sole well-known-port authority. The retired browser presets no longer mirror
# this table; Settings renders any discovered ProviderAccess from v2.
WELL_KNOWN_ENGINES = (
    {'engine': 'ollama', 'name': 'Ollama', 'host': '127.0.0.1', 'port': 11434},
    {'engine': 'vllm',   'name': 'vLLM',   'host': '127.0.0.1', 'port': 8000},
    {'engine': 'sglang', 'name': 'SGLang', 'host': '127.0.0.1', 'port': 30000},
    {'engine': 'llamacpp', 'name': 'llama.cpp', 'host': '127.0.0.1', 'port': 8080},
)

_CONNECT_TIMEOUT = 1.0   # closed loopback port refuses instantly; this is the firewall-drop guard
_DISCOVER_TIMEOUT = 3    # loopback /models is local and fast
def _env_seconds(name: str, default: float, minimum: float,
                 maximum: float) -> float:
    """Read one bounded polling value without making import env-fragile."""
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    return min(maximum, max(minimum, value))


_BOOT_DELAY = _env_seconds(
    'TOFU_LOCAL_AUTODISCOVER_DELAY', 5.0, 0.0, 300.0)
_SWEEP_INTERVAL = _env_seconds(
    'TOFU_LOCAL_AUTODISCOVER_INTERVAL', 120.0, 10.0, 3600.0)
_MAX_PROBE_INTERVAL = max(
    _SWEEP_INTERVAL,
    _env_seconds(
        'TOFU_LOCAL_AUTODISCOVER_MAX_INTERVAL', 900.0,
        _SWEEP_INTERVAL, 3600.0),
)

_STATE_PATH = config_path('local_autodiscover.json')

# Auto-discovery is a cooperative job of health_local's single monitor thread,
# not a second resident worker. These fields hold only bounded in-process
# scheduling state. Provider authority is model-routing v2; the small per-owner
# dismissal ledger prevents deleted auto-providers from resurrecting.
_schedule_lock = threading.Lock()
_runtime_enabled = True
_next_sweep_at: float | None = None
_last_open_keys: set[str] = set()
_empty_keys: set[str] = set()
_probe_due_at: dict[str, float] = {}
_next_probe_delay: dict[str, float] = {}
_force_full_probe = False
_schedule_generation = 0


def _disabled() -> bool:
    return os.environ.get('TOFU_LOCAL_AUTODISCOVER', '1').strip().lower() in (
        '0', 'false', 'no', 'off')


def _port_key(host: str, port: int) -> str:
    """Canonical ``host:port`` identity — all loopback spellings fold together."""
    h = (host or '').strip().lower()
    if h in ('', 'localhost', '::1', '0.0.0.0', '::'):
        h = '127.0.0.1'
    return '%s:%d' % (h, int(port))


def _parse_host_port(raw: str, default_port: int) -> tuple:
    """Parse 'host[:port]' / 'scheme://host[:port]' → (host, port). ('', 0) on garbage."""
    raw = (raw or '').strip()
    if not raw:
        return '', 0
    if '://' not in raw:
        raw = 'http://' + raw
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or '').strip()
        port = parsed.port or default_port
    except (ValueError, TypeError) as e:
        logger.debug('[AutoDiscover] unparseable host:port %r: %s', raw, e)
        return '', 0
    return host, port


def _candidates() -> list:
    """Well-known ports, plus ``$OLLAMA_HOST`` when it points somewhere else."""
    rows = [dict(r) for r in WELL_KNOWN_ENGINES]
    ollama_env = os.environ.get('OLLAMA_HOST', '').strip()
    if ollama_env:
        host, port = _parse_host_port(ollama_env, 11434)
        if host:
            h, p = host, port
            if h.lower() in ('0.0.0.0', '::', 'localhost', '::1'):
                h = '127.0.0.1'
            rows.append({'engine': 'ollama', 'name': 'Ollama',
                         'host': h, 'port': p})
    seen = set()
    out = []
    for r in rows:
        key = _port_key(r['host'], r['port'])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _runtime_context() -> tuple[OwnerBoundary, ModelRoutingRepository] | None:
    """Return the personal monitor owner, or refuse implicit enumeration."""
    from runtime_guards import load_deployment_configuration

    deployment = load_deployment_configuration()
    if deployment.mode != 'personal':
        return None
    from lib.identity import PERSONAL_USER_ID

    return OwnerBoundary.create(PERSONAL_USER_ID), ModelRoutingRepository()


def _covered_port_keys(document: dict) -> set:
    """``host:port`` keys every configured v2 Connection points at."""
    keys = set()
    for urls in connection_urls(document).values():
        for raw in urls:
            norm = normalize_base_url(raw.strip())
            host, port = _parse_host_port(
                norm, 443 if norm.lower().startswith('https://') else 80)
            if host:
                keys.add(_port_key(host, port))
    return keys


def _state_owner_key(boundary: OwnerBoundary) -> str:
    return '%s:%d' % (boundary.tenant_id, boundary.owner_user_id)


def _normalized_state(value) -> dict:
    if not isinstance(value, dict):
        return {'added': {}, 'dismissed': []}
    added = value.get('added') if isinstance(value.get('added'), dict) else {}
    dismissed = (
        value.get('dismissed')
        if isinstance(value.get('dismissed'), list)
        else []
    )
    return {
        'added': {
            str(key): str(provider_id)
            for key, provider_id in added.items()
            if isinstance(key, str) and isinstance(provider_id, str)
        },
        'dismissed': [item for item in dismissed if isinstance(item, str)],
    }


def _load_state(boundary: OwnerBoundary) -> dict:
    st = read_json(_STATE_PATH, default=None)
    if not isinstance(st, dict):
        return {'added': {}, 'dismissed': []}
    owners = st.get('owners')
    if isinstance(owners, dict):
        return _normalized_state(owners.get(_state_owner_key(boundary)))
    # One-time personal-mode read compatibility. Saving rewrites this old
    # unscoped shape under the explicit owner key.
    return _normalized_state(st)


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
        logger.warning('[AutoDiscover] state persist failed: %s', e)


def _port_open(host: str, port: int, timeout: float = _CONNECT_TIMEOUT) -> bool:
    """Cheap TCP probe — the closed-port fast path that keeps sweeps silent."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as e:
        logger.debug('[AutoDiscover] %s:%d closed: %s', host, port, e)
        return False


def _discover(base_url: str):
    """``(models, effective_base_url)`` via the shared discovery machinery."""
    register_no_proxy_url(base_url)
    return discover_models(base_url, '', timeout=_DISCOVER_TIMEOUT,
                           return_effective=True, quiet_not_found=True)


def _provider_id(candidate: dict) -> str:
    return 'auto_%s_%d' % (candidate['engine'], candidate['port'])


def _rebuild_slots() -> None:
    from .health_local import _rebuild_dispatcher_slots
    _rebuild_dispatcher_slots()


def sweep_once(
    port_open=None,
    discover=None,
    rebuild=None,
    probe_due=None,
    *,
    boundary: OwnerBoundary | None = None,
    repository=None,
) -> dict:
    """One auto-discovery pass over the well-known ports.

    Dependency-injected seams (``port_open`` / ``discover`` / ``rebuild``)
    keep the tests off the network. ``probe_due(key)`` may defer only the full
    HTTP model query after an open TCP result; it never suppresses the cheap
    topology check. Returns a stats dict.
    """
    stats = {
        'scanned': 0,
        'open': [],
        'probed': 0,
        'probed_keys': [],
        'deferred': [],
        'empty': [],
        'failed': [],
        'unpersisted': [],
        'added': [],
        'authority_loaded': False,
    }
    if _disabled():
        stats['disabled'] = True
        return stats
    port_open = port_open or _port_open
    discover = discover or _discover
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
        logger.warning('[AutoDiscover] cannot load model-routing authority: %s', e)
        return stats
    if authority.revision <= 0:
        stats['authority_inactive'] = True
        return stats
    stats['authority_loaded'] = True
    covered = _covered_port_keys(authority.document)
    state = _load_state(boundary)

    # ── Reconcile deletions: an auto-added provider whose id vanished from
    # the aggregate was removed BY THE USER — mark the port dismissed so the
    # provider never resurrects. Disabled-but-present still counts as covered.
    provider_ids = {
        row.get('provider_id')
        for row in authority.document['providers']
        if isinstance(row, dict)
    }
    gone = [k for k, pid in state['added'].items() if pid not in provider_ids]
    if gone:
        for key in gone:
            state['added'].pop(key, None)
            if key not in covered and key not in state['dismissed']:
                state['dismissed'].append(key)
        _save_state(boundary, state)
        logger.info('[AutoDiscover] %d auto provider(s) deleted by user — '
                    'dismissed: %s', len(gone), sorted(gone))

    dismissed = set(state['dismissed'])

    for cand in _candidates():
        key = _port_key(cand['host'], cand['port'])
        stats['scanned'] += 1
        if key in covered or key in dismissed:
            continue
        try:
            if not port_open(cand['host'], cand['port']):
                continue
            stats['open'].append(key)
            if probe_due is not None and not probe_due(key):
                stats['deferred'].append(key)
                continue
            stats['probed'] += 1
            stats['probed_keys'].append(key)
            # All three well-known engines expose the OpenAI-compatible
            # catalogue at /v1/models. Starting at /v1 avoids the guaranteed
            # bare-origin 404 + retry that doubled every successful probe.
            base = 'http://%s:%d/v1' % (cand['host'], cand['port'])
            models, effective = discover(base)
        except Exception as e:
            # One misbehaving engine must not mask the others.
            logger.warning('[AutoDiscover] probe of %s failed: %s', key, e)
            stats['failed'].append(key)
            continue
        if not models:
            # Engine answers but serves nothing yet (no model pulled). Not
            # an error and NOT dismissed — the next sweep re-probes, so a
            # model pulled later still appears automatically.
            stats['empty'].append(key)
            logger.debug('[AutoDiscover] %s answers but serves no models', key)
            continue
        provider_id = _provider_id(cand)
        try:
            mutation = upsert_local_provider(
                repository,
                boundary,
                provider_id=provider_id,
                display_name='%s (auto)' % cand['name'],
                base_url=effective,
                models=models,
                require_unclaimed_connection=True,
            )
        except ModelRoutingError as e:
            logger.error(
                '[AutoDiscover] model-routing persist failed for %s: %s',
                provider_id, e, exc_info=True)
            stats['unpersisted'].append(key)
            continue
        if not mutation.changed:
            # A concurrent owner edit claimed the endpoint before our CAS.
            # It is already covered, so discovery has nothing to publish.
            continue
        state['added'][key] = provider_id
        _save_state(boundary, state)
        covered.add(key)
        stats['added'].append({
            'engine': cand['engine'], 'endpoint': effective,
            'n_models': len(models), 'provider_id': provider_id, 'key': key,
            'model_routing_revision': mutation.authority.revision,
        })
        logger.info('[AutoDiscover] %s (%s) serves %d model(s) — provider %r '
                    'auto-configured', cand['name'], effective, len(models),
                    provider_id)
        audit_log('local_provider_autodiscovered', engine=cand['engine'],
                  endpoint=effective, n_models=len(models),
                  provider_id=provider_id,
                  owner_user_id=boundary.owner_user_id,
                  model_routing_revision=mutation.authority.revision)

    if stats['added']:
        try:
            (rebuild or _rebuild_slots)()
        except Exception as e:
            logger.error('[AutoDiscover] slot rebuild failed: %s', e, exc_info=True)
    return stats


def _inactive_poll_stats(reason: str) -> dict:
    return {
        reason: True,
        'scheduled': False,
        'next_poll_s': None,
    }


def _remaining_poll_delay_locked(clock_now: float) -> float:
    deadline = clock_now if _next_sweep_at is None else _next_sweep_at
    return max(0.0, deadline - clock_now)


def _record_poll_result(stats: dict, *, clock_now: float, generation: int,
                        previous_empty: set[str]) -> dict:
    """Commit one claimed sweep unless a newer wake/stop generation won."""
    global _last_open_keys, _empty_keys

    with _schedule_lock:
        # A Settings wake or stop that landed during network I/O owns the next
        # generation. Never let this stale completion erase its immediate run.
        if generation != _schedule_generation:
            stats['next_poll_s'] = _remaining_poll_delay_locked(clock_now)
            return stats
        if not stats.get('authority_loaded'):
            stats['next_poll_s'] = _remaining_poll_delay_locked(clock_now)
            return stats

        open_keys = set(stats['open'])
        probed_keys = set(stats['probed_keys'])
        added_keys = {row['key'] for row in stats['added']}

        for key in set(_probe_due_at) - open_keys:
            _probe_due_at.pop(key, None)
            _next_probe_delay.pop(key, None)
        for key in probed_keys:
            if key in added_keys:
                _probe_due_at.pop(key, None)
                _next_probe_delay.pop(key, None)
                continue
            delay = max(
                _SWEEP_INTERVAL,
                _next_probe_delay.get(key, _SWEEP_INTERVAL),
            )
            _probe_due_at[key] = clock_now + delay
            _next_probe_delay[key] = min(
                _MAX_PROBE_INTERVAL, max(_SWEEP_INTERVAL, delay * 2.0))

        current_empty = previous_empty & open_keys
        current_empty -= probed_keys
        current_empty.update(stats['empty'])
        newly_empty = sorted(current_empty - previous_empty)
        _empty_keys = current_empty
        _last_open_keys = open_keys
        stats['next_poll_s'] = _remaining_poll_delay_locked(clock_now)

    if newly_empty:
        logger.info(
            '[AutoDiscover] %s answers but serves no models; full HTTP probes '
            'back off to <=%.0fs while TCP topology checks remain %.0fs',
            ', '.join(newly_empty), _MAX_PROBE_INTERVAL, _SWEEP_INTERVAL)
    return stats


def poll_if_due(*, now: float | None = None, port_open=None,
                discover=None, rebuild=None, boundary=None, repository=None) -> dict:
    """Run one due topology sweep from the shared local-endpoint monitor.

    TCP topology remains sampled every ``_SWEEP_INTERVAL`` so a newly started
    engine is discovered promptly. Only repeated full HTTP probes of an
    already-open, still-empty endpoint back off: 2m -> 4m -> 8m -> 15m by
    default. The state is process-local, bounded by the three candidate keys,
    and reset immediately by a topology change or explicit trigger.
    """
    global _next_sweep_at, _last_open_keys, _empty_keys
    global _force_full_probe

    if _disabled():
        return _inactive_poll_stats('disabled')
    clock_now = time.monotonic() if now is None else float(now)
    with _schedule_lock:
        if not _runtime_enabled:
            return _inactive_poll_stats('stopped')
        if _next_sweep_at is None:
            _next_sweep_at = clock_now + _BOOT_DELAY
        if clock_now < _next_sweep_at and not _force_full_probe:
            return {
                'scheduled': False,
                'next_poll_s': _next_sweep_at - clock_now,
            }
        generation = _schedule_generation
        force_full = _force_full_probe
        _force_full_probe = False
        previous_open = set(_last_open_keys)
        previous_empty = set(_empty_keys)
        due_at = dict(_probe_due_at)
        # Claim this topology sweep before any network I/O so an accidental
        # concurrent caller joins the next cadence instead of duplicating it.
        _next_sweep_at = clock_now + _SWEEP_INTERVAL

    def _probe_is_due(key: str) -> bool:
        return (
            force_full
            or key not in previous_open
            or clock_now >= due_at.get(key, 0.0)
        )

    stats = sweep_once(
        port_open=port_open,
        discover=discover,
        rebuild=rebuild,
        probe_due=_probe_is_due,
        boundary=boundary,
        repository=repository,
    )
    stats['scheduled'] = True
    return _record_poll_result(
        stats,
        clock_now=clock_now,
        generation=generation,
        previous_empty=previous_empty,
    )


def trigger_local_autodiscovery(*, reset_backoff: bool = True) -> bool:
    """Request an immediate pass after an explicit provider/config change."""
    global _next_sweep_at, _force_full_probe, _schedule_generation
    if _disabled():
        return False
    with _schedule_lock:
        if not _runtime_enabled:
            return False
        _schedule_generation += 1
        _force_full_probe = True
        _next_sweep_at = 0.0
        if reset_backoff:
            _probe_due_at.clear()
            _next_probe_delay.clear()
    try:
        from .health_local import wake_local_health_checker
        return wake_local_health_checker()
    except Exception as exc:
        logger.debug('[AutoDiscover] shared-monitor wake failed: %s', exc)
        return False


def start_local_autodiscovery() -> bool:
    """Compatibility entry point: enable discovery on the shared monitor."""
    global _runtime_enabled, _next_sweep_at, _force_full_probe
    global _schedule_generation
    if _disabled():
        logger.info('[AutoDiscover] disabled via TOFU_LOCAL_AUTODISCOVER=0')
        return False
    if _runtime_context() is None:
        logger.info(
            '[AutoDiscover] disabled outside personal mode; distributed mode '
            'must enumerate owner boundaries explicitly')
        return False
    with _schedule_lock:
        was_enabled = _runtime_enabled
        _runtime_enabled = True
        _schedule_generation += 1
        _force_full_probe = False
        _next_sweep_at = time.monotonic() + _BOOT_DELAY
    from .health_local import (
        start_local_health_checker,
        wake_local_health_checker,
    )
    started = start_local_health_checker()
    wake_local_health_checker()
    return bool(started or not was_enabled)


def stop_local_autodiscovery(timeout: float = 2.0) -> bool:
    """Disable discovery; the shared health monitor retains its own lifecycle."""
    del timeout  # retained for compatibility with the former worker API
    global _runtime_enabled, _next_sweep_at, _force_full_probe
    global _schedule_generation
    with _schedule_lock:
        _runtime_enabled = False
        _schedule_generation += 1
        _force_full_probe = False
        _next_sweep_at = None
        _last_open_keys.clear()
        _empty_keys.clear()
        _probe_due_at.clear()
        _next_probe_delay.clear()
    try:
        from .health_local import wake_local_health_checker
        wake_local_health_checker()
    except Exception as exc:
        logger.debug('[AutoDiscover] shared-monitor wake failed: %s', exc)
    return True
