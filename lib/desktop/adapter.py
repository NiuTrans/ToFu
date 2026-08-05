"""lib/desktop/adapter.py — 订阅适配器（CLIProxyAPI sidecar）服务器层（E4）。

The tofu server treats a CLIProxyAPI instance running on the user's
desktop agent as an ordinary OpenAI-compatible provider whose base_url
happens to be loopback-ON-THE-AGENT — every request rides the bridge's
``target='loopback'`` relay (lib/desktop/egress.py), so the subscription
tokens and the cloaking arms race both stay at the edge
(docs/SUBSCRIPTION_RELAY_SCENARIOS_DESIGN.md §4.4).

This module owns:
  * the policy store (``data/config/subscription_adapter.json``): per-agent
    random api-key + management secret + port + version pin. The SERVER
    mints the credentials (it needs the api-key to authenticate provider
    calls; agent-side minting would only add an upload path) and sends
    them down with ``adapter_ensure`` — the bridge channel is already the
    authenticated boundary;
  * loopback relay helpers (thin wrappers over egress with the loopback
    whitelist class, ALWAYS pinned to one agent — never the fallback
    chain: another machine hosts another adapter with another api-key);
  * ensure/status/stop task orchestration and the managed
    ``adapter_<id>`` provider in server_config.json (dispatcher sees a
    plain OpenAI slot; the transport branch routes it by the ``adapter``
    marker).
"""

from __future__ import annotations

import threading
import time
import uuid

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'DEFAULT_PORT',
    'policy_for',
    'adapter_policy_public',
    'is_adapter_provider',
    'relay_http',
    'relay_stream',
    'adapter_status',
    'ensure_adapter',
    'stop_adapter',
    'ensure_task_state',
    'provision_provider',
    'deprovision_provider',
    'fetch_models',
]

DEFAULT_PORT = 8317
_POLICY_NAME = 'subscription_adapter.json'
_STATUS_CACHE_TTL_S = 10
_ENSURE_TTL_S = 600  # first run downloads ~20 MB through the user's network

_ensure_tasks: dict = {}
_ensure_lock = threading.Lock()
_status_cache: dict = {}
_status_lock = threading.Lock()


# ══════════════════════════════════════════════════════════
#  Policy store
# ══════════════════════════════════════════════════════════

def _policy_path() -> str:
    return config_path(_POLICY_NAME)


def policy_for(agent_id: str, create: bool = False) -> dict:
    """The stored adapter policy for one agent ({} when absent).

    ``create=True`` mints the random api-key + management secret on first
    sight and persists them — the pair is per-agent and stable, so a
    re-ensure is idempotent and a leaked key scopes to one machine.
    """
    doc = read_json(_policy_path(), default={}) or {}
    entry = (doc.get('agents') or {}).get(agent_id) or {}
    if entry or not create:
        return dict(entry)

    def _mutate(d):
        d = dict(d or {})
        agents = dict(d.get('agents') or {})
        cur = agents.get(agent_id)
        if not cur:
            cur = {
                'api_key': 'ta_' + uuid.uuid4().hex,
                'mgmt_secret': uuid.uuid4().hex + uuid.uuid4().hex,
                'port': DEFAULT_PORT,
                'desired_version': 'latest',
                'auto_update': True,
                'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                            time.gmtime()),
            }
            agents[agent_id] = cur
        d['agents'] = agents
        return d

    update_json_atomic(_policy_path(), _mutate, default={})
    doc = read_json(_policy_path(), default={}) or {}
    return dict((doc.get('agents') or {}).get(agent_id) or {})


def adapter_policy_public(agent_id: str) -> dict:
    """Redacted policy view for the status surface (no secrets)."""
    p = policy_for(agent_id)
    if not p:
        return {}
    return {'port': p.get('port'), 'desired_version': p.get('desired_version'),
            'auto_update': p.get('auto_update'), 'created_at': p.get('created_at')}


# ══════════════════════════════════════════════════════════
#  Loopback relay (pinned to ONE agent, loopback whitelist class)
# ══════════════════════════════════════════════════════════

