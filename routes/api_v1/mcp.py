"""routes/api_v1/mcp.py — Model Context Protocol bridge management.

Routes:
  GET    /api/v1/mcp/servers            — list configured servers
  POST   /api/v1/mcp/servers            — add/update a server config
  DELETE /api/v1/mcp/servers/<name>     — remove a server config
  POST   /api/v1/mcp/connect            — connect all (or specific) server(s)
  POST   /api/v1/mcp/disconnect         — disconnect all (or specific)
  GET    /api/v1/mcp/tools              — list discovered tools
  GET    /api/v1/mcp/catalog            — curated app-store catalog
  POST   /api/v1/mcp/catalog/install    — one-click install + connect
  POST   /api/v1/mcp/catalog/uninstall  — soft (default) or purge

All routes require ``@require_auth``. Mutations operate on
``data/config/mcp_servers.json`` and the live bridge process; cookie-auth
UI users have admin scope locally so the settings panel keeps working.
"""

from __future__ import annotations

import time

from quart import Blueprint

from lib.api_response import (
    api_bad_request, api_error, api_internal_error, api_not_found, api_ok,
    api_payload,
)
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_mcp_bp = Blueprint('api_v1_mcp', __name__)


def _enabled_tool_summary(bridge) -> dict:
    """Best-effort compact projection; never fail the owning MCP operation."""
    try:
        return bridge.get_enabled_tool_summary()
    except Exception as exc:
        logger.debug('[MCP.v1] enabled tool summary unavailable: %s', exc)
        return {'servers': [], 'total': 0}


# ── Config CRUD ──────────────────────────────────────────────────────

@api_v1_mcp_bp.route('/api/v1/mcp/servers', methods=['GET'])
@require_auth
@api_meta(
    summary='List configured MCP servers',
    description=(
        'Returns ``{servers: [...]}`` with connection status, tool count, '
        'and (when connected) the upstream server\'s reported version. '
        'The ``env`` block is intentionally stripped from each entry; '
        'use ``stored_env_keys`` to learn which keys have stored values. '
        'A logically connected local server may report ``parked=true`` when '
        'its idle stdio child has exited; its catalog remains available and '
        'the next tool call reconnects transparently. '
        'When a server\'s automatic reconnect is failing, ``breaker`` is '
        '``{failures, retry_in, next_retry_ts}`` (else ``null``).'
    ),
    tags=['mcp'],
)
def list_servers_v1():
    from lib.mcp import get_bridge
    from lib.mcp.config import load_mcp_config
    from lib.mcp.transport import header_env_keys, redact_config

    config = load_mcp_config()
    bridge = get_bridge()
    connected_servers = {
        row['name']: row for row in bridge.list_servers()
    }

    servers = []
    for name, srv_cfg in config.items():
        is_connected = name in connected_servers
        tools_count = 0
        tool_names: list[str] = []
        server_version = ''
        server_impl_name = ''
        protocol_version = ''
        compatibility_notice = None
        parked = False
        if is_connected:
            server_row = connected_servers[name]
            tools_count = server_row['tools_count']
            tool_names = server_row['tool_names']
            server_version = server_row.get('server_version', '') or ''
            server_impl_name = server_row.get('server_impl_name', '') or ''
            protocol_version = server_row.get('protocol_version', '') or ''
            compatibility_notice = server_row.get('compatibility_notice')
            parked = bool(server_row.get('parked'))
        # Circuit-breaker status: present only for servers whose automatic
        # reconnect is currently failing+backing off, so the UI can show
        # "retrying in N min" instead of a bare "disconnected".
        breaker = bridge.get_breaker_state(name)
        # Credential health: 'expired' means the subprocess is alive but the
        # stored session cookie/token no longer authenticates (a second health
        # axis the transport ping cannot see). None when never probed.
        cred_health = bridge.get_cred_health(name)
        disabled_tools = [t for t in (srv_cfg.get('disabled_tools') or [])
                          if isinstance(t, str)]
        servers.append({
            'name': name,
            'config': redact_config(srv_cfg),
            'has_env': bool(srv_cfg.get('env')),
            'header_env_keys': header_env_keys(srv_cfg),
            'enabled': srv_cfg.get('enabled', True),
            'connected': is_connected,
            'parked': parked,
            'tools_count': tools_count,
            'tool_names': tool_names,
            'disabled_tools': disabled_tools,
            'enabled_tools_count': max(0, tools_count - len(disabled_tools)),
            'server_version': server_version,
            'server_impl_name': server_impl_name,
            'protocol_version': protocol_version,
            'compatibility_notice': compatibility_notice,
            'breaker': breaker,
            'cred_health': cred_health,
        })
    return api_ok({'servers': servers})


