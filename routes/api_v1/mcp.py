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


# ── Config CRUD ──────────────────────────────────────────────────────

@api_v1_mcp_bp.route('/api/v1/mcp/servers', methods=['GET'])
@require_auth
@api_meta(
    summary='List configured MCP servers',
    description=(
        'Returns ``{servers: [...]}`` with connection status, tool count, '
        'and (when connected) the upstream server\'s reported version. '
        'The ``env`` block is intentionally stripped from each entry; '
        'use ``stored_env_keys`` to learn which keys have stored values.'
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
            bridge._disconnect_one(name)
            logger.info('[MCP.v1] disconnected %s before removal', name)
        except Exception as e:
            logger.warning('[MCP.v1] disconnect %s failed: %s', name, e)

    cfg_remove(name)
    logger.info('[MCP.v1] config removed: %s', name)
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
            bridge._disconnect_one(target)
            logger.info('[MCP.v1] disconnected %s', target)
            return jsonify({'ok': True,
                            'message': f'Disconnected from "{target}"'})
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

    entries = []
    for entry in get_catalog():
        sid = entry['id']
        installed = sid in config
        connected = sid in connected_names
        tools_count = 0
        server_version = ''
        server_impl_name = ''
        if connected:
            for s in bridge.list_servers():
                if s['name'] == sid:
                    tools_count = s['tools_count']
                    server_version = s.get('server_version', '') or ''
                    server_impl_name = s.get('server_impl_name', '') or ''
                    break
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
    from lib.mcp import get_bridge
    from lib.mcp.client import MCPConnectError
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

    bridge = get_bridge()
    try:
        tools = bridge.connect_server(server_id, server_cfg)
        return jsonify({
            'ok': True,
            'message': f'{entry["name"]} installed and connected',
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
            bridge._disconnect_one(server_id)
            logger.info('[MCP.v1] disconnected %s before uninstall '
                        '(purge=%s)', server_id, purge)
        except Exception as e:
            logger.warning('[MCP.v1] disconnect %s failed: %s', server_id, e)

    if purge:
        cfg_remove(server_id)
        audit_log('mcp_uninstall', server=server_id, mode='purge')
        logger.info('[MCP.v1] catalog uninstall (purge): %s', server_id)
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
