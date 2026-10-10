"""Bind declared Overleaf write receipts to one authorized task/tool request.

The MCP bridge owns transport correlation and bounded structured results.
This boundary injects a fresh receipt identity, validates the published contract
and response, then hands committed facts to canonical turn projection.
"""
from __future__ import annotations

import uuid

from lib.project_mod.receipts import contract, FileReceiptError, relative_path
from lib.mcp.documents import decode_document, MCPDocumentError
from lib.mcp.result_content import MCPToolResult

CONTROL = contract()['x-mcp-write-protocol']['control_field']
META = contract()['x-mcp-write-protocol']['metadata_key']


def _definition(name):
    value = contract()
    return {**value['$defs'][name],
            **({'$defs': value['$defs']} if name == 'OverleafWriteReceiptV1' else {})}


def _declared(info):
    if not isinstance(info, dict):
        return False
    metadata = (info.get('meta') or {}).get(META)
    if not isinstance(metadata, dict) or type(metadata.get('schema_version')) is not int or metadata != {
            'schema_version': 1, 'source': 'overleaf'}:
        return False
    name = info.get('tool_name')
    parameters = (info.get('openai_def') or {}).get('function', {}).get('parameters', {})
    return (name in contract()['x-mcp-write-tools'] and info.get('read_only_hint') is False
            and parameters.get('properties', {}).get(CONTROL) == _definition('OverleafWriteControlV1')
            and info.get('output_schema') == _definition('OverleafWriteReceiptV1'))


def call_with_file_receipts(bridge, name, arguments, *, task):
    from ._mcp_local_paths import call_with_local_paths
    return call_with_local_paths(bridge, name, arguments, task=task,
        invoke=lambda routed, **options: _call_with_file_receipts(bridge, name, routed, task=task, **options))


def _call_with_file_receipts(bridge, name, arguments, *, task, local_cancel=None):
    info = bridge.get_tool_info(name)
    declared = _declared(info)
    advertised = isinstance(info, dict) and META in (info.get('meta') or {})
    if advertised and not declared:
        return MCPToolResult('MCP Error: File-write capability changed; refresh the MCP connection.', is_error=True)
    if not declared:
        if local_cancel is not None:
            return bridge.call_tool(name, arguments, cancel=local_cancel, retry_on_disconnect=False)
        return bridge.call_tool(name, arguments)
    owner = task.get('_userId')
    try:
        owner = int(owner) if not isinstance(owner, bool) else 0
        path = relative_path(arguments.get('file_path'))
        if owner <= 0 or not task.get('id'):
            raise ValueError('missing task owner')
    except (ValueError, TypeError, FileReceiptError):
        return MCPToolResult('MCP Error: File write requires an owned task and project-relative path.', is_error=True)
    request_id = uuid.uuid4().hex
    result = bridge.call_tool(name, {**arguments, CONTROL: {'request_id': request_id, 'owner_user_id': owner}},
        timeout_override=contract()['x-resource-limits']['overleaf_write_seconds'],
        cancel=task.get('abort_event'), retry_on_disconnect=False)
    if getattr(result, 'is_error', False):
        return result
    try:
        from jsonschema import Draft202012Validator, ValidationError
        current = bridge.get_tool_info(name)
        if (not getattr(result, 'structured_json', None) or not _declared(current) or any(
                current.get(key) != info.get(key) for key in
                ('server_name', 'tool_name', 'openai_def', 'output_schema', 'meta'))):
            raise FileReceiptError('missing structured proof or changed capability')
        receipt = decode_document(result)
        Draft202012Validator(_definition('OverleafWriteReceiptV1')).validate(receipt)
        if (receipt['request_id'] != request_id or receipt['owner_user_id'] != owner
                or receipt['project_id'] != arguments.get('project_id')):
            raise FileReceiptError('receipt identity does not match the requested write')
        entries = receipt['file_changes']
        if any(relative_path(entry['path']) != path or entry['action'] !=
               contract()['x-mcp-write-tools'][info['tool_name']] for entry in entries):
            raise FileReceiptError('receipt is outside the requested write set')
    except (FileReceiptError, MCPDocumentError, ValidationError, ValueError, TypeError):
        return MCPToolResult('MCP Error: File-write confirmation is invalid. Inspect the remote project before retrying.', is_error=True)
    from lib.tasks_pkg.commit_round._derive import note_overleaf_file_changes
    note_overleaf_file_changes(task['id'], owner, info['server_name'], receipt['project_id'],
        [{**entry, 'sourceRevision': receipt['source_revision']} for entry in entries])
    return result
