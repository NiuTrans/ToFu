"""Project domain schemas through declared HTTP response boundaries.

Entry points: project_v4_responses, native_contract_components,
native_contract_operation, schema_definition, rebase_inline_schema_refs. Dependencies: generated release/Conversation Sync
contracts, loaded only during spec construction. No routes, storage or workers.
"""
from __future__ import annotations

from copy import deepcopy
import re


def schema_definition(authority: dict, name: str) -> dict:
    """Keep a canonical definition and its reachable local schema resources."""
    definitions = authority.get('$defs', {})
    retained = {}

    def collect(value):
        if isinstance(value, dict):
            reference = value.get('$ref', '')
            if isinstance(reference, str) and reference.startswith('#/$defs/'):
                target = reference[len('#/$defs/'):].split('/')[0]
                target = target.replace('~1', '/').replace('~0', '~')
                if target not in retained:
                    retained[target] = deepcopy(definitions[target])
                    collect(retained[target])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    result = deepcopy(definitions[name])
    collect(result)
    if retained:
        result['$defs'] = retained
    return result


def rebase_inline_schema_refs(operation: dict, path: str, method: str) -> dict:
    """Embedded JSON Schema fragments resolve against their containing schema."""
    def pointer(value):
        return str(value).replace('~', '~0').replace('/', '~1')

    def rebase(value, root):
        if isinstance(value, dict):
            # A declared resource ID owns its own reference resolution.
            if '$id' in value:
                return deepcopy(value)
            return {key: root + child[1:] if key == '$ref' and isinstance(child, str)
                    and child.startswith('#/$defs/') else rebase(child, root)
                    for key, child in value.items()}
        if isinstance(value, list):
            return [rebase(child, root) for child in value]
        return value

    def visit(value, location):
        if isinstance(value, dict):
            # The release envelope moves a domain schema under properties.data.
            # Its local resource still owns refs there, including recursive refs.
            if '$defs' in value:
                return rebase(value, location)
            return {key: visit(child, location + '/' + pointer(key))
                    for key, child in value.items()}
        if isinstance(value, list):
            return [visit(child, location + '/' + str(index)) for index, child in enumerate(value)]
        return value

    return visit(operation, '#/paths/' + pointer(path) + '/' + method)


def _domain_schema(schema: dict | bool, components: dict) -> dict | bool:
    """Remove only the handler's top-level true transport sentinel."""
    if isinstance(schema, bool):
        return schema
    result = deepcopy(schema)
    reference = result.get('$ref', '')
    prefix = '#/components/schemas/'
    if isinstance(reference, str) and reference.startswith(prefix):
        target = components.get('schemas', {}).get(reference[len(prefix):])
        if target is not None:
            siblings = {key: value for key, value in result.items() if key != '$ref'}
            result = {'allOf': [deepcopy(target), siblings]} if siblings else deepcopy(target)
    for key in ('oneOf', 'anyOf', 'allOf'):
        if key in result:
            result[key] = [_domain_schema(child, components) for child in result[key]]
    properties = result.get('properties', {})
    if 'ok' in properties:
        flag = properties['ok']
        enums = flag.get('enum')
        true_only = flag.get('const') is True or (isinstance(enums, list)
                                                 and len(enums) == 1 and enums[0] is True)
        can_be_false = flag.get('const') is False or any(value is False for value in enums or [])
        boolean = flag.get('type') == 'boolean'
        if not boolean and not true_only and not can_be_false:
            return result
        if can_be_false or (boolean and not true_only):
            properties['ok'] = {'type': 'boolean', 'const': False}
        else:
            properties.pop('ok')
        required = result.get('required', [])
        false_only = flag.get('const') is False or (isinstance(enums, list)
                                                   and len(enums) == 1 and enums[0] is False)
        if 'ok' in required and not false_only:
            result['required'] = [name for name in required if name != 'ok']
    return result


def project_v4_responses(responses: dict, components: dict, *, success_envelope: bool) -> dict:
    """JSON product successes wrap data; generic HTTP failures are problems."""
    from lib.api_v4_generated import OPENAPI_DOCUMENT
    from lib.api_v4 import success_envelope as envelope

    result = deepcopy(responses)
    template = OPENAPI_DOCUMENT['components']['schemas']['ApiSuccessResponse']
    for status, response in result.items():
        content = response.get('content', {})
        if status == 'default' or (str(status).isdigit() and int(status) >= 400):
            if 'application/json' in content:
                response['content'] = {
                    **{kind: media for kind, media in content.items()
                       if kind != 'application/json'},
                    'application/problem+json': {
                        'schema': {'$ref': '#/components/schemas/Problem'},
                    },
                }
        elif (success_envelope and str(status).isdigit() and 200 <= int(status) < 300
              and int(status) not in (204, 205)):
            media = content.get('application/json')
            if media is not None:
                schema = deepcopy(template)
                schema['properties']['data'] = _domain_schema(media.get('schema', {}), components)
                media['schema'] = schema
                if 'example' in media:
                    media['example'] = envelope(media['example'], request_id='example', server_time_ms=0)
                for example in media.get('examples', {}).values():
                    if 'value' in example:
                        example['value'] = envelope(example['value'], request_id='example', server_time_ms=0)
    return result


def _namespace(value):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key == '$ref' and isinstance(child, str):
                for kind in ('schemas', 'parameters'):
                    prefix = f'#/components/{kind}/'
                    if child.startswith(prefix):
                        child = prefix + 'ConversationSyncV3_' + child[len(prefix):]
            result[key] = _namespace(child)
        return result
    if isinstance(value, list):
        return [_namespace(child) for child in value]
    return value


def native_contract_components(contract_id: str) -> dict:
    if contract_id != 'tofu.conversation-sync/v3':
        return {}
    from lib.conversation_sync.generated_contract import OPENAPI_PARAMETERS, OPENAPI_SCHEMAS

    return {
        'schemas': {'ConversationSyncV3_' + name: _namespace(schema)
                    for name, schema in OPENAPI_SCHEMAS.items()},
        'parameters': {'ConversationSyncV3_' + name: _namespace(parameter)
                       for name, parameter in OPENAPI_PARAMETERS.items()},
    }


def native_contract_path(contract_id: str, path: str) -> str:
    """Map framework argument names onto the generated public parameter names."""
    if contract_id != 'tofu.conversation-sync/v3':
        return path
    from lib.conversation_sync.generated_contract import OPENAPI_PATHS

    shape = re.sub(r'\{[^}]+\}', '{}', path)
    matches = [candidate for candidate in OPENAPI_PATHS
               if re.sub(r'\{[^}]+\}', '{}', candidate) == shape]
    if len(matches) != 1:
        raise ValueError(f'Native route has no unique generated contract path: {path}')
    return matches[0]


def native_contract_operation(contract_id: str, path: str, method: str) -> dict | None:
    if contract_id != 'tofu.conversation-sync/v3':
        return None
    from lib.conversation_sync.generated_contract import OPENAPI_PATHS

    operation = OPENAPI_PATHS.get(path, {}).get(method)
    return _namespace(operation) if operation is not None else None
