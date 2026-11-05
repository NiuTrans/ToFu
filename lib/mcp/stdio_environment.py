"""Build and validate the exact MCP stdio child environment.

The bridge and read-only diagnostic command share this entry point. Explicit
server configuration wins; cache/proxy policy stays with the MCP launcher owner.

``env`` values may name a credential-vault entry instead of holding plaintext:

    {"GITHUB_PERSONAL_ACCESS_TOKEN": "vault:github_token"}

That is the portable form an internal export writes: the registration travels
without embedding a token, and each destination resolves the reference against
its own (carried) vault. Plaintext values keep working unchanged. Resolution is
fail-closed — an unresolvable reference raises instead of launching a server
with an empty credential, which would surface as an unexplained 401.
"""
import os
import re

from lib.tools.process_environment import apply_tool_paths, validate_environment

_VAULT_ENV_PREFIX = 'vault:'
_VAULT_ENTRY_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,63}$')


def resolve_vault_env_references(overlay: dict) -> dict:
    """Resolve ``vault:<entry>`` env values against the credential vault.

    Only this explicit prefix is interpreted; every other value passes through
    byte-identically. Values never enter logs or diagnostics.
    """
    references = {key: value for key, value in overlay.items()
                  if isinstance(value, str) and value.startswith(_VAULT_ENV_PREFIX)}
    if not references:
        return overlay
    from lib.credentials_vault import get_entry
    resolved = dict(overlay)
    for key, value in references.items():
        entry = value[len(_VAULT_ENV_PREFIX):].strip()
        if not _VAULT_ENTRY_RE.match(entry):
            raise ValueError(f'MCP env {key} names an invalid credential vault entry')
        secret = get_entry(entry)
        if not secret:
            raise ValueError(
                f'MCP env {key} references vault entry {entry!r}, which is not '
                f'configured. Add it in Settings → Advanced → 凭证保管库, or '
                f'set a literal value for this server.')
        resolved[key] = secret
    return resolved


def build_stdio_environment(config: dict, *, prepare_directories=True,
                            owner_user_id=None) -> dict[str, str]:
    from lib.mcp.client._vendor import _ensure_writable_caches, _propagate_proxy_env

    if not isinstance(config, dict):
        raise ValueError('MCP configuration must be an object')
    environment = dict(os.environ)
    overlay = config.get('env') or {}
    if not isinstance(overlay, dict):
        raise ValueError('MCP env configuration must be a string mapping')
    overlay = resolve_vault_env_references(overlay)
    validate_environment(overlay)
    environment.update(overlay)
    # Principal-owned cache partition is not configurable by the MCP overlay.
    # OS uid is a standalone process scope, never a Tofu tenant/owner proof.
    if owner_user_id is not None:
        if type(owner_user_id) is not int or owner_user_id < 1:
            raise ValueError('MCP owner_user_id must be a positive integer')
        environment['TOFU_MCP_OWNER_ID'] = 'tofu:' + str(owner_user_id)
    elif 'TOFU_MCP_OWNER_ID' in overlay:
        raise ValueError('MCP owner cache identity cannot be supplied in server env')
    else:
        environment.pop('TOFU_MCP_OWNER_ID', None)
    _propagate_proxy_env(environment)
    # New Node releases support this env switch. Older releases ignore it;
    # adding --use-env-proxy to NODE_OPTIONS makes those releases fail startup.
    # https://nodejs.org/download/release/v22.21.0/docs/api/cli.html
    environment.setdefault('NODE_USE_ENV_PROXY', '1')
    _ensure_writable_caches(environment, prepare_directories=prepare_directories)
    apply_tool_paths(environment)
    # An explicit PATH is an operator-selected search boundary.
    if 'PATH' in overlay:
        environment['PATH'] = overlay['PATH']
    validate_environment(environment)
    return environment