def relay_http(agent_id: str, port: int, path: str, *, method: str = 'GET',
               headers: dict = None, body: bytes = b'', timeout: float = 30,
               user_id: str = ''):
    """One-shot request to the agent-local adapter (EgressResponse)."""
    from lib.desktop import egress as _eg
    url = f'http://127.0.0.1:{int(port)}{path}'
    return _eg.egress_http(url, method=method, headers=headers, body=body,
                           timeout=timeout, user_id=user_id, agent_id=agent_id,
                           target='loopback', loopback_port=int(port))


def relay_stream(agent_id: str, port: int, path: str, *, method: str = 'POST',
                 headers: dict = None, body: bytes = b'', user_id: str = '',
                 log_prefix: str = ''):
    """Streamed request to the agent-local adapter (EgressStreamReader)."""
    from lib.desktop import egress as _eg
    url = f'http://127.0.0.1:{int(port)}{path}'
    return _eg.open_stream(url, method=method, headers=headers, body=body,
                           agent_id=agent_id, user_id=user_id,
                           log_prefix=log_prefix,
                           target='loopback', loopback_port=int(port))


# ══════════════════════════════════════════════════════════
#  Status + ensure orchestration
# ══════════════════════════════════════════════════════════

def adapter_status(agent_id: str, user_id: str = '') -> dict:
    """Live adapter state from the agent (10s cache — status polls must
    not stampede the bridge). ``{'ok': False, 'error': …}`` when the
    agent is unreachable; the agent's own status dict otherwise."""
    now = time.monotonic()
    with _status_lock:
        cached = _status_cache.get(agent_id)
        if cached and now - cached[0] < _STATUS_CACHE_TTL_S:
            return dict(cached[1])
    from lib.desktop import send_desktop_command
    result, error = send_desktop_command(
        'adapter_status', {}, timeout=12, target_agent_id=agent_id,
        user_id=user_id, ttl=30)
    if error or result is None:
        out = {'ok': False, 'error': error or 'no result'}
    elif isinstance(result, dict) and result.get('error'):
        out = {'ok': False, 'error': result['error']}
    else:
        out = {'ok': True, **(result or {})}
    with _status_lock:
        _status_cache[agent_id] = (now, out)
    return dict(out)


def ensure_task_state(agent_id: str = '') -> dict:
    with _ensure_lock:
        if agent_id:
            return dict(_ensure_tasks.get(agent_id) or {})
        return {k: dict(v) for k, v in _ensure_tasks.items()}


def ensure_adapter(agent_id: str, agent_name: str = '', user_id: str = '') -> dict:
    """Kick a background bring-up: policy → adapter_ensure (long TTL) →
    fetch models → provision the managed provider. Returns the task
    snapshot immediately ('ensuring'); poll :func:`ensure_task_state` /
    :func:`adapter_status` for completion."""
    with _ensure_lock:
        cur = _ensure_tasks.get(agent_id) or {}
        if cur.get('state') == 'ensuring':
            return dict(cur)
        _ensure_tasks[agent_id] = {'state': 'ensuring', 'detail': '',
                                   'started_at': time.time()}

    def _run():
        outcome = {'state': 'error', 'detail': ''}
        try:
            policy = policy_for(agent_id, create=True)
            params = {
                'port': policy['port'],
                'api_key': policy['api_key'],
                'mgmt_secret': policy['mgmt_secret'],
                'version': policy.get('desired_version') or 'latest',
                'auto_update': bool(policy.get('auto_update', True)),
            }
            from lib.desktop import send_desktop_command
            result, error = send_desktop_command(
                'adapter_ensure', params, timeout=_ENSURE_TTL_S,
                target_agent_id=agent_id, user_id=user_id,
                ttl=_ENSURE_TTL_S)
            if error or result is None:
                outcome['detail'] = error or 'no result'
            elif result.get('error'):
                outcome['detail'] = result['error']
            else:
                port = int(result.get('port') or policy['port'])
                models = fetch_models(agent_id, port, policy['api_key'],
                                      user_id=user_id)
                provision_provider(agent_id, agent_name, port,
                                   policy['api_key'], models)
                outcome = {'state': 'ready', 'detail': '',
                           'version': result.get('version', ''),
                           'port': port, 'models': len(models),
                           'provider_id': f'adapter_{agent_id[:8]}'}
        except Exception as e:
            logger.error('[Adapter] ensure failed for %s: %s',
                         agent_id[:8], e, exc_info=True)
            outcome['detail'] = str(e)[:300]
        with _ensure_lock:
            _ensure_tasks[agent_id] = {**outcome, 'started_at':
                                       _ensure_tasks[agent_id]['started_at'],
                                       'finished_at': time.time()}
        logger.info('[Adapter] ensure %s → %s %s', agent_id[:8],
                    outcome['state'], outcome.get('detail') or '')

    threading.Thread(target=_run, daemon=True,
                     name=f'adapter-ensure-{agent_id[:8]}').start()
    return ensure_task_state(agent_id)


