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

import copy
import os
from typing import Any

from lib.json_store import JsonStoreReadError, read_json, update_json_atomic
from lib.log import get_logger
from lib.mcp.types import MCP_CONFIG_FILENAME, MCPServerConfig

logger = get_logger(__name__)

# ── Locate config dir (writable data root — see lib/runtime_paths) ──
from lib.runtime_paths import data_root
_CONFIG_DIR = os.path.join(data_root(), 'config')


def _config_path() -> str:
    return os.path.join(_CONFIG_DIR, MCP_CONFIG_FILENAME)


def _harden_config_permissions(path: str) -> None:
    """Best-effort migration: MCP env/header blocks often contain secrets."""
    if not os.path.exists(path):
        return
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.warning('[MCP:Config] Could not restrict %s to mode 0600: %s',
                       path, e)


def _locked_config_update(mutator):
    """Apply one strict, cross-process-safe config transaction."""
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def _mutate(current):
        if not isinstance(current, dict):
            raise JsonStoreReadError(
                f'MCP config is not a JSON object: {path}')
        return mutator(current)

    updated = update_json_atomic(
        path, _mutate, default={}, strict=True, indent=2, mode=0o600)
    if updated is not None:
        _harden_config_permissions(path)
    return updated


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
    _harden_config_permissions(path)
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        logger.warning('[MCP:Config] Config file is not a valid object, ignoring: %s',
                       path)
        return {}
    logger.info('[MCP:Config] Loaded %d server configs from %s', len(data), path)
    if not _migrate_stale_entries(data):
        return data

    # The probe above used an unlocked snapshot. Re-run the migration against
    # the latest config inside the write transaction so a concurrent server
    # add/remove cannot be overwritten by an automatic startup migration.
    outcome = {'latest': data}

    def _migrate_latest(latest):
        outcome['latest'] = latest
        if not _migrate_stale_entries(latest):
            return None
        return latest

    try:
        migrated = _locked_config_update(_migrate_latest)
        return migrated if migrated is not None else outcome['latest']
    except (JsonStoreReadError, OSError, TypeError, ValueError) as e:
        logger.error('[MCP:Config] Failed to persist config migration: %s',
                     e, exc_info=True)
        return data


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
    # overleaf-mcp-plus 0.3.0 migrated to MCP SDK v2; 0.3.1 fixes credential
    # independence, log retrieval and progressive discovery. Rewrite every
    # historical launcher shape we shipped (including the emergency
    # ``--with mcp<2`` override) to the reviewed release while preserving
    # credentials.
    'overleaf': [
        {
            'match_command': 'overleaf-mcp',
            'new_command': 'uvx',
            'new_args': [
                '--exclude-newer', '2026-08-14T00:00:00Z',
                '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp',
            ],
        },
        {
            'match_command': 'uvx',
            'match_args': [
                '--from', 'overleaf-mcp-plus[compile]>=0.1.3',
                '--with', 'mcp<2', 'overleaf-mcp',
            ],
            'new_command': 'uvx',
            'new_args': [
                '--exclude-newer', '2026-08-14T00:00:00Z',
                '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp',
            ],
        },
        {
            'match_command': 'uvx',
            'match_args': ['--from', 'overleaf-mcp-plus[compile]', 'overleaf-mcp'],
            'new_command': 'uvx',
            'new_args': [
                '--exclude-newer', '2026-08-14T00:00:00Z',
                '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp',
            ],
        },
        {
            'match_command': 'uvx',
            'match_args': [
                '--from', 'overleaf-mcp-plus[compile]>=0.1.3', 'overleaf-mcp',
            ],
            'new_command': 'uvx',
            'new_args': [
                '--exclude-newer', '2026-08-14T00:00:00Z',
                '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp',
            ],
        },
        {
            # Intermediate v2 config written before the reviewed cutoff was
            # made explicit. Upgrade it so an old, not-yet-restarted parent
            # can still cold-reconnect this exact release.
            'match_command': 'uvx',
            'match_args': [
                '--from', 'overleaf-mcp-plus[compile]==0.3.0', 'overleaf-mcp',
            ],
            'new_command': 'uvx',
            'new_args': [
                '--exclude-newer', '2026-08-14T00:00:00Z',
                '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp',
            ],
        },
        {
            # Current 0.3.0 installations already carry the old cutoff, so a
            # dedicated exact match is needed to move them to 0.3.1.
            'match_command': 'uvx',
            'match_args': [
                '--exclude-newer', '2026-08-02T00:00:00Z',
                '--from', 'overleaf-mcp-plus[compile]==0.3.0', 'overleaf-mcp',
            ],
            'new_command': 'uvx',
            'new_args': [
                '--exclude-newer', '2026-08-14T00:00:00Z',
                '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp',
            ],
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
    if not isinstance(config, dict):
        logger.error('[MCP:Config] Refusing to save non-object config')
        return False
    try:
        candidate = copy.deepcopy(config)
        updated = _locked_config_update(lambda _current: candidate)
        path = _config_path()
        logger.info('[MCP:Config] Saved %d server configs to %s', len(config), path)
        return updated is not None
    except (JsonStoreReadError, OSError, TypeError, ValueError) as e:
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
    def _upsert(config):
        config[name] = copy.deepcopy(server_cfg)
        return config

    config = _locked_config_update(_upsert)
    logger.info('[MCP:Config] Upserted server %r', name)
    return config


def patch_server(name: str, changes: dict[str, Any]) -> dict[str, MCPServerConfig] | None:
    """Atomically merge fields into one existing server row.

    Returns the updated full config, or ``None`` when the row did not exist.
    This avoids the stale ``load -> edit row -> upsert`` sequence used by
    toggle-style endpoints, which could otherwise erase a concurrent change
    to a different field on the same server.
    """
    outcome = {'found': False}
    patch = copy.deepcopy(changes)

    def _patch(config):
        current = config.get(name)
        if not isinstance(current, dict):
            return None
        outcome['found'] = True
        updated = copy.deepcopy(current)
        updated.update(patch)
        config[name] = updated
        return config

    config = _locked_config_update(_patch)
    if not outcome['found']:
        return None
    logger.info('[MCP:Config] Patched server %r fields=%s',
                name, sorted(patch))
    return config


def remove_server(name: str) -> dict[str, MCPServerConfig]:
    """Remove a MCP server config.

    Returns:
        The updated full config dict.
    """
    outcome = {'config': {}}

    def _remove(config):
        outcome['config'] = config
        if name not in config:
            return None
        del config[name]
        return config

    config = _locked_config_update(_remove)
    if config is not None:
        logger.info('[MCP:Config] Removed server %r', name)
        return config
    logger.warning('[MCP:Config] Server %r not found in config', name)
    return outcome['config']
