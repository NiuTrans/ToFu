"""lib/mcp/config.py — Persistent configuration for MCP servers.

Reads/writes ``data/config/mcp_servers.json``.

Config format::

    {
      "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": "YOUR_GITHUB_TOKEN"},
        "transport": "stdio",
        "enabled": true,
        "description": "GitHub PR/Issue management"
      },
      "tavily": {
        "command": "npx",
        "args": ["-y", "@anthropic/mcp-server-tavily"],
        "env": {"TAVILY_API_KEY": "tvly-xxx"},
        "enabled": true
      }
    }
"""

from __future__ import annotations

import json
import os
from typing import Any

from lib.log import get_logger
from lib.mcp.types import MCP_CONFIG_FILENAME, MCPServerConfig

logger = get_logger(__name__)

# ── Locate config dir ──
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG_DIR = os.path.join(_BASE_DIR, 'data', 'config')


def _config_path() -> str:
    return os.path.join(_CONFIG_DIR, MCP_CONFIG_FILENAME)


def load_mcp_config() -> dict[str, MCPServerConfig]:
    """Load MCP server configurations from disk.

    Returns:
        Dict mapping server_name → MCPServerConfig.
        Empty dict if no config file exists.
    """
    path = _config_path()
    if not os.path.isfile(path):
        logger.debug('[MCP:Config] No config file at %s', path)
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning('[MCP:Config] Config file is not a dict, ignoring: %s', path)
            return {}
        logger.info('[MCP:Config] Loaded %d server configs from %s', len(data), path)
        if _migrate_stale_entries(data):
            save_mcp_config(data)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('[MCP:Config] Failed to load config: %s', e)
        return {}


# Known stale entries from earlier versions that must be rewritten to use an
# auto-installing runner (otherwise a FileNotFoundError is raised when the
# bare executable is not on PATH). Preserves user-supplied env/credentials.
_STALE_COMMAND_MIGRATIONS: dict[str, dict[str, Any]] = {
    # Before v0.9.2 the Overleaf card shipped with `'command': 'overleaf-mcp'`
    # which only works if the user has pip-installed the package globally.
    # Switch to `uvx` which auto-installs from PyPI on first run.
    'overleaf': {
        'match_command': 'overleaf-mcp',
        'new_command': 'uvx',
        'new_args': ['--from', 'overleaf-mcp-plus[compile]', 'overleaf-mcp'],
    },
}


def _migrate_stale_entries(config: dict[str, Any]) -> bool:
    """Rewrite any known-stale server entries in-place. Returns True if mutated."""
    changed = False
    for name, rule in _STALE_COMMAND_MIGRATIONS.items():
        entry = config.get(name)
        if not isinstance(entry, dict):
            continue
        if entry.get('command') != rule['match_command']:
            continue
        old_cmd = entry.get('command')
        entry['command'] = rule['new_command']
        entry['args'] = list(rule['new_args'])
        logger.info(
            '[MCP:Config] Migrated stale entry %r: command %r -> %r (env preserved)',
            name, old_cmd, rule['new_command'],
        )
        changed = True
    return changed


def save_mcp_config(config: dict[str, MCPServerConfig]) -> bool:
    """Save MCP server configurations to disk.

    Args:
        config: Dict mapping server_name → MCPServerConfig.

    Returns:
        True on success, False on failure.
    """
    path = _config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info('[MCP:Config] Saved %d server configs to %s', len(config), path)
        return True
    except OSError as e:
        logger.error('[MCP:Config] Failed to save config: %s', e, exc_info=True)
        return False


def upsert_server(name: str, server_cfg: dict[str, Any]) -> dict[str, MCPServerConfig]:
    """Add or update a single MCP server config.

    Args:
        name: Server name (used as namespace in tool names).
        server_cfg: Server configuration dict.

    Returns:
        The updated full config dict.
    """
    config = load_mcp_config()
    config[name] = server_cfg
    save_mcp_config(config)
    logger.info('[MCP:Config] Upserted server %r', name)
    return config


def remove_server(name: str) -> dict[str, MCPServerConfig]:
    """Remove a MCP server config.

    Returns:
        The updated full config dict.
    """
    config = load_mcp_config()
    if name in config:
        del config[name]
        save_mcp_config(config)
        logger.info('[MCP:Config] Removed server %r', name)
    else:
        logger.warning('[MCP:Config] Server %r not found in config', name)
    return config