@api_v1_mcp_bp.route('/api/v1/mcp/servers', methods=['POST'])
@require_auth
@api_meta(
    summary='Add or update a server config',
    description='Body: ``{name, command|url, args?, env?, transport?, enabled?, description?}``.',
    tags=['mcp'],
)
def upsert_server_v1():
    from lib.mcp.config import upsert_server as cfg_upsert
    from lib.mcp.transport import (
        VALID_TRANSPORTS, is_stdio, normalize_transport,
    )

    data = parse_body()
    name = data.pop('name', '').strip()
    if not name:
        return api_bad_request('Server name is required', field='name')

    transport = normalize_transport(data)
    if transport not in VALID_TRANSPORTS:
        return api_bad_request(
            f'Unknown transport {transport!r}. Expected one of: '
            f'{", ".join(sorted(VALID_TRANSPORTS))}',
            field='transport')
    # Persist the canonical spelling so aliases ('http', 'streamable_http')
    # never reach the bridge or a stored config.
    data['transport'] = transport
    if is_stdio(data) and not data.get('command'):
        return api_bad_request('command is required for stdio transport',
                                field='command')
    if not is_stdio(data) and not data.get('url'):
        return api_bad_request(f'url is required for {transport} transport',
                               field='url')
    headers = data.get('headers')
    if headers is not None and not isinstance(headers, dict):
        return api_bad_request('headers must be an object', field='headers')

    cfg_upsert(name, data)
    logger.info('[MCP.v1] config upserted: %s (transport=%s)', name, transport)
    return api_ok(message=f'Server "{name}" configured')


@api_v1_mcp_bp.route('/api/v1/mcp/servers/<name>', methods=['DELETE'])
@require_auth
@api_meta(summary='Remove a server config (disconnects first)',
          tags=['mcp'])
def delete_server_v1(name):
    from lib.mcp import get_bridge
    from lib.mcp.config import remove_server as cfg_remove

    bridge = get_bridge()
    if name in {s['name'] for s in bridge.list_servers()}:
        try:
            bridge._disconnect_one(name, forget=True)
            logger.info('[MCP.v1] disconnected %s before removal', name)
        except Exception as e:
            logger.warning('[MCP.v1] disconnect %s failed: %s', name, e)

    cfg_remove(name)
    logger.info('[MCP.v1] config removed: %s', name)
    return api_ok(message=f'Server "{name}" removed')


# ── Lifecycle ────────────────────────────────────────────────────────

@api_v1_mcp_bp.route('/api/v1/mcp/connect', methods=['POST'])
@require_auth
@api_meta(
    summary='Connect MCP server(s)',
    description='Body: ``{server: "<id>"}`` for one, ``{}`` for all enabled.',
    tags=['mcp'],
)
def connect_servers_v1():
    from lib.mcp import get_bridge
    from lib.mcp.client import MCPConnectError
    from lib.mcp.config import load_mcp_config

    data = parse_body()
    target = data.get('server', '').strip()
    bridge = get_bridge()

    if target:
        config = load_mcp_config()
        if target not in config:
            return api_not_found(f'Server "{target}" not in config')
        try:
            tools = bridge.connect_server(target, config[target])
            if not config[target].get('enabled', True):
                from lib.mcp.config import patch_server as cfg_patch
                cfg_patch(target, {'enabled': True})
                logger.info('[MCP.v1] re-enabled %s on connect', target)
            return api_ok({
                'server': target,
                'tools_count': len(tools),
                'tool_names': [t.name for t in tools],
            })
        except MCPConnectError as e:
            logger.error('[MCP.v1] connect %s failed: %s', target, e)
            return api_error(str(e), status=500,
                               stderr_tail=e.stderr_tail or '')
        except Exception as e:
            logger.error('[MCP.v1] connect %s crashed: %s', target, e,
                         exc_info=True)
            return api_internal_error(e, source='api_v1.mcp.connect')

    try:
        result = bridge.connect_all()
        total_tools = sum(len(v) for v in result.values())
        return api_ok({
            'servers': {k: {'tools': v} for k, v in result.items()},
            'total_tools': total_tools,
        })
    except Exception as e:
        logger.error('[MCP.v1] connect_all failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.mcp.connect_all')


