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
#
# Each entry is a list of rules (matched in order). A rule is keyed by the
# server-config 'name' and matches if either:
#   - the config's 'command' field equals rule['match_command'], OR
#   - the full 'args' list equals rule['match_args'] (when present).
# If the rule's 'new_args' list differs from the current 'args', it is
# applied (so we can evolve the args when the upstream package changes).
_STALE_COMMAND_MIGRATIONS: dict[str, list[dict[str, Any]]] = {
    # Before v0.9.2 the Overleaf card shipped with `'command': 'overleaf-mcp'`
    # which only works if the user has pip-installed the package globally.
    # Switch to `uvx` which auto-installs from PyPI on first run.
    #
    # Between v0.9.2 and v0.9.3 the args list included playwright (unused, ~100 MB).
    # The 0.1.3 release of `overleaf-mcp-plus` drops that extra. Force an
    # args refresh so users get the slimmer install automatically.
    'overleaf': [
        {
            'match_command': 'overleaf-mcp',
            'new_command': 'uvx',
            'new_args': ['--from', 'overleaf-mcp-plus[compile]>=0.1.3', 'overleaf-mcp'],
        },
        {
            # uvx entry from 0.9.2 — refresh to pin >=0.1.3 so new installs
            # pull the slimmer (playwright-free) release.
            'match_command': 'uvx',
            'match_args': ['--from', 'overleaf-mcp-plus[compile]', 'overleaf-mcp'],
            'new_command': 'uvx',
            'new_args': ['--from', 'overleaf-mcp-plus[compile]>=0.1.3', 'overleaf-mcp'],
        },
    ],
}


def _rule_matches(entry: dict[str, Any], rule: dict[str, Any]) -> bool:
    if 'match_command' in rule and entry.get('command') != rule['match_command']:
        return False
    if 'match_args' in rule and entry.get('args') != rule['match_args']:
        return False
    return True


def _migrate_stale_entries(config: dict[str, Any]) -> bool:
    """Rewrite any known-stale server entries in-place. Returns True if mutated."""
    changed = False
    for name, rules in _STALE_COMMAND_MIGRATIONS.items():
        entry = config.get(name)
        if not isinstance(entry, dict):
            continue
        for rule in rules:
            if not _rule_matches(entry, rule):
                continue
            new_cmd = rule['new_command']
            new_args = list(rule['new_args'])
            # Skip if already up-to-date
            if entry.get('command') == new_cmd and entry.get('args') == new_args:
                continue
            old_cmd = entry.get('command')
            old_args = entry.get('args')
            entry['command'] = new_cmd
            entry['args'] = new_args
            logger.info(
                '[MCP:Config] Migrated %r: %r %s -> %r %s (env preserved)',
                name, old_cmd, old_args, new_cmd, new_args,
            )
            changed = True
            break  # only apply one rule per entry per run
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
