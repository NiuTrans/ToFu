"""Build and validate the exact MCP stdio child environment.

The bridge and read-only diagnostic command share this entry point. Explicit
server configuration wins; cache/proxy policy stays with the MCP launcher owner.
"""
import os

from lib.tools.process_environment import apply_tool_paths, validate_environment


def build_stdio_environment(config: dict, *, prepare_directories=True) -> dict[str, str]:
    from lib.mcp.client._vendor import _ensure_writable_caches, _propagate_proxy_env

    if not isinstance(config, dict):
        raise ValueError('MCP configuration must be an object')
    environment = dict(os.environ)
    overlay = config.get('env') or {}
    if not isinstance(overlay, dict):
        raise ValueError('MCP env configuration must be a string mapping')
    validate_environment(overlay)
    environment.update(overlay)
    _propagate_proxy_env(environment)
    # New Node releases support this env switch. Older releases ignore it;
    # adding --use-env-proxy to NODE_OPTIONS makes those releases fail startup.
    # https://nodejs.org/download/release/v22.21.0/docs/api/cli.html
    environment.setdefault('NODE_USE_ENV_PROXY', '1')
    _ensure_writable_caches(environment, prepare_directories=prepare_directories)
    apply_tool_paths(environment, interpreter_first='PATH' not in overlay)
    # An explicit PATH is an operator-selected search boundary.
    if 'PATH' in overlay:
        environment['PATH'] = overlay['PATH']
    validate_environment(environment)
    return environment