@api_v1_mcp_bp.route('/api/v1/mcp/disconnect', methods=['POST'])
@require_auth
@api_meta(summary='Disconnect MCP server(s)',
          description='Body: ``{server: "<id>"}`` for one, ``{}`` for all.',
          tags=['mcp'])
def disconnect_servers_v1():
    from lib.mcp import get_bridge

    data = parse_body()
    target = data.get('server', '').strip()
    bridge = get_bridge()

    if target:
        try:
            bridge._disconnect_one(target, forget=True)
            logger.info('[MCP.v1] disconnected %s', target)
            return api_ok(message=f'Disconnected from "{target}"')
        except Exception as e:
            logger.error('[MCP.v1] disconnect %s failed: %s', target, e,
                         exc_info=True)
            return api_internal_error(e, source='api_v1.mcp.disconnect')

    try:
        bridge.disconnect_all()
        return api_ok({'message': 'All MCP servers disconnected'})
    except Exception as e:
        logger.error('[MCP.v1] disconnect_all failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.mcp.disconnect_all')


# ── Tool introspection ──────────────────────────────────────────────

@api_v1_mcp_bp.route('/api/v1/mcp/tools', methods=['GET'])
@require_auth
@api_meta(
    summary='List discovered MCP tools',
    description='Aggregated across all connected servers.',
    tags=['mcp'],
)
def list_tools_v1():
    from quart import request

    from lib.mcp import get_bridge
    from lib.mcp.config import load_mcp_config
    from lib.mcp.types import make_namespaced_name

    bridge = get_bridge()
    only_server = (request.args.get('server') or '').strip()
    config = load_mcp_config()
    tools = []
    for server_info in bridge.list_servers():
        if only_server and server_info['name'] != only_server:
            continue
        disabled = set((config.get(server_info['name'], {}) or {})
                       .get('disabled_tools') or [])
        for tool_name in server_info['tool_names']:
            ns_name = make_namespaced_name(server_info['name'], tool_name)
            info = bridge.get_tool_info(ns_name)
            tools.append({
                'server': server_info['name'],
                'name': tool_name,
                'namespaced_name': ns_name,
                'description': info['description'] if info else '',
                'input_schema': info['input_schema'] if info else {},
                'enabled': tool_name not in disabled,
            })
    return api_ok({
        'tools': tools,
        'total': len(tools),
        'servers_connected': bridge.server_count,
    })


@api_v1_mcp_bp.route('/api/v1/mcp/servers/<name>/tools', methods=['PUT'])
@require_auth
@api_meta(
    summary='Set per-tool enable/disable on a server',
    description=(
        'Body: ``{disabled_tools: [tool_name, ...]}`` — the FULL replacement '
        'list of disabled (upstream, non-namespaced) tool names for this '
        'server. Disabled tools are hidden from the model\'s tool list and '
        'refused at call time. Persists into the server config row and '
        'hot-applies to the running bridge (no reconnect).'
    ),
    tags=['mcp'],
)
def set_server_tools_v1(name):
    from lib.mcp import get_bridge
    from lib.mcp.config import load_mcp_config, patch_server as cfg_patch

    data = parse_body()
    disabled = data.get('disabled_tools', [])
    if not isinstance(disabled, list) or not all(
            isinstance(t, str) and t.strip() for t in disabled):
        return api_bad_request(
            'disabled_tools must be a list of tool names',
            field='disabled_tools')
    config = load_mcp_config()
    if name not in config:
        return api_not_found(f'Server "{name}" not in config')
    disabled_tools = sorted(set(disabled))
    if cfg_patch(name, {'disabled_tools': disabled_tools}) is None:
        return api_not_found(f'Server "{name}" not in config')
    bridge = get_bridge()
    bridge.set_disabled_tools(name, disabled_tools)
    logger.info('[MCP.v1] tool filter set: %s → %d disabled tool(s)',
                name, len(disabled_tools))
    return api_ok({
        'server': name,
        'disabled_tools': disabled_tools,
        'mcp_tool_summary': _enabled_tool_summary(bridge),
    })


