"""lib/llm_dispatch/autodiscover_local.py — Auto-discovery of well-known local engine ports.

The Settings flow for local engines is user-driven: pick a preset (vLLM /
SGLang / Ollama), paste a URL, probe. But the three engines have canonical
default ports, and a box on loopback costs one TCP connect to find. This
worker probes those ports shortly after startup (and periodically, so an
engine started AFTER Tofu — or a model pulled later — is still picked up)
and, when an endpoint answers with a non-empty model list, registers it as
a normal ``brand: 'local'`` provider in ``server_config.json``. From that
moment the regular machinery owns it: the dispatcher gets slots, the health
checker (``health_local.py``) tracks model drift, and the Settings UI
renders the card exactly like a user-created one (engine icon included).

Safety rails
------------
* **Loopback only, three ports.** No subnet scanning, ever. The only
  extension is ``$OLLAMA_HOST`` (the operator's own declaration of where
  Ollama listens).
* **Cheap when absent.** A closed loopback port is a ~1 ms
  ``ECONNREFUSED``; absence is logged at DEBUG, never WARNING.
* **Idempotent.** A port already covered by ANY provider's
  ``endpoints``/``base_url`` (any spelling of localhost) is skipped.
* **No zombies.** If the user deletes an auto-created provider, the port
  moves to a ``dismissed`` list in ``data/config/local_autodiscover.json``
  and is never re-added. Accidental resurrection after deletion is the
  classic failure mode of config auto-provisioning.
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
from lib.proxy import register_no_proxy_url

from .discovery import discover_models, normalize_base_url

logger = get_logger(__name__)

__all__ = [
    'WELL_KNOWN_ENGINES',
    'sweep_once',
    'start_local_autodiscovery',
]


# Mirrors the frontend `_LOCAL_ENGINE_PRESETS` (static/js/settings/
# local_endpoints.js) — a parity test keeps the two tables in sync.
WELL_KNOWN_ENGINES = (
    {'engine': 'ollama', 'name': 'Ollama', 'host': '127.0.0.1', 'port': 11434},
    {'engine': 'vllm',   'name': 'vLLM',   'host': '127.0.0.1', 'port': 8000},
    {'engine': 'sglang', 'name': 'SGLang', 'host': '127.0.0.1', 'port': 30000},
)

_CONNECT_TIMEOUT = 1.0   # closed loopback port refuses instantly; this is the firewall-drop guard
_DISCOVER_TIMEOUT = 3    # loopback /models is local and fast
_BOOT_DELAY = float(os.environ.get('TOFU_LOCAL_AUTODISCOVER_DELAY', '5'))
_SWEEP_INTERVAL = int(os.environ.get('TOFU_LOCAL_AUTODISCOVER_INTERVAL', '120'))

_STATE_PATH = config_path('local_autodiscover.json')

_thread = None
_stop_event = threading.Event()


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


def _covered_port_keys(providers) -> set:
    """``host:port`` keys every configured provider already points at."""
    keys = set()
    for prov in providers or []:
        if not isinstance(prov, dict):
            continue
        urls = []
        eps = prov.get('endpoints')
        if isinstance(eps, list):
            urls.extend(u for u in eps if isinstance(u, str) and u.strip())
        if prov.get('base_url'):
            urls.append(prov['base_url'])
        for raw in urls:
            norm = normalize_base_url(raw.strip())
            host, port = _parse_host_port(
                norm, 443 if norm.lower().startswith('https://') else 80)
            if host:
                keys.add(_port_key(host, port))
    return keys


def _load_state() -> dict:
    st = read_json(_STATE_PATH, default=None)
    if not isinstance(st, dict):
        return {'added': {}, 'dismissed': []}
    added = st.get('added') if isinstance(st.get('added'), dict) else {}
    dismissed = st.get('dismissed') if isinstance(st.get('dismissed'), list) else []
    return {'added': dict(added),
            'dismissed': [d for d in dismissed if isinstance(d, str)]}


def _save_state(state: dict) -> None:
    try:
        update_json_atomic(_STATE_PATH, lambda _: dict(state), default={})
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
                           return_effective=True)


def _build_provider(cand: dict, effective_url: str, models: list) -> dict:
    ids = sorted(m['model_id'] for m in models if m.get('model_id'))
    return {
        # Deterministic id: re-adding the same engine+port after a state-file
        # loss collides with the existing row instead of duplicating it.
        'id': 'auto_%s_%d' % (cand['engine'], cand['port']),
        'name': '%s (auto)' % cand['name'],
        'brand': 'local',
        'engine': cand['engine'],
        'enabled': True,
        'base_url': effective_url,
        'endpoints': [effective_url],
        'endpoint_models': {effective_url: ids},
        'api_keys': [''],
        'models': models,
        'thinking_format': '',
        'auto_discovered': True,
        'created_at': time.time(),
    }


def _persist_provider(prov: dict) -> bool:
    """Append *prov* to server_config.json, re-checking coverage under the lock.

    A concurrent Settings save may have added a provider for the same port
    between our config read and this write; the mutator re-computes coverage
    on the FRESH dict and returns None (no write) when covered.
    """
    try:
        import lib as _lib
        prov_keys = _covered_port_keys([prov])
        wrote = {'ok': False}

        def _mutate(cfg):
            if not isinstance(cfg, dict):
                cfg = {}
            providers = cfg.get('providers')
            if not isinstance(providers, list):
                providers = cfg['providers'] = []
            if any(p.get('id') == prov['id'] for p in providers
                   if isinstance(p, dict)):
                return None
            if prov_keys & _covered_port_keys(providers):
                return None
            providers.append(prov)
            wrote['ok'] = True
            return cfg

        update_json_atomic(_lib._SERVER_CONFIG_PATH, _mutate, default={})
        return wrote['ok']
    except Exception as e:
        logger.error('[AutoDiscover] persist failed for %s: %s',
                     prov.get('id'), e, exc_info=True)
        return False


def _rebuild_slots() -> None:
    from .health_local import _rebuild_dispatcher_slots
    _rebuild_dispatcher_slots()


def sweep_once(port_open=None, discover=None, rebuild=None) -> dict:
    """One auto-discovery pass over the well-known ports.

    Dependency-injected seams (``port_open`` / ``discover`` / ``rebuild``)
    keep the tests off the network. Returns a stats dict.
    """
    stats = {'scanned': 0, 'probed': 0, 'added': []}
    if _disabled():
        stats['disabled'] = True
        return stats
    port_open = port_open or _port_open
    discover = discover or _discover
    try:
        import lib as _lib
        cfg = _lib._load_server_config() or {}
    except Exception as e:
        logger.warning('[AutoDiscover] cannot load server config: %s', e)
        return stats
    providers = cfg.get('providers') or []
    covered = _covered_port_keys(providers)
    state = _load_state()

    # ── Reconcile deletions: an auto-added provider whose id vanished from
    # the config was removed BY THE USER — mark the port dismissed so the
    # provider never resurrects. (Disabled-but-present still counts as
    # covered above, so merely toggling it off does NOT trigger this.)
    provider_ids = {p.get('id') for p in providers if isinstance(p, dict)}
    gone = [k for k, pid in state['added'].items() if pid not in provider_ids]
    if gone:
        for key in gone:
            state['added'].pop(key, None)
            if key not in covered and key not in state['dismissed']:
                state['dismissed'].append(key)
        _save_state(state)
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
            stats['probed'] += 1
            base = 'http://%s:%d' % (cand['host'], cand['port'])
            models, effective = discover(base)
        except Exception as e:
            # One misbehaving engine must not mask the others.
            logger.warning('[AutoDiscover] probe of %s failed: %s', key, e)
            continue
        if not models:
            # Engine answers but serves nothing yet (no model pulled). Not
            # an error and NOT dismissed — the next sweep re-probes, so a
            # model pulled later still appears automatically.
            logger.info('[AutoDiscover] %s answers but serves no models', key)
            continue
        prov = _build_provider(cand, effective, models)
        if not _persist_provider(prov):
            continue
        state['added'][key] = prov['id']
        _save_state(state)
        covered.add(key)
        stats['added'].append({
            'engine': cand['engine'], 'endpoint': effective,
            'n_models': len(models), 'provider_id': prov['id'],
        })
        logger.info('[AutoDiscover] %s (%s) serves %d model(s) — provider %r '
                    'auto-configured', cand['name'], effective, len(models),
                    prov['id'])
        audit_log('local_provider_autodiscovered', engine=cand['engine'],
                  endpoint=effective, n_models=len(models),
                  provider_id=prov['id'])

    if stats['added']:
        try:
            (rebuild or _rebuild_slots)()
        except Exception as e:
            logger.error('[AutoDiscover] slot rebuild failed: %s', e, exc_info=True)
    return stats


def _loop() -> None:
    logger.info('[AutoDiscover] worker started (delay=%.0fs, interval=%ds)',
                _BOOT_DELAY, _SWEEP_INTERVAL)
    if _stop_event.wait(_BOOT_DELAY):
        return
    while not _stop_event.is_set():
        try:
            sweep_once()
        except Exception as e:
            logger.error('[AutoDiscover] sweep failed: %s', e, exc_info=True)
        if _stop_event.wait(_SWEEP_INTERVAL):
            break
    logger.info('[AutoDiscover] worker stopped')


def start_local_autodiscovery() -> None:
    """Idempotent: spawn the background sweep thread (no-op if running/disabled)."""
    global _thread
    if _disabled():
        logger.info('[AutoDiscover] disabled via TOFU_LOCAL_AUTODISCOVER=0')
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name='local-autodiscover',
                               daemon=True)
    _thread.start()
