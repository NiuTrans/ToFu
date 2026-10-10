"""Owned Overleaf source selections and exact MCP artifact capability bindings.

The conversation repository owns project grants. This module projects enabled
bridge tools and checks their actual input/output schemas locally; a server name
or a provider-supplied schema hash never grants compile authority.
"""
from __future__ import annotations

import copy
import hashlib
from functools import lru_cache
from pathlib import PurePosixPath

from .contract import limit, schema, validate
from .errors import BuildError


def _shape(document):
    """Keep exact schema constraints and only their reachable local definitions."""
    definitions = document.get('$defs', {})
    if not isinstance(definitions, dict):
        raise BuildError('overleaf_capability', 'Invalid paper tool schema definitions')
    referenced = {}
    nodes = 0

    def visit(value, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 32 or nodes > 8192:
            raise BuildError('overleaf_capability', 'Paper tool schema exceeds its nesting limit')
        if isinstance(value, list):
            return [visit(item, depth + 1) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get('$ref')
        if reference is not None:
            if not isinstance(reference, str) or not reference.startswith('#/$defs/'):
                raise BuildError('overleaf_capability', 'Paper tool schema requires local references')
            name = reference.removeprefix('#/$defs/')
            if name not in definitions:
                raise BuildError('overleaf_capability', 'Paper tool schema reference is unavailable')
            if name not in referenced:
                referenced[name] = None
                referenced[name] = visit(definitions[name], depth + 1)
        return {key: visit(item, depth + 1) for key, item in value.items()}

    result = visit({key: value for key, value in document.items() if key != '$defs'})
    if referenced:
        result['$defs'] = referenced
    return result


@lru_cache(maxsize=8)
def _expected(name):
    authority = schema()
    return _shape({**authority['$defs'][name], '$defs': authority['$defs']})


def enabled_capabilities(bridge, server_name: str, *, owner_user_id: int, settings: dict) -> dict:
    from lib.identity import require_user_id
    require_user_id(owner_user_id, context='paper MCP capability owner')
    if settings.get('mcpEnabled', True) is not True:
        raise BuildError('overleaf_capability', 'MCP is disabled for this conversation')
    result = {}
    roles = schema()['x-mcp-tools']
    # The existing MCP policy exposes deployment-configured, enabled tools.
    # Owner-scoped document grants are a separate repository boundary below.
    for row in bridge.get_tool_catalog_snapshot():
        if row.get('server_name') != server_name:
            continue
        raw_metadata = row.get('meta')
        metadata = raw_metadata.get('tofu/paper-build') if isinstance(raw_metadata, dict) else None
        if not isinstance(metadata, dict) or type(metadata.get('schema_version')) is not int:
            continue
        role = metadata.get('role')
        if metadata['schema_version'] != 1 or not isinstance(role, str) or role not in roles:
            continue
        if role in result:
            raise BuildError('overleaf_capability', 'Select an unambiguous paper export capability')
        specification = roles[role]
        actual_input = ((row.get('openai_def') or {}).get('function') or {}).get('parameters')
        actual_output = row.get('output_schema')
        if not isinstance(actual_input, dict) or not isinstance(actual_output, dict):
            raise BuildError('overleaf_capability', 'Paper export schemas are unavailable')
        input_shape, output_shape = _shape(actual_input), _shape(actual_output)
        if (input_shape != _expected(specification['input'])
                or output_shape != _expected(specification['output'])
                or bool(row.get('read_only_hint')) != (role == 'read')):
            raise BuildError('overleaf_capability', 'Update the MCP paper export capability')
        name = row.get('namespaced_name')
        if not isinstance(name, str) or not name.startswith(f'mcp__{server_name}__'):
            raise BuildError('overleaf_capability', 'Paper tool address does not match its connection')
        fingerprint = {'name': name, 'input': input_shape, 'output': output_shape,
                       'metadata': metadata, 'annotations': row.get('annotations') or {}}
        from lib.mcp.documents import MCPDocumentError, encode_document
        try:
            fingerprint_text = encode_document(fingerprint)
        except MCPDocumentError as exc:
            raise BuildError('overleaf_capability', 'Invalid or oversized paper capability metadata') from exc
        result[role] = {'name': name, 'contract_hash': hashlib.sha256(fingerprint_text.encode()).hexdigest()}
    if set(result) != set(roles):
        raise BuildError('overleaf_capability', 'Connect an MCP server with the paper artifact capability')
    return result


def _main_file(value):
    if value is None:
        return None
    path = PurePosixPath(value)
    if (path.is_absolute() or '..' in path.parts or '\\' in value or ':' in value
            or any(ord(char) < 32 for char in value) or path.suffix.lower() not in ('.tex', '.ltx')):
        raise BuildError('overleaf_main_file', 'Choose a relative Overleaf main document')
    return path.as_posix()


def _sources(settings):
    values = settings.get('paperOverleafSources', [])
    if not isinstance(values, list) or len(values) > limit('overleaf_sources'):
        raise BuildError('overleaf_binding', 'Invalid Overleaf project selections')
    result = [validate(value, 'PaperOverleafSourceV1') for value in values]
    for source in result:
        if _main_file(source['main_file']) != source['main_file']:
            raise BuildError('overleaf_binding', 'Invalid Overleaf main document selection')
    keys = [(value['mcp_server'], value['project_id']) for value in result]
    if len(keys) != len(set(keys)):
        raise BuildError('overleaf_binding', 'Duplicate Overleaf project selection')
    return result


def _bridge(owner_user_id):
    from lib.mcp import get_bridge
    return get_bridge(owner_user_id)


def source_document(source: dict) -> dict:
    return validate({'source': 'overleaf', **{key: source[key] for key in (
        'project_id', 'mcp_server', 'main_file')},
        'label': f"{source['mcp_server']} / {source['project_id'][:8]}"
                 + (f" / {source['main_file']}" if source['main_file'] else '')}, 'PaperOverleafDocumentV1')


def discover_sources(settings: dict, *, owner_user_id: int, bridge=None) -> tuple[list, list]:
    """Read the existing catalog and owned grants; never connect or compile."""
    if settings.get('mcpEnabled', True) is not True:
        return [], []
    client = bridge or _bridge(owner_user_id)
    names = sorted({row['server_name'] for row in client.get_tool_catalog_snapshot()
                    if isinstance(row.get('server_name'), str) and 0 < len(row['server_name']) <= 160})
    capabilities = {}
    for name in names[:64]:
        try:
            capabilities[name] = enabled_capabilities(client, name, owner_user_id=owner_user_id, settings=settings)
        except BuildError:
            continue
    sources = [source_document(source) for source in _sources(settings)
               if capabilities.get(source['mcp_server']) == source['capabilities']]
    return sources, list(capabilities)


def select_source(payload: dict, *, owner_user_id: int, bridge=None) -> dict:
    from lib.conversations.settings_store import update_conversation_settings
    from .service import owned_settings
    from .document_repository import PaperDocumentRepository
    validate(payload, 'PaperOverleafSelectRequestV1')
    settings = owned_settings(payload['conv_id'], owner_user_id)
    bridge = bridge or _bridge(owner_user_id)
    source = validate({'project_id': payload['project_id'], 'mcp_server': payload['mcp_server'],
                       'main_file': _main_file(payload.get('main_file')), 'capabilities': enabled_capabilities(
                           bridge, payload['mcp_server'], owner_user_id=owner_user_id, settings=settings)},
                      'PaperOverleafSourceV1')
    selected = PaperDocumentRepository(owner_user_id).register(source_document(source))['source']

    def mutate(current):
        if current.get('mcpEnabled', True) is not True:
            raise BuildError('overleaf_capability', 'MCP is disabled for this conversation')
        sources = [value for value in _sources(current) if (
            value['mcp_server'], value['project_id']) != (source['mcp_server'], source['project_id'])]
        if len(sources) >= limit('overleaf_sources'):
            raise BuildError('overleaf_binding', 'Overleaf project selection limit reached')
        current['paperOverleafSources'] = [*sources, copy.deepcopy(source)]
        current['paperDocument'] = selected

    if update_conversation_settings(payload['conv_id'], mutate, user_id=owner_user_id) is None:
        raise BuildError('document_not_found', 'Paper conversation is unavailable')
    return validate({'source': source}, 'PaperOverleafSelectionResultV1')


def remove_source(payload: dict, *, owner_user_id: int) -> None:
    from lib.conversations.settings_store import update_conversation_settings
    validate(payload, 'PaperOverleafRemoveRequestV1')

    def mutate(current):
        current['paperOverleafSources'] = [source for source in _sources(current) if (
            source['mcp_server'], source['project_id']) != (payload['mcp_server'], payload['project_id'])]
        selected = current.get('paperDocument')
        if isinstance(selected, dict) and selected.get('source') == 'overleaf' and (
                selected.get('mcp_server'), selected.get('project_id')) == (payload['mcp_server'], payload['project_id']):
            current.pop('paperDocument', None)

    if update_conversation_settings(payload['conv_id'], mutate, user_id=owner_user_id) is None:
        raise BuildError('document_not_found', 'Paper conversation is unavailable')


def bound_source(payload: dict, *, owner_user_id: int, settings: dict, bridge) -> dict:
    selected = [source for source in _sources(settings) if source['project_id'] == payload['project_id']
                and (not payload.get('mcp_server') or source['mcp_server'] == payload['mcp_server'])]
    if len(selected) != 1:
        raise BuildError('overleaf_binding', 'Select this Overleaf project in the current conversation')
    source = selected[0]
    if payload.get('main_file') and payload['main_file'] != source['main_file']:
        raise BuildError('overleaf_binding', 'Select the requested Overleaf main document first')
    current = enabled_capabilities(bridge, source['mcp_server'], owner_user_id=owner_user_id, settings=settings)
    if source['capabilities'] != current:
        raise BuildError('overleaf_capability', 'Paper export capability changed; select the project again')
    return copy.deepcopy(source)