# ── Catalog (App-Store) ─────────────────────────────────────────────

@api_v1_mcp_bp.route('/api/v1/mcp/catalog', methods=['GET'])
@require_auth
@api_meta(
    summary='Curated MCP server catalog',
    description=(
        'Returns each catalog entry annotated with ``installed`` / '
        '``connected`` / ``tools_count`` / ``server_version`` / '
        '``protocol_version`` / ``compatibility_notice`` / ``parked`` / '
        '``stored_env_keys`` (which env vars already have a stored value, '
        '*without* leaking the value) / ``cred_health`` (``expired`` when the '
        'stored session cookie/token no longer authenticates a live server). '
        'The top-level ``health_probe_contract`` describes how any server '
        '(curated or custom) declares a background credential probe.'
    ),
    tags=['mcp'],
)
def get_catalog_v1():
    from lib.mcp import get_bridge
    from lib.mcp.config import load_mcp_config
    from lib.mcp.registry import get_catalog

    config = load_mcp_config()
    bridge = get_bridge()
    connected_servers = {
        row['name']: row for row in bridge.list_servers()
    }
    connected_names = set(connected_servers)

    def _live_meta(sid):
        """Runtime metadata for one connected server."""
        server_row = connected_servers.get(sid)
        if server_row is None:
            return {}
        return {
            'tools_count': server_row['tools_count'],
            'server_version': server_row.get('server_version', '') or '',
            'server_impl_name': server_row.get('server_impl_name', '') or '',
            'protocol_version': server_row.get('protocol_version', '') or '',
            'compatibility_notice': server_row.get('compatibility_notice'),
            'parked': bool(server_row.get('parked')),
        }

    entries = []
    catalog_ids = set()
    for entry in get_catalog():
        sid = entry['id']
        catalog_ids.add(sid)
        installed = sid in config
        connected = sid in connected_names
        live = _live_meta(sid) if connected else {}
        tools_count = int(live.get('tools_count') or 0)
        stored_env = (config.get(sid, {}) or {}).get('env', {}) or {}
        stored_env_keys = [k for k, v in stored_env.items()
                           if isinstance(v, str) and v.strip()]
        entries.append({
            **entry,
            'installed': installed,
            'connected': connected,
            'parked': bool(live.get('parked')),
            'tools_count': tools_count,
            'disabled_tools': [t for t in ((config.get(sid, {}) or {}).get('disabled_tools') or [])
                               if isinstance(t, str)],
            'server_version': live.get('server_version', ''),
            'server_impl_name': live.get('server_impl_name', ''),
            'protocol_version': live.get('protocol_version', ''),
            'compatibility_notice': live.get('compatibility_notice'),
            'stored_env_keys': stored_env_keys,
            'breaker': bridge.get_breaker_state(sid),
            'cred_health': bridge.get_cred_health(sid),
        })

    # Surface servers that are configured in mcp_servers.json but have no
    # curated catalog entry, so they can never be silently invisible in the
    # settings panel. Synthesize a minimal "Custom" card from the stored
    # config. env values are NOT leaked — only their keys (as stored_env_keys).
    from lib.mcp.registry import CAT_CUSTOM
    from lib.mcp.transport import redact_config
    for sid, srv_cfg in config.items():
        if sid in catalog_ids:
            continue
        connected = sid in connected_names
        live = _live_meta(sid) if connected else {}
        tools_count = int(live.get('tools_count') or 0)
        stored_env = (srv_cfg or {}).get('env', {}) or {}
        stored_env_keys = [k for k, v in stored_env.items()
                           if isinstance(v, str) and v.strip()]
        # Re-expose stored env as optional, secret env_specs so the install
        # modal can re-edit credentials without inventing schema.
        env_specs = [{'key': k, 'label': k, 'required': False, 'secret': True}
                     for k in stored_env_keys]
        entries.append({
            'id': sid,
            'name': sid,
            'description': srv_cfg.get('description', '') or 'Custom MCP server (from mcp_servers.json)',
            'icon': '🔌',
            'category': CAT_CUSTOM,
            'command': srv_cfg.get('command', ''),
            'args': srv_cfg.get('args', []),
            'transport': srv_cfg.get('transport', 'stdio'),
            'env_specs': env_specs,
            'url': srv_cfg.get('url', ''),
            'headers': redact_config(srv_cfg).get('headers', {}),
            'tags': ['custom'],
            'custom': True,
            'installed': True,
            'connected': connected,
            'parked': bool(live.get('parked')),
            'tools_count': tools_count,
            'disabled_tools': [t for t in (srv_cfg.get('disabled_tools') or [])
                               if isinstance(t, str)],
            'server_version': live.get('server_version', ''),
            'server_impl_name': live.get('server_impl_name', ''),
            'protocol_version': live.get('protocol_version', ''),
            'compatibility_notice': live.get('compatibility_notice'),
            'stored_env_keys': stored_env_keys,
            'breaker': bridge.get_breaker_state(sid),
            'cred_health': bridge.get_cred_health(sid),
        })

    # Advertise the standard credential health-probe contract so the settings
    # UI / API consumers can discover how ANY server (curated or custom) opts
    # into background credential verification — reflected from the canonical
    # schema, never hand-typed here (so it can't drift).
    from lib.mcp.health_probe import HEALTH_PROBE_SCHEMA
    return api_ok({
        'catalog': entries,
        'health_probe_contract': HEALTH_PROBE_SCHEMA,
        # Settings already paid for this catalog response.  Carry the same
        # compact authority used by server-config so it never follows with a
        # full /mcp/tools schema fetch merely to repaint the context rail.
        'mcp_tool_summary': _enabled_tool_summary(bridge),
    })