def stop_adapter(agent_id: str, user_id: str = '') -> dict:
    """Stop the sidecar on the agent AND deprovision the managed provider
    (a stopped adapter must not keep serving slots)."""
    from lib.desktop import send_desktop_command
    result, error = send_desktop_command(
        'adapter_stop', {}, timeout=15, target_agent_id=agent_id,
        user_id=user_id, ttl=30)
    deprovision_provider(agent_id)
    if error:
        return {'ok': False, 'error': error}
    return {'ok': True, **(result or {})}


# ══════════════════════════════════════════════════════════
#  Managed provider (server_config.json)
# ══════════════════════════════════════════════════════════

def is_adapter_provider(provider: dict) -> dict:
    """The ``adapter`` marker dict of a provider ({} when not one)."""
    marker = (provider or {}).get('adapter')
    return marker if isinstance(marker, dict) else {}


def fetch_models(agent_id: str, port: int, api_key: str,
                 user_id: str = '') -> list:
    """GET /v1/models through the loopback relay → [model_id, …]."""
    resp = relay_http(agent_id, port, '/v1/models',
                      headers={'Authorization': f'Bearer {api_key}'},
                      timeout=30, user_id=user_id)
    if resp.status_code != 200:
        raise RuntimeError(
            f'adapter /v1/models answered HTTP {resp.status_code}: '
            f'{resp.text[:200]}')
    data = resp.json()
    items = data.get('data') or []
    ids = [m.get('id') for m in items if isinstance(m, dict) and m.get('id')]
    if not ids:
        raise RuntimeError('adapter /v1/models returned an empty list — '
                           'no subscription account configured on it yet')
    logger.info('[Adapter] %s exposes %d models', agent_id[:8], len(ids))
    return ids


def provision_provider(agent_id: str, agent_name: str, port: int,
                       api_key: str, model_ids: list) -> bool:
    """Add/refresh the managed ``adapter_<id>`` provider (idempotent)."""
    from lib import _SERVER_CONFIG_PATH, reload_config
    from lib.llm_dispatch import reset_dispatcher

    entry = {
        'id': f'adapter_{agent_id[:8]}',
        'name': f'订阅适配器 · {agent_name or agent_id[:8]}',
        'base_url': f'http://127.0.0.1:{int(port)}/v1',
        'brand': 'adapter',
        'enabled': True,
        'adapter': {'agent_id': agent_id, 'port': int(port)},
        'api_keys': [api_key],
        'protocol': 'openai',
        'models': [{'model_id': mid, 'capabilities': ['text', 'vision']}
                   for mid in model_ids],
    }

    def _mutate(cfg):
        providers = [p for p in (cfg.get('providers') or [])
                     if p.get('id') != entry['id']]
        providers.append(entry)
        cfg['providers'] = providers
        return cfg

    update_json_atomic(_SERVER_CONFIG_PATH, _mutate, default={})
    reload_config()
    reset_dispatcher()
    logger.info('[Adapter] provisioned provider %s (%d models)',
                entry['id'], len(model_ids))
    return True


def deprovision_provider(agent_id: str) -> bool:
    """Remove the managed provider (adapter stop / agent retired)."""
    from lib import _SERVER_CONFIG_PATH, reload_config
    from lib.llm_dispatch import reset_dispatcher

    pid = f'adapter_{agent_id[:8]}'
    removed = {'n': 0}

    def _mutate(cfg):
        before = cfg.get('providers') or []
        after = [p for p in before if p.get('id') != pid]
        removed['n'] = len(before) - len(after)
        cfg['providers'] = after
        return cfg

    update_json_atomic(_SERVER_CONFIG_PATH, _mutate, default={})
    if removed['n']:
        reload_config()
        reset_dispatcher()
        logger.info('[Adapter] deprovisioned provider %s', pid)
    return bool(removed['n'])
