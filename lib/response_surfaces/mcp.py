"""Read-only catalog/queue bridge, scoped to the authenticated owner.

Only a registered, enabled, read-only tool with the exact SDK operation name
can serve a resource view. Model content cannot choose arbitrary tool methods,
URLs, credentials, arguments or writes. The normal MCP bridge owns transport.
"""
import json
import re
from jsonschema import Draft202012Validator


def _tools(user_id):
    from lib.mcp.client import get_bridge
    bridge = get_bridge(user_id)
    _version, rows = bridge.get_tool_catalog_projection()
    safety = bridge.get_tool_safety()
    return bridge, [row for row in rows if row.get('tool_name') in ('compute_catalog', 'get_queue_resource')
                    and safety.get(row.get('namespaced_name')) is True]


def providers(user_id):
    _bridge, rows = _tools(user_id)
    values = []
    for row in rows:
        if row.get('tool_name') == 'get_queue_resource':
            values.append({'id': 'queue', 'connection': row['server_name'], 'requiresQueue': True})
            continue
        schema = (row.get('openai_def') or {}).get('function', {}).get('parameters', {})
        declared = schema.get('properties', {}).get('provider', {})
        choices = declared.get('enum', [declared.get('const')])
        # Unknown provider names are not guessed. Such tools can still return
        # a typed catalog whose card binds the exact provider and connection.
        for provider in choices if isinstance(choices, list) else []:
            if isinstance(provider, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', provider):
                values.append({'id': provider, 'connection': row['server_name']})
    return values


def read_catalog(connection, provider, *, user_id, cancelled, queue=None):
    if not isinstance(connection, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', connection):
        raise PermissionError('Invalid compute connection')
    bridge, rows = _tools(user_id)
    operation = 'get_queue_resource' if queue is not None else 'compute_catalog'
    if queue is not None and (not isinstance(queue, str) or not queue.strip() or len(queue) > 256):
        raise ValueError('Explicit queue name required')
    selected = [row for row in rows if row.get('server_name') == connection and row.get('tool_name') == operation]
    if len(selected) != 1:
        raise PermissionError('Read-only compute catalog unavailable')
    name = selected[0]['namespaced_name']
    info = bridge.get_tool_info(name)
    if not info or info.get('tool_name') != operation or bridge.get_tool_safety().get(name) is not True:
        raise PermissionError('Compute catalog capability changed')
    arguments = {'queue': queue, 'include_apps': False} if queue is not None else {'provider': provider}
    Draft202012Validator(info['input_schema']).validate(arguments)
    class Cancellation:
        def is_set(self):
            return cancelled()
    if cancelled():
        raise InterruptedError('Resource read cancelled')
    value = bridge.call_tool(name, arguments, cancel=Cancellation(), timeout_override=60, retry_on_disconnect=False)
    if cancelled():
        raise InterruptedError('Resource read cancelled')
    result = json.loads(value) if isinstance(value, str) else value
    if queue is not None:
        from tofu_environments.capacity import queue_resources
        return {'provider': provider, 'queues': [{'name': queue, 'resources': queue_resources(result, queue)}]}
    if not isinstance(result, dict) or result.get('ok') is False or result.get('provider') != provider:
        raise ValueError('Invalid compute catalog result')
    return result