@api_v1_mcp_bp.route('/api/v1/mcp/catalog/install', methods=['POST'])
@require_auth
@api_meta(
    summary='One-click install + connect',
    description=(
        'Body: ``{id, env?: {KEY: value, ...}}``. User-supplied env values '
        'take precedence over any previously-stored values (e.g. from a '
        'soft uninstall). Required env vars from the catalog ``env_specs`` '
        'are validated against the merged set.'
    ),
    tags=['mcp'],
)
def install_from_catalog_v1():
    from lib.mcp.config import load_mcp_config, upsert_server as cfg_upsert
    from lib.mcp.registry import build_server_config, get_catalog_entry

    data = parse_body()
    server_id = data.get('id', '').strip()
    env_values = data.get('env', {})
    if not server_id:
        return api_bad_request('server id is required', field='id')
    entry = get_catalog_entry(server_id)
    if entry is None:
        return api_not_found(f'Unknown server: {server_id}')

    existing_cfg = load_mcp_config().get(server_id, {})
    existing_env = existing_cfg.get('env', {}) or {}
    merged_env = dict(existing_env)
    for k, v in (env_values or {}).items():
        if isinstance(v, str) and v.strip():
            merged_env[k] = v

    for spec in entry.get('env_specs', []):
        if spec.get('required') and not str(merged_env.get(spec['key'], '')).strip():
            return api_bad_request(
                f'Required: {spec.get("label", spec["key"])}')

    server_cfg = build_server_config(server_id, merged_env)
    if server_cfg is None:
        return api_internal_error('Failed to build config',
                                  source='api_v1.mcp.catalog_install')
    server_cfg['enabled'] = True

    cfg_upsert(server_id, server_cfg)
    logger.info('[MCP.v1] catalog install: %s', server_id)

    # For a vendored internal launcher that isn't on PATH yet, the first
    # install does a cold `pip install` that can take MINUTES. We do NOT block
    # this request on it — a multi-minute synchronous POST would be cut by a
    # reverse proxy's response timeout (the app supports cloud-IDE proxies)
    # and would leave a half-installed package. Instead we start a background
    # install job and return `status:'installing'` immediately; the front end
    # polls /catalog/install/status, which performs the (fast) connect once
    # pip finishes. Launchers already on PATH skip straight to connect.
    from lib.mcp.transport import stdio_command
    command = stdio_command(server_cfg)
    if command:
        from lib.mcp.client import is_vendored_launcher, start_install_job
        if is_vendored_launcher(command):
            job = start_install_job(command)
            if job.get('state') == 'installing':
                return api_payload({
                    'status': 'installing',
                    'id': server_id,
                    'message': f'{entry["name"]} 正在安装依赖…',
                }, 202)
            if job.get('state') == 'error':
                from lib.mcp.client import _launcher_install_hint
                logger.error('[MCP.v1] catalog install: install of %s failed: %s',
                             server_id, job.get('detail'))
                return api_error(
                    _launcher_install_hint(command), status=500,
                    config_saved=True,
                    stderr_tail=job.get('detail') or '')
            # state == 'ready' → fall through to the fast connect below.

    return _connect_after_install(server_id, server_cfg, entry['name'])


