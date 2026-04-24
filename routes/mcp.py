"""routes/mcp.py — API endpoints for MCP server management.

Provides CRUD operations for MCP server configurations and lifecycle
management (connect, disconnect, status).

Endpoints:
    GET    /api/mcp/servers          — list configured servers & connection status
    POST   /api/mcp/servers          — add/update a server config
    DELETE /api/mcp/servers/<name>   — remove a server config
    POST   /api/mcp/connect          — connect to all (or specific) servers
    POST   /api/mcp/disconnect       — disconnect all (or specific) servers
    GET    /api/mcp/tools            — list all discovered MCP tools
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from lib.log import get_logger

logger = get_logger(__name__)

mcp_bp = Blueprint('mcp', __name__)


# ═══════════════════════════════════════════════════════════
#  Config CRUD
# ═══════════════════════════════════════════════════════════

@mcp_bp.route('/api/mcp/servers', methods=['GET'])
def list_servers():
    """List all configured MCP servers with their connection status."""
    from lib.mcp import get_bridge
    from lib.mcp.config import load_mcp_config

    config = load_mcp_config()
    bridge = get_bridge()
    connected_servers = {s['name'] for s in bridge.list_servers()}

    servers = []
    for name, srv_cfg in config.items():
        is_connected = name in connected_servers
        # Get tool count if connected
        tools_count = 0
        tool_names = []
        if is_connected:
            for s in bridge.list_servers():
                if s['name'] == name:
                    tools_count = s['tools_count']
                    tool_names = s['tool_names']
                    break
        servers.append({
            'name': name,
            'config': {k: v for k, v in srv_cfg.items() if k != 'env'},  # hide secrets
            'has_env': bool(srv_cfg.get('env')),
            'enabled': srv_cfg.get('enabled', True),
            'connected': is_connected,
            'tools_count': tools_count,
            'tool_names': tool_names,
        })

    return jsonify({'ok': True, 'servers': servers})


@mcp_bp.route('/api/mcp/servers', methods=['POST'])
def upsert_server():
    """Add or update an MCP server configuration.

    Request body::

        {
            "name": "github",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_TOKEN": "ghp-xxx"},
            "transport": "stdio",
            "enabled": true,
            "description": "GitHub PR/Issue management"
        }
    """
    from lib.mcp.config import upsert_server as cfg_upsert

    data = request.get_json(silent=True) or {}
    name = data.pop('name', '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Server name is required'}), 400

    # Validate: either command (stdio) or url (sse) must be provided
    transport = data.get('transport', 'stdio')
    if transport == 'stdio' and not data.get('command'):
        return jsonify({'ok': False, 'error': 'command is required for stdio transport'}), 400
    if transport == 'sse' and not data.get('url'):
        return jsonify({'ok': False, 'error': 'url is required for sse transport'}), 400

    cfg_upsert(name, data)
    logger.info('[MCP:API] Server config upserted: %s (transport=%s)', name, transport)

    return jsonify({'ok': True, 'message': f'Server "{name}" configured'})


@mcp_bp.route('/api/mcp/servers/<name>', methods=['DELETE'])
def delete_server(name):
    """Remove an MCP server configuration and disconnect if running."""
    from lib.mcp import get_bridge
    from lib.mcp.config import remove_server as cfg_remove

    bridge = get_bridge()

    # Disconnect first if connected
    connected_names = {s['name'] for s in bridge.list_servers()}
    if name in connected_names:
        try:
            bridge._disconnect_one(name)
            logger.info('[MCP:API] Disconnected server %s before removal', name)
        except Exception as e:
            logger.warning('[MCP:API] Error disconnecting %s: %s', name, e)

    cfg_remove(name)
    logger.info('[MCP:API] Server config removed: %s', name)

    return jsonify({'ok': True, 'message': f'Server "{name}" removed'})


# ═══════════════════════════════════════════════════════════
#  Lifecycle management
# ═══════════════════════════════════════════════════════════

@mcp_bp.route('/api/mcp/connect', methods=['POST'])
def connect_servers():
    """Connect to MCP servers.

    Request body (optional)::

        {"server": "github"}    — connect to a specific server
        {}                      — connect to all enabled servers
    """
    from lib.mcp import get_bridge
    from lib.mcp.config import load_mcp_config

    data = request.get_json(silent=True) or {}
    target = data.get('server', '').strip()
    bridge = get_bridge()

    if target:
        # Connect a specific server
        config = load_mcp_config()
        if target not in config:
            return jsonify({'ok': False, 'error': f'Server "{target}" not in config'}), 404
        try:
            tools = bridge.connect_server(target, config[target])
            # Auto re-enable if previously soft-uninstalled
            if not config[target].get('enabled', True):
                from lib.mcp.config import upsert_server as cfg_upsert
                updated = dict(config[target])
                updated['enabled'] = True
                cfg_upsert(target, updated)
                logger.info('[MCP:API] Re-enabled %s on successful connect', target)
            return jsonify({
                'ok': True,
                'server': target,
                'tools_count': len(tools),
                'tool_names': [t.name for t in tools],
            })
        except Exception as e:
            logger.error('[MCP:API] Failed to connect %s: %s', target, e, exc_info=True)
            return jsonify({'ok': False, 'error': str(e)}), 500
    else:
        # Connect all enabled servers
        try:
            result = bridge.connect_all()
            total_tools = sum(len(v) for v in result.values())
            return jsonify({
                'ok': True,
                'servers': {k: {'tools': v} for k, v in result.items()},
                'total_tools': total_tools,
            })
        except Exception as e:
            logger.error('[MCP:API] Failed to connect all: %s', e, exc_info=True)
            return jsonify({'ok': False, 'error': str(e)}), 500


@mcp_bp.route('/api/mcp/disconnect', methods=['POST'])
def disconnect_servers():
    """Disconnect from MCP servers.

    Request body (optional)::

        {"server": "github"}    — disconnect a specific server
        {}                      — disconnect all servers
    """
    from lib.mcp import get_bridge

    data = request.get_json(silent=True) or {}
    target = data.get('server', '').strip()
    bridge = get_bridge()

    if target:
        try:
            bridge._disconnect_one(target)
            logger.info('[MCP:API] Disconnected server: %s', target)
            return jsonify({'ok': True, 'message': f'Disconnected from "{target}"'})
        except Exception as e:
            logger.error('[MCP:API] Failed to disconnect %s: %s', target, e, exc_info=True)
            return jsonify({'ok': False, 'error': str(e)}), 500
    else:
        try:
            bridge.disconnect_all()
            return jsonify({'ok': True, 'message': 'All MCP servers disconnected'})
        except Exception as e:
            logger.error('[MCP:API] Failed to disconnect all: %s', e, exc_info=True)
            return jsonify({'ok': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  Tool introspection
# ═══════════════════════════════════════════════════════════

@mcp_bp.route('/api/mcp/tools', methods=['GET'])
def list_tools():
    """List all MCP tools discovered across connected servers."""
    from lib.mcp import get_bridge

    bridge = get_bridge()
    tools = []
    for server_info in bridge.list_servers():
        for tool_name in server_info['tool_names']:
            from lib.mcp.types import make_namespaced_name
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



# ═══════════════════════════════════════════════════════════
#  Catalog — curated MCP server registry (App Store)
# ═══════════════════════════════════════════════════════════

@mcp_bp.route('/api/mcp/catalog', methods=['GET'])
def get_catalog():
    """Return the curated catalog of MCP servers.

    Annotates each entry with ``installed`` and ``connected`` status
    based on current config and bridge state.
    """
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

        # Get tool count if connected
        tools_count = 0
        if connected:
            for s in bridge.list_servers():
                if s['name'] == sid:
                    tools_count = s['tools_count']
                    break

        # Report which env keys already have a stored non-empty value
        # (boolean only — never leak the actual secret to the frontend).
        stored_env = (config.get(sid, {}) or {}).get('env', {}) or {}
        stored_env_keys = [
            k for k, v in stored_env.items()
            if isinstance(v, str) and v.strip()
        ]

        entries.append({
            **entry,
            'installed': installed,
            'connected': connected,
            'tools_count': tools_count,
            'stored_env_keys': stored_env_keys,
        })

    return jsonify({'ok': True, 'catalog': entries})


@mcp_bp.route('/api/mcp/catalog/install', methods=['POST'])
def install_from_catalog():
    """One-click install: save config + connect in one step.

    Request body::

        {
            "id": "github",
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "YOUR_TOKEN"}
        }
    """
    from lib.mcp import get_bridge
    from lib.mcp.config import upsert_server as cfg_upsert
    from lib.mcp.registry import build_server_config, get_catalog_entry

    data = request.get_json(silent=True) or {}
    server_id = data.get('id', '').strip()
    env_values = data.get('env', {})

    if not server_id:
        return jsonify({'ok': False, 'error': 'server id is required'}), 400

    entry = get_catalog_entry(server_id)
    if entry is None:
        return jsonify({'ok': False, 'error': f'Unknown server: {server_id}'}), 404

    # Merge with existing stored env (e.g. from a previous soft-uninstall).
    # User-supplied values take precedence; missing required ones fall back to stored.
    from lib.mcp.config import load_mcp_config
    existing_cfg = load_mcp_config().get(server_id, {})
    existing_env = existing_cfg.get('env', {}) or {}
    merged_env = dict(existing_env)
    for k, v in (env_values or {}).items():
        if isinstance(v, str) and v.strip():
            merged_env[k] = v

    # Validate required env vars against merged set
    for spec in entry.get('env_specs', []):
        if spec.get('required') and not str(merged_env.get(spec['key'], '')).strip():
            return jsonify({
                'ok': False,
                'error': f'Required: {spec.get("label", spec["key"])}',
            }), 400

    server_cfg = build_server_config(server_id, merged_env)
    if server_cfg is None:
        return jsonify({'ok': False, 'error': 'Failed to build config'}), 500
    server_cfg['enabled'] = True  # re-enable if it was soft-uninstalled

    # Save config
    cfg_upsert(server_id, server_cfg)
    logger.info('[MCP:API] Catalog install: %s', server_id)

    # Auto-connect
    bridge = get_bridge()
    try:
        tools = bridge.connect_server(server_id, server_cfg)
        return jsonify({
            'ok': True,
            'message': f'{entry["name"]} installed and connected',
            'tools_count': len(tools),
            'tool_names': [t.name for t in tools],
        })
    except Exception as e:
        logger.error('[MCP:API] Catalog install connect failed for %s: %s',
                     server_id, e, exc_info=True)
        # Config is saved, but connection failed
        return jsonify({
            'ok': False,
            'error': f'Config saved but connection failed: {e}',
            'config_saved': True,
        }), 500


@mcp_bp.route('/api/mcp/catalog/uninstall', methods=['POST'])
def uninstall_from_catalog():
    """Soft uninstall by default: disconnect + disable (keep env for easy re-enable).

    Pass ``purge: true`` to hard-delete the config entirely (forget credentials).

    Request body::

        {"id": "github"}               — soft (default): keep env, set enabled=false
        {"id": "github", "purge": true} — hard: remove config row completely
    """
    from lib.log import audit_log
    from lib.mcp import get_bridge
    from lib.mcp.config import (
        load_mcp_config,
        remove_server as cfg_remove,
        upsert_server as cfg_upsert,
    )

    data = request.get_json(silent=True) or {}
    server_id = data.get('id', '').strip()
    purge = bool(data.get('purge', False))
    if not server_id:
        return jsonify({'ok': False, 'error': 'server id is required'}), 400

    bridge = get_bridge()

    # Disconnect if connected
    connected_names = {s['name'] for s in bridge.list_servers()}
    if server_id in connected_names:
        try:
            bridge._disconnect_one(server_id)
            logger.info('[MCP:API] Disconnected %s before uninstall (purge=%s)',
                        server_id, purge)
        except Exception as e:
            logger.warning('[MCP:API] Error disconnecting %s: %s', server_id, e)

    if purge:
        cfg_remove(server_id)
        audit_log('mcp_uninstall', server=server_id, mode='purge')
        logger.info('[MCP:API] Catalog uninstall (purge): %s', server_id)
        return jsonify({'ok': True, 'message': f'Uninstalled {server_id}', 'purged': True})

    # Soft: keep config + env, just disable
    config = load_mcp_config()
    if server_id in config:
        srv_cfg = dict(config[server_id])
        srv_cfg['enabled'] = False
        cfg_upsert(server_id, srv_cfg)
        audit_log('mcp_uninstall', server=server_id, mode='soft')
        logger.info('[MCP:API] Catalog uninstall (soft, env kept): %s', server_id)
        return jsonify({
            'ok': True,
            'message': f'{server_id} disabled (credentials kept for re-enable)',
            'purged': False,
        })

    logger.warning('[MCP:API] Uninstall requested but not in config: %s', server_id)
    return jsonify({'ok': True, 'message': f'{server_id} was not installed', 'purged': False})
