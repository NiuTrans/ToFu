"""lib/desktop/adapter.py — 订阅适配器（CLIProxyAPI sidecar）服务器层（E4）。

The tofu server treats a CLIProxyAPI instance running on the user's
desktop agent as an ordinary OpenAI-compatible provider whose base_url
happens to be loopback-ON-THE-AGENT — every request rides the bridge's
``target='loopback'`` relay (lib/desktop/egress.py), so the subscription
tokens and the cloaking arms race both stay at the edge
(docs/modules/remote_execution.md).

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

import json
import threading
import time
import uuid
from urllib.parse import urlencode

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
    'adapter_accounts',
    'start_adapter_oauth',
    'adapter_oauth_status',
    'submit_adapter_oauth_callback',
    'delete_adapter_account',
    'sync_provider',
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
_accounts_cache: dict = {}
_accounts_lock = threading.Lock()


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
#  CLIProxyAPI Management API (credentials never leave the agent)
# ══════════════════════════════════════════════════════════

def _management_request(agent_id: str, path: str, *, method: str = 'GET',
                        payload: dict = None, user_id: str = '') -> dict:
    if not path.startswith('/v0/management/'):
        raise ValueError('adapter management path is not allowed')
    policy = policy_for(agent_id)
    if not policy or not policy.get('mgmt_secret'):
        raise RuntimeError('subscription adapter is not configured for this agent')
    body = (json.dumps(payload, separators=(',', ':')).encode()
            if payload is not None else b'')
    headers = {'Authorization': f"Bearer {policy['mgmt_secret']}"}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    response = relay_http(
        agent_id, int(policy.get('port') or DEFAULT_PORT), path,
        method=method, headers=headers, body=body, timeout=30,
        user_id=user_id)
    if response.status_code < 200 or response.status_code >= 300:
        detail = response.text[:300]
        try:
            parsed = response.json()
            detail = parsed.get('error') or parsed.get('message') or detail
        except (ValueError, TypeError, AttributeError) as e:
            logger.debug('[Adapter] management error body is not structured '
                         'JSON (HTTP %s): %s', response.status_code, e)
        raise RuntimeError(
            f'adapter management API answered HTTP {response.status_code}: '
            f'{detail}')
    if not response.content:
        return {}
    try:
        data = response.json()
    except (ValueError, TypeError) as e:
        raise RuntimeError('adapter management API returned invalid JSON') from e
    return data if isinstance(data, dict) else {}


def _account_provider(item: dict) -> str:
    raw = str(item.get('provider') or item.get('type') or '').strip().lower()
    if raw in ('anthropic', 'claude') or raw.startswith('claude'):
        return 'claude'
    if raw in ('codex', 'openai', 'chatgpt') or raw.startswith('codex'):
        return 'codex'
    return raw or 'other'


def _invalidate_adapter_caches(agent_id: str) -> None:
    with _status_lock:
        _status_cache.pop(agent_id, None)
    with _accounts_lock:
        _accounts_cache.pop(agent_id, None)


def adapter_accounts(agent_id: str, user_id: str = '',
                     force: bool = False) -> list:
    """Return a sanitized account inventory from ``/auth-files``."""
    now = time.monotonic()
    with _accounts_lock:
        cached = _accounts_cache.get(agent_id)
        if not force and cached and now - cached[0] < _STATUS_CACHE_TTL_S:
            return [dict(a) for a in cached[1]]
    data = _management_request(agent_id, '/v0/management/auth-files',
                               user_id=user_id)
    out = []
    for item in data.get('files') or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or item.get('id') or '').strip()
        if not name:
            continue
        out.append({
            'name': name,
            'auth_index': item.get('auth_index'),
            'provider': _account_provider(item),
            'email': str(item.get('email') or '').strip(),
            'label': str(item.get('label') or '').strip(),
            'status': str(item.get('status') or '').strip(),
            'status_message': str(item.get('status_message') or '').strip(),
            'disabled': bool(item.get('disabled', False)),
            'unavailable': bool(item.get('unavailable', False)),
        })
    with _accounts_lock:
        _accounts_cache[agent_id] = (now, out)
    return [dict(a) for a in out]


_ADAPTER_OAUTH_PATHS = {
    'claude': '/v0/management/anthropic-auth-url?is_webui=true',
    'codex': '/v0/management/codex-auth-url?is_webui=true',
}


def start_adapter_oauth(agent_id: str, provider: str,
                        user_id: str = '') -> dict:
    path = _ADAPTER_OAUTH_PATHS.get(provider)
    if not path:
        raise ValueError('unsupported adapter OAuth provider')
    data = _management_request(agent_id, path, user_id=user_id)
    if data.get('status') != 'ok' or not data.get('url') or not data.get('state'):
        raise RuntimeError(data.get('error') or 'adapter returned an invalid OAuth flow')
    return {'provider': provider, 'status': 'started',
            'auth_url': data['url'], 'state': data['state']}


def sync_provider(agent_id: str, agent_name: str = '',
                  user_id: str = '') -> dict:
    """Refresh the managed adapter provider after account changes."""
    policy = policy_for(agent_id)
    if not policy:
        raise RuntimeError('subscription adapter is not configured for this agent')
    port = int(policy.get('port') or DEFAULT_PORT)
    try:
        models = fetch_models(agent_id, port, policy['api_key'], user_id=user_id)
    except RuntimeError as e:
        if 'empty list' in str(e):
            deprovision_provider(agent_id)
            return {'provider_id': f'adapter_{agent_id[:8]}', 'models': 0}
        raise
    provision_provider(agent_id, agent_name, port, policy['api_key'], models)
    return {'provider_id': f'adapter_{agent_id[:8]}', 'models': len(models)}


def _adapter_provider_status(agent_id: str) -> dict:
    from lib import _SERVER_CONFIG_PATH
    pid = f'adapter_{agent_id[:8]}'
    cfg = read_json(_SERVER_CONFIG_PATH, default={}) or {}
    entry = next((p for p in (cfg.get('providers') or [])
                  if p.get('id') == pid), None)
    model_count = len((entry or {}).get('models') or [])
    return {'provider_id': pid,
            'provider_ready': bool(entry and entry.get('enabled', True)
                                   and model_count),
            'model_count': model_count}


def adapter_oauth_status(agent_id: str, state: str, agent_name: str = '',
                         user_id: str = '') -> dict:
    query = urlencode({'state': state})
    data = _management_request(
        agent_id, f'/v0/management/get-auth-status?{query}', user_id=user_id)
    status = str(data.get('status') or 'wait').lower()
    out = {'status': status, 'error': str(data.get('error') or '')[:300]}
    if status == 'ok':
        _invalidate_adapter_caches(agent_id)
        try:
            synced = sync_provider(agent_id, agent_name, user_id=user_id)
            out.update(synced)
            out['provider_ready'] = bool(synced.get('models'))
        except Exception as e:
            logger.debug('[Adapter] OAuth provider sync unavailable for %s: %s',
                         agent_id[:8], e)
            out['provider_ready'] = False
            out['catalog_error'] = str(e)[:300]
        try:
            out['accounts'] = adapter_accounts(agent_id, user_id=user_id,
                                               force=True)
        except Exception as e:
            logger.debug('[Adapter] OAuth account refresh unavailable for %s: %s',
                         agent_id[:8], e)
            out['accounts'] = []
            out['accounts_error'] = str(e)[:300]
    return out


def submit_adapter_oauth_callback(agent_id: str, provider: str, state: str,
                                  *, code: str = '', redirect_url: str = '',
                                  error: str = '', user_id: str = '') -> dict:
    canonical = {'claude': 'anthropic', 'codex': 'codex'}.get(provider)
    if not canonical:
        raise ValueError('unsupported adapter OAuth provider')
    payload = {'provider': canonical, 'state': state}
    if code:
        payload['code'] = code
    if redirect_url:
        payload['redirect_url'] = redirect_url
    if error:
        payload['error'] = error
    return _management_request(agent_id, '/v0/management/oauth-callback',
                               method='POST', payload=payload,
                               user_id=user_id)


def delete_adapter_account(agent_id: str, name: str, auth_index=None,
                           agent_name: str = '', user_id: str = '') -> dict:
    accounts = adapter_accounts(agent_id, user_id=user_id, force=True)
    match = next((a for a in accounts if a.get('name') == name and
                  (auth_index is None or a.get('auth_index') == auth_index)), None)
    if not match:
        raise ValueError('unknown adapter account')
    query_data = {'name': name}
    if auth_index is not None:
        query_data['auth_index'] = auth_index
    _management_request(agent_id,
                        '/v0/management/auth-files?' + urlencode(query_data),
                        method='DELETE', user_id=user_id)
    _invalidate_adapter_caches(agent_id)
    remaining = adapter_accounts(agent_id, user_id=user_id, force=True)
    if not remaining:
        deprovision_provider(agent_id)
        return {'deleted': True,
                'provider_id': f'adapter_{agent_id[:8]}', 'models': 0}
    synced = sync_provider(agent_id, agent_name, user_id=user_id)
    return {'deleted': True, **synced}


# ══════════════════════════════════════════════════════════
#  Status + ensure orchestration
# ══════════════════════════════════════════════════════════

def adapter_status(agent_id: str, agent_name: str = '',
                   user_id: str = '') -> dict:
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
        if out.get('running'):
            try:
                out['accounts'] = adapter_accounts(agent_id, user_id=user_id)
                counts = {'claude': 0, 'codex': 0, 'other': 0}
                for account in out['accounts']:
                    key = account.get('provider')
                    counts[key if key in counts else 'other'] += 1
                out['provider_counts'] = counts
                try:
                    provider_state = _adapter_provider_status(agent_id)
                    usable_accounts = [a for a in out['accounts']
                                       if not a.get('disabled')
                                       and not a.get('unavailable')]
                    if usable_accounts and not provider_state['provider_ready']:
                        sync_provider(agent_id, agent_name, user_id=user_id)
                        provider_state = _adapter_provider_status(agent_id)
                    out.update(provider_state)
                except Exception as e:
                    logger.debug('[Adapter] provider status unavailable for %s: %s',
                                 agent_id[:8], e)
                    out.update({'provider_id': f'adapter_{agent_id[:8]}',
                                'provider_ready': False, 'model_count': 0})
                    out['catalog_error'] = str(e)[:300]
            except Exception as e:
                logger.debug('[Adapter] account inventory unavailable for %s: %s',
                             agent_id[:8], e)
                out['accounts'] = []
                out['provider_counts'] = {'claude': 0, 'codex': 0, 'other': 0}
                out['accounts_error'] = str(e)[:300]
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
                try:
                    models = fetch_models(agent_id, port, policy['api_key'],
                                          user_id=user_id)
                except RuntimeError as e:
                    # A brand-new adapter has no accounts yet. Starting it is
                    # still success: Settings must expose the login buttons.
                    if 'empty list' not in str(e):
                        raise
                    models = []
                    deprovision_provider(agent_id)
                if models:
                    provision_provider(agent_id, agent_name, port,
                                       policy['api_key'], models)
                outcome = {'state': 'ready', 'detail': '',
                           'version': result.get('version', ''),
                           'port': port, 'models': len(models),
                           'accounts_needed': not bool(models),
                           'provider_id': f'adapter_{agent_id[:8]}'}
        except Exception as e:
            logger.error('[Adapter] ensure failed for %s: %s',
                         agent_id[:8], e, exc_info=True)
            outcome['detail'] = str(e)[:300]
        with _ensure_lock:
            _ensure_tasks[agent_id] = {**outcome, 'started_at':
                                       _ensure_tasks[agent_id]['started_at'],
                                       'finished_at': time.time()}
        # A pre-start status poll may have cached ``running: false``. Drop it
        # as soon as bring-up finishes so the next UI poll reflects the agent
        # and its newly created account/catalog state immediately.
        _invalidate_adapter_caches(agent_id)
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
    _invalidate_adapter_caches(agent_id)
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

    changed = {'yes': False}

    def _mutate(cfg):
        providers = list(cfg.get('providers') or [])
        out = []
        found = False
        for current in providers:
            if current.get('id') != entry['id']:
                out.append(current)
                continue
            if not found:
                out.append(entry)
                found = True
                if current != entry:
                    changed['yes'] = True
            else:
                changed['yes'] = True
        if not found:
            out.append(entry)
            changed['yes'] = True
        if not changed['yes']:
            return None
        cfg['providers'] = out
        return cfg

    update_json_atomic(_SERVER_CONFIG_PATH, _mutate, default={})
    if changed['yes']:
        reload_config()
        reset_dispatcher()
        logger.info('[Adapter] provisioned provider %s (%d models)',
                    entry['id'], len(model_ids))
    return changed['yes']


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
        if not removed['n']:
            return None
        cfg['providers'] = after
        return cfg

    update_json_atomic(_SERVER_CONFIG_PATH, _mutate, default={})
    if removed['n']:
        reload_config()
        reset_dispatcher()
        logger.info('[Adapter] deprovisioned provider %s', pid)
    return bool(removed['n'])