def _connect_after_install(server_id, server_cfg, display_name):
    """Do the (fast) MCP handshake for an installed server + surface errors.

    Shared by the synchronous install path (launcher already on PATH) and the
    async status poll (launcher just finished pip-installing).
    """
    from lib.mcp import get_bridge
    from lib.mcp.client import MCPConnectError

    bridge = get_bridge()
    try:
        tools = bridge.connect_server(server_id, server_cfg)
        return api_ok({
            'status': 'ready',
            'message': f'{display_name} installed and connected',
            'tools_count': len(tools),
            'tool_names': [t.name for t in tools],
        })
    except MCPConnectError as e:
        logger.error('[MCP.v1] catalog install connect %s failed: %s',
                     server_id, e)
        return api_error(
            f'Config saved but connection failed.\n\n{e}', status=500,
            config_saved=True, stderr_tail=e.stderr_tail or '')
    except Exception as e:
        logger.error('[MCP.v1] catalog install crashed for %s: %s',
                     server_id, e, exc_info=True)
        return api_error(
            f'Config saved but connection failed: {e}', status=500,
            config_saved=True)


@api_v1_mcp_bp.route('/api/v1/mcp/catalog/install/status', methods=['GET'])
@require_auth
@api_meta(
    summary='Poll an async catalog install',
    description=(
        'Query ``?id=<server>``. Returns ``{status: installing|ready|error}``. '
        'When the background pip finishes successfully this endpoint performs '
        'the (fast) MCP handshake and returns the connected tool list, mirroring '
        'the synchronous install response.'
    ),
    tags=['mcp'],
)
def install_status_v1():
    from quart import request

    from lib.mcp.client import (
        _launcher_install_hint, get_install_job,
    )
    from lib.mcp.config import load_mcp_config
    from lib.mcp.registry import get_catalog_entry

    server_id = (request.args.get('id') or '').strip()
    if not server_id:
        return api_bad_request('server id is required', field='id')

    server_cfg = load_mcp_config().get(server_id)
    if server_cfg is None:
        return api_error(f'Unknown server: {server_id}', status=404)

    from lib.mcp.transport import stdio_command
    command = stdio_command(server_cfg)
    job = get_install_job(command) if command else None

    # No job recorded (e.g. server restarted mid-install) — treat as unknown
    # and let the client re-POST install to restart cleanly.
    if job is None:
        return api_ok({'status': 'unknown', 'id': server_id})

    state = job.get('state')
    if state == 'installing':
        return api_payload({'status': 'installing', 'id': server_id}, 202)
    if state == 'error':
        return api_payload({
            'status': 'error',
            'error': _launcher_install_hint(command),
            'config_saved': True,
            'stderr_tail': job.get('detail') or '',
        }, 500)

    # state == 'ready' → perform the fast handshake now.
    entry = get_catalog_entry(server_id)
    display_name = entry['name'] if entry else server_id
    return _connect_after_install(server_id, server_cfg, display_name)


