"""Storage-side optional request-list projection; no storage or application imports.

Both authorities implement contracts/request_inspector_projection_v1.json.
Only delta snapshots with declared counts can discard message bodies without
changing the existing inspector fold. Legacy and malformed rows stay complete.
"""
from __future__ import annotations

from lib.task_replay.generated_request_inspector_projection import SNAPSHOT_FIELDS


def project_request_inspector_event(event):
    if (not isinstance(event, dict) or event.get('type') != 'messages_snapshot'
            or 'prefixLen' not in event
            or type(event.get('messageCount')) is not int
            or type(event.get('toolsCount')) is not int):
        return event
    messages = event.get('newMessages')
    if messages is None or messages is False:
        messages = []
    if not isinstance(messages, list):
        return event
    projected = {key: event[key] for key in SNAPSHOT_FIELDS if key in event}
    tail = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        calls = message.get('tool_calls')
        if calls is None or calls is False:
            calls = []
        if not isinstance(calls, list):
            return event
        names = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get('function')
            name = function.get('name') if isinstance(function, dict) else call.get('name')
            names.append({'name': name})
        content = message.get('content')
        blocks = [
            {'type': 'tool_use', 'name': block.get('name')}
            for block in content
            if isinstance(block, dict) and block.get('type') == 'tool_use'
        ] if isinstance(content, list) else []
        if names or blocks:
            tail.append({'tool_calls': names, 'content': blocks})
    projected['newMessages'] = tail
    return projected
