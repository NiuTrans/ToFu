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

from flask import Blueprint, jsonify

from lib.api_response import api_bad_request, api_internal_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_mcp_bp = Blueprint('api_v1_mcp', __name__)


def _invalidate_tool_latches(reason: str) -> None:
    """Drop every conversation's tool-schema latch after an MCP mutation.

    The per-conversation latch (lib/tools/registry.py) freezes the tool array
    a conversation first used to keep the prompt-cache prefix byte-identical,
    deferring composer-toggle flaps to the next NEW conversation. But an MCP
    install / uninstall / connect / disconnect changes the GLOBAL tool surface
    on purpose — the user expects the new (or removed) tools to take effect on
    the next round of EVERY conversation, not just a brand-new one. Clearing
    all latches makes that happen; conversations whose effective tool set is
    unchanged re-latch byte-identically next round (no cache rebuild), so the
    cost is paid only where the tool set genuinely changed.
    """
    try:
        from lib.tools import clear_all_tool_list_latches
        n = clear_all_tool_list_latches()
        if n:
            logger.info('[MCP.v1] %s → cleared %d tool-schema latch(es)',
                        reason, n)
    except Exception as e:
        logger.warning('[MCP.v1] tool-latch invalidation failed (%s): %s',
                        reason, e)


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
        'When a server\'s automatic reconnect is failing, ``breaker`` is '
        '``{failures, retry_in, next_retry_ts}`` (else ``null``).'
    ),
    tags=['mcp'],
)
def list_servers_v1():
    from lib.mcp import get_bridge
    from lib.mcp.config import load_mcp_config

    config = load_mcp_config()
    bridge = get_bridge()
    connected_servers = {s['name'] for s in bridge.list_servers()}

    servers = []
    for name, srv_cfg in config.items():
        is_connected = name in connected_servers
        tools_count = 0
        tool_names: list[str] = []
        server_version = ''
        server_impl_name = ''
        if is_connected:
            for s in bridge.list_servers():
                if s['name'] == name:
                    tools_count = s['tools_count']
                    tool_names = s['tool_names']
                    server_version = s.get('server_version', '') or ''
                    server_impl_name = s.get('server_impl_name', '') or ''
                    break
        # Circuit-breaker status: present only for servers whose automatic
        # reconnect is currently failing+backing off, so the UI can show
        # "retrying in N min" instead of a bare "disconnected".
        breaker = bridge.get_breaker_state(name)
        servers.append({
            'name': name,
            'config': {k: v for k, v in srv_cfg.items() if k != 'env'},
            'has_env': bool(srv_cfg.get('env')),
            'enabled': srv_cfg.get('enabled', True),
            'connected': is_connected,
            'tools_count': tools_count,
            'tool_names': tool_names,
            'server_version': server_version,
            'server_impl_name': server_impl_name,
            'breaker': breaker,
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

    data = parse_body()
    name = data.pop('name', '').strip()
    if not name:
        return api_bad_request('Server name is required', field='name')

    transport = data.get('transport', 'stdio')
    if transport == 'stdio' and not data.get('command'):
        return api_bad_request('command is required for stdio transport',
                                field='command')
    if transport == 'sse' and not data.get('url'):
        return api_bad_request('url is required for sse transport', field='url')

    cfg_upsert(name, data)
    logger.info('[MCP.v1] config upserted: %s (transport=%s)', name, transport)
    _invalidate_tool_latches(f'server upsert {name}')
    return jsonify({'ok': True, 'message': f'Server "{name}" configured'})


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
    _invalidate_tool_latches(f'server removal {name}')
    return jsonify({'ok': True, 'message': f'Server "{name}" removed'})


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
            return jsonify({'ok': False,
                            'error': f'Server "{target}" not in config'}), 404
        try:
            tools = bridge.connect_server(target, config[target])
            if not config[target].get('enabled', True):
                from lib.mcp.config import upsert_server as cfg_upsert
                updated = dict(config[target])
                updated['enabled'] = True
                cfg_upsert(target, updated)
                logger.info('[MCP.v1] re-enabled %s on connect', target)
            _invalidate_tool_latches(f'connect {target}')
            return jsonify({
                'ok': True,
                'server': target,
                'tools_count': len(tools),
                'tool_names': [t.name for t in tools],
            })
        except MCPConnectError as e:
            logger.error('[MCP.v1] connect %s failed: %s', target, e)
            return jsonify({
                'ok': False, 'error': str(e),
                'stderr_tail': e.stderr_tail or '',
            }), 500
        except Exception as e:
            logger.error('[MCP.v1] connect %s crashed: %s', target, e,
                         exc_info=True)
            return api_internal_error(e, source='api_v1.mcp.connect')

    try:
        result = bridge.connect_all()
        total_tools = sum(len(v) for v in result.values())
        _invalidate_tool_latches('connect_all')
        return jsonify({
            'ok': True,
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
            _invalidate_tool_latches(f'disconnect {target}')
            return jsonify({'ok': True,
                            'message': f'Disconnected from "{target}"'})
        except Exception as e:
            logger.error('[MCP.v1] disconnect %s failed: %s', target, e,
                         exc_info=True)
            return api_internal_error(e, source='api_v1.mcp.disconnect')

    try:
        bridge.disconnect_all()
        _invalidate_tool_latches('disconnect_all')
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
    from lib.mcp import get_bridge
    from lib.mcp.types import make_namespaced_name

    bridge = get_bridge()
    tools = []
    for server_info in bridge.list_servers():
        for tool_name in server_info['tool_names']:
            ns_name = make_namespaced_name(server_info['name'], tool_name)
            info = bridge.get_tool_info(ns_name)
            tools.append({
                'server': server_info['name'],
                'name': tool_name,
                'namespaced_name': ns_name,
                'description': info['description'] if info else '',
                'input_schema': info['input_schema'] if info else {},
            })
    return jsonify({
        'ok': True,
        'tools': tools,
        'total': len(tools),
        'servers_connected': bridge.server_count,
    })


# ── Catalog (App-Store) ─────────────────────────────────────────────

@api_v1_mcp_bp.route('/api/v1/mcp/catalog', methods=['GET'])
@require_auth
@api_meta(
    summary='Curated MCP server catalog',
    description=(
        'Returns each catalog entry annotated with ``installed`` / '
        '``connected`` / ``tools_count`` / ``server_version`` / '
        '``stored_env_keys`` (which env vars already have a stored value, '
        '*without* leaking the value).'
    ),
    tags=['mcp'],
)
def get_catalog_v1():
    from lib.mcp import get_bridge
    from lib.mcp.config import load_mcp_config
    from lib.mcp.registry import get_catalog

    config = load_mcp_config()
    bridge = get_bridge()
    connected_names = {s['name'] for s in bridge.list_servers()}

    def _live_meta(sid):
        """(tools_count, server_version, server_impl_name) for a connected server."""
        for s in bridge.list_servers():
            if s['name'] == sid:
                return (s['tools_count'],
                        s.get('server_version', '') or '',
                        s.get('server_impl_name', '') or '')
        return (0, '', '')

    entries = []
    catalog_ids = set()
    for entry in get_catalog():
        sid = entry['id']
        catalog_ids.add(sid)
        installed = sid in config
        connected = sid in connected_names
        tools_count, server_version, server_impl_name = (
            _live_meta(sid) if connected else (0, '', ''))
        stored_env = (config.get(sid, {}) or {}).get('env', {}) or {}
        stored_env_keys = [k for k, v in stored_env.items()
                           if isinstance(v, str) and v.strip()]
        entries.append({
            **entry,
            'installed': installed,
            'connected': connected,
            'tools_count': tools_count,
            'server_version': server_version,
            'server_impl_name': server_impl_name,
            'stored_env_keys': stored_env_keys,
            'breaker': bridge.get_breaker_state(sid),
        })

    # Surface servers that are configured in mcp_servers.json but have no
    # curated catalog entry, so they can never be silently invisible in the
    # settings panel. Synthesize a minimal "Custom" card from the stored
    # config. env values are NOT leaked — only their keys (as stored_env_keys).
    from lib.mcp.registry import CAT_CUSTOM
    for sid, srv_cfg in config.items():
        if sid in catalog_ids:
            continue
        connected = sid in connected_names
        tools_count, server_version, server_impl_name = (
            _live_meta(sid) if connected else (0, '', ''))
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
            'tags': ['custom'],
            'custom': True,
            'installed': True,
            'connected': connected,
            'tools_count': tools_count,
            'server_version': server_version,
            'server_impl_name': server_impl_name,
            'stored_env_keys': stored_env_keys,
            'breaker': bridge.get_breaker_state(sid),
        })

    return api_ok({'catalog': entries})


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
        return jsonify({'ok': False,
                        'error': f'Unknown server: {server_id}'}), 404

    existing_cfg = load_mcp_config().get(server_id, {})
    existing_env = existing_cfg.get('env', {}) or {}
    merged_env = dict(existing_env)
    for k, v in (env_values or {}).items():
        if isinstance(v, str) and v.strip():
            merged_env[k] = v

    for spec in entry.get('env_specs', []):
        if spec.get('required') and not str(merged_env.get(spec['key'], '')).strip():
            return jsonify({
                'ok': False,
                'error': f'Required: {spec.get("label", spec["key"])}',
            }), 400

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
    command = server_cfg.get('command', '') if server_cfg.get('transport', 'stdio') != 'sse' else ''
    if command:
        from lib.mcp.client import is_vendored_launcher, start_install_job
        if is_vendored_launcher(command):
            job = start_install_job(command)
            if job.get('state') == 'installing':
                return jsonify({
                    'ok': True,
                    'status': 'installing',
                    'id': server_id,
                    'message': f'{entry["name"]} 正在安装依赖…',
                }), 202
            if job.get('state') == 'error':
                from lib.mcp.client import _launcher_install_hint
                logger.error('[MCP.v1] catalog install: install of %s failed: %s',
                             server_id, job.get('detail'))
                return jsonify({
                    'ok': False,
                    'error': _launcher_install_hint(command),
                    'config_saved': True,
                    'stderr_tail': job.get('detail') or '',
                }), 500
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
        _invalidate_tool_latches(f'catalog install {server_id}')
        return jsonify({
            'ok': True,
            'status': 'ready',
            'message': f'{display_name} installed and connected',
            'tools_count': len(tools),
            'tool_names': [t.name for t in tools],
        })
    except MCPConnectError as e:
        logger.error('[MCP.v1] catalog install connect %s failed: %s',
                     server_id, e)
        return jsonify({
            'ok': False,
            'error': f'Config saved but connection failed.\n\n{e}',
            'config_saved': True,
            'stderr_tail': e.stderr_tail or '',
        }), 500
    except Exception as e:
        logger.error('[MCP.v1] catalog install crashed for %s: %s',
                     server_id, e, exc_info=True)
        return jsonify({
            'ok': False,
            'error': f'Config saved but connection failed: {e}',
            'config_saved': True,
        }), 500


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
    from flask import request

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
        return jsonify({'ok': False, 'error': f'Unknown server: {server_id}'}), 404

    command = (server_cfg.get('command', '')
               if server_cfg.get('transport', 'stdio') != 'sse' else '')
    job = get_install_job(command) if command else None

    # No job recorded (e.g. server restarted mid-install) — treat as unknown
    # and let the client re-POST install to restart cleanly.
    if job is None:
        return jsonify({'ok': True, 'status': 'unknown', 'id': server_id})

    state = job.get('state')
    if state == 'installing':
        return jsonify({'ok': True, 'status': 'installing', 'id': server_id}), 202
    if state == 'error':
        return jsonify({
            'ok': False,
            'status': 'error',
            'error': _launcher_install_hint(command),
            'config_saved': True,
            'stderr_tail': job.get('detail') or '',
        }), 500

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
        upsert_server as cfg_upsert,
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
        _invalidate_tool_latches(f'catalog uninstall/purge {server_id}')
        return jsonify({'ok': True,
                        'message': f'Uninstalled {server_id}',
                        'purged': True})

    config = load_mcp_config()
    if server_id in config:
        srv_cfg = dict(config[server_id])
        srv_cfg['enabled'] = False
        cfg_upsert(server_id, srv_cfg)
        audit_log('mcp_uninstall', server=server_id, mode='soft')
        logger.info('[MCP.v1] catalog uninstall (soft, env kept): %s',
                    server_id)
        _invalidate_tool_latches(f'catalog uninstall/soft {server_id}')
        return jsonify({
            'ok': True,
            'message': f'{server_id} disabled (credentials kept for re-enable)',
            'purged': False,
        })

    logger.warning('[MCP.v1] uninstall requested but not in config: %s',
                   server_id)
    return jsonify({'ok': True,
                    'message': f'{server_id} was not installed',
                    'purged': False})


__all__ = ['api_v1_mcp_bp']