@api_v1_mcp_bp.route('/api/v1/mcp/catalog/uninstall', methods=['POST'])
@require_auth
@api_meta(
    summary='Uninstall (soft by default)',
    description=(
        'Body: ``{id, purge?: bool}``. Default is *soft uninstall*: '
        'disconnect + ``enabled=false`` but keep the env block so the '
        'user can reconnect without re-entering credentials. Pass '
        '``purge: true`` to hard-delete the config row entirely.'
    ),
    tags=['mcp'],
)
def uninstall_from_catalog_v1():
    from lib.log import audit_log
    from lib.mcp import get_bridge
    from lib.mcp.config import (
        load_mcp_config, remove_server as cfg_remove,
        patch_server as cfg_patch,
    )

    data = parse_body()
    server_id = data.get('id', '').strip()
    purge = bool(data.get('purge', False))
    if not server_id:
        return api_bad_request('server id is required', field='id')

    bridge = get_bridge()
    if server_id in {s['name'] for s in bridge.list_servers()}:
        try:
            bridge._disconnect_one(server_id, forget=True)
            logger.info('[MCP.v1] disconnected %s before uninstall '
                        '(purge=%s)', server_id, purge)
        except Exception as e:
            logger.warning('[MCP.v1] disconnect %s failed: %s', server_id, e)

    if purge:
        cfg_remove(server_id)
        audit_log('mcp_uninstall', server=server_id, mode='purge')
        logger.info('[MCP.v1] catalog uninstall (purge): %s', server_id)
        return api_ok(message=f'Uninstalled {server_id}', purged=True)

    config = load_mcp_config()
    if server_id in config:
        if cfg_patch(server_id, {'enabled': False}) is None:
            return api_ok(message=f'{server_id} was not installed', purged=False)
        audit_log('mcp_uninstall', server=server_id, mode='soft')
        logger.info('[MCP.v1] catalog uninstall (soft, env kept): %s',
                    server_id)
        return api_ok({
            'message': f'{server_id} disabled (credentials kept for re-enable)',
            'purged': False,
        })

    logger.warning('[MCP.v1] uninstall requested but not in config: %s',
                   server_id)
    return api_ok(message=f'{server_id} was not installed', purged=False)


# ── Upstream version updates ───────────────────────────────────────

@api_v1_mcp_bp.route('/api/v1/mcp/updates', methods=['GET'])
@require_auth
@api_meta(
    summary='Check installed servers for upstream updates',
    description=(
        'Compares each configured stdio server\'s launch spec (npx/uvx) '
        'against its upstream registry (npm/PyPI). Returns ``{updates: '
        '{name: {updatable, source, package, current, latest, pinned, '
        'update_available, ...}}}``. ``update_available`` is ``null`` when '
        'the current version cannot be determined (floating spec + not '
        'connected); non-updatable servers carry ``updatable: false`` + '
        'a ``reason``. Latest-version sightings are cached server-side.'
    ),
    tags=['mcp'],
)
async def check_updates_v1():
    from lib.mcp.updates import check_all_updates

    try:
        updates = await check_all_updates()
    except Exception as e:
        logger.error('[MCP.v1] updates check failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.mcp.updates_check')
    return api_ok({'updates': updates, 'checked_at': time.time()})


@api_v1_mcp_bp.route('/api/v1/mcp/updates/apply', methods=['POST'])
@require_auth
@api_meta(
    summary='Pin a server to the latest upstream release and reconnect',
    description=(
        'Body: ``{id}``. Rewrites the stored launch args to pin the '
        'registry-latest version (env/credentials preserved), then '
        'reconnects an enabled server so the new code is what runs. A '
        'reconnect failure after the config was rewritten returns 500 '
        'with ``config_saved: true`` (mirrors the install flow).'
    ),
    tags=['mcp'],
)
async def apply_update_v1():
    from lib.mcp.client import MCPConnectError
    from lib.mcp.updates import apply_update
    from lib.request_parser import async_parse_body

    data = await async_parse_body()
    server_id = data.get('id', '').strip()
    if not server_id:
        return api_bad_request('server id is required', field='id')

    try:
        result = await apply_update(server_id)
    except KeyError:
        return api_not_found(f'Server "{server_id}" not in config')
    except ValueError as e:
        return api_bad_request(
            f'Server "{server_id}" is not updatable ({e})')
    except MCPConnectError as e:
        logger.error('[MCP.v1] update apply %s: reconnect failed: %s',
                     server_id, e)
        return api_error(
            f'Updated but reconnection failed.\n\n{e}', status=500,
            config_saved=True, updated=True,
            stderr_tail=e.stderr_tail or '')
    except Exception as e:
        logger.error('[MCP.v1] update apply %s crashed: %s', server_id, e,
                     exc_info=True)
        return api_internal_error(e, source='api_v1.mcp.update_apply')

    if result.get('error'):
        return api_error(
            f'Upstream version lookup failed for '
            f'{result.get("package", server_id)}', status=502,
            package=result.get('package', ''),
            source=result.get('source', ''))
    return api_ok(**result)

__all__ = ['api_v1_mcp_bp']
