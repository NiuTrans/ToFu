"""lib/openapi/_spec.py — ``build_spec`` document assembly.

Walks ``app.url_map`` and reads back each handler's ``_api_meta``
(stashed by :func:`lib.openapi._meta.api_meta`) to assemble a full
OpenAPI 3.1 document, pulling helpers from :mod:`lib.openapi._paths`
and :mod:`lib.openapi._schema`.
"""

from __future__ import annotations

from copy import deepcopy

from lib.log import get_logger

from lib.openapi._paths import (
    _auto_tags,
    _default_description,
    _flask_to_openapi_path,
    _path_parameters,
    _skip_rule,
)
from lib.openapi._schema import _components, _default_responses
from lib.openapi._wire import (
    native_contract_components,
    native_contract_operation,
    native_contract_path,
    project_v4_responses,
    rebase_inline_schema_refs,
)

logger = get_logger(__name__)


def build_spec(app, *, title: str = 'Tofu API',
               version: str | None = None,
               description: str = '') -> dict:
    """Walk ``app.url_map`` and build an OpenAPI 3.1 document."""
    paths: dict[str, dict] = {}

    from lib.api_v4 import NativeContractBlueprint
    components = _components()
    native_contracts = set()
    rules = list(app.url_map.iter_rules())

    for rule in rules:
        path = _flask_to_openapi_path(str(rule.rule))
        if _skip_rule(path):
            continue
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            continue
        meta = getattr(view, '_api_meta', None)
        blueprint = getattr(app, 'blueprints', {}).get(rule.endpoint.rsplit('.', 1)[0])
        native = isinstance(blueprint, NativeContractBlueprint)
        if native:
            path = native_contract_path(blueprint.native_contract_id, path)
        path_params = _path_parameters(str(rule.rule))
        # OPTIONS / HEAD don't need to be documented.
        methods = sorted(m for m in (rule.methods or set())
                          if m not in ('HEAD', 'OPTIONS'))
        if not methods:
            continue

        path_item = paths.setdefault(path, {})
        for method in methods:
            op_id = f'{rule.endpoint.replace(".", "_")}_{method.lower()}'
            op = {
                'operationId': op_id,
                'tags': (meta.get('tags') if meta else []) or _auto_tags(path),
                'summary': (meta.get('summary') if meta else '') or rule.endpoint,
                'description': meta.get('description', '') if meta else '',
                'parameters': path_params + (meta.get('parameters', []) if meta else []),
                'responses': (meta.get('responses') if meta else None) or _default_responses(),
            }
            if meta and meta.get('deprecated'):
                op['deprecated'] = True
            if meta:
                op.update(meta.get('extensions', {}))
            rb = (meta.get('request_body') if meta else None)
            if rb is False:
                pass
            elif rb:
                op['requestBody'] = rb
            elif method in ('POST', 'PUT', 'PATCH'):
                op['requestBody'] = {
                    'required': False,
                    'content': {'application/json': {
                        'schema': {'type': 'object',
                                   'additionalProperties': True}}},
                }
            required_scopes = getattr(view, '_required_scopes', None)
            if required_scopes:
                op['security'] = [{'bearerAuth': list(required_scopes)}]
            elif not (meta and meta.get('public')):
                scope = meta.get('scope') if meta else ''
                if scope:
                    op['security'] = [{'bearerAuth': [scope]}]
                else:
                    op['security'] = [{'bearerAuth': []}]
            else:
                op['security'] = []
            if native:
                contract_id = blueprint.native_contract_id
                if contract_id not in native_contracts:
                    for kind, values in native_contract_components(contract_id).items():
                        components.setdefault(kind, {}).update(values)
                    native_contracts.add(contract_id)
                generated = native_contract_operation(contract_id, path, method.lower())
                if generated is not None:
                    security = op['security']
                    op = generated
                    op.setdefault('security', security)
                op['x-tofu-http-response-contract'] = contract_id
            elif path.startswith('/api/v4/'):
                raw_success = bool(getattr(view, 'tofu_raw_success_response', False))
                enveloped = (bool(getattr(blueprint, 'tofu_v4_success_envelope', False))
                             and not raw_success)
                if raw_success:
                    op['x-tofu-raw-success-response'] = True
                op['responses'] = project_v4_responses(
                    op['responses'], components, success_envelope=enveloped)
                if enveloped:
                    op['x-tofu-http-response-contract'] = 'tofu.api/v4'
            if getattr(rule, 'websocket', False):
                op['x-tofu-transport'] = 'websocket'
                errors = {status: response for status, response in op['responses'].items()
                          if str(status).isdigit() and int(status) >= 400}
                op['responses'] = {'101': {'description': 'WebSocket upgrade'}, **errors}
            path_item[method.lower()] = rebase_inline_schema_refs(op, path, method.lower())

    spec = {
        'openapi': '3.1.0',
        'info': {
            'title': title,
            'version': version or '4.0.0',
            'description': description or _default_description(),
        },
        'servers': [{'url': '/', 'description': 'Same-origin'}],
        'security': [{'bearerAuth': []}],
        'paths': paths,
        'components': components,
        'tags': [
            {'name': 'chat', 'description': 'Chat completions (native + OpenAI/Anthropic compat)'},
            {'name': 'tasks', 'description': 'Long-running task lifecycle'},
            {'name': 'conversations', 'description': 'Conversation CRUD + branches'},
            {'name': 'capabilities', 'description': 'Self-describing model/tool registry'},
            {'name': 'agents', 'description': 'Higher-level agents (paper/translate/swarm/etc.)'},
            {'name': 'keys', 'description': 'API key administration'},
            {'name': 'webhooks', 'description': 'Outbound event delivery'},
            {'name': 'compat:openai', 'description': 'OpenAI-compatible adapter'},
            {'name': 'compat:anthropic', 'description': 'Anthropic-compatible adapter'},
        ],
    }
    # Bootstrap DTOs are already complete HTTP schemas, not handler payloads.
    # Merge only registered operations: disabled/absent surfaces stay absent.
    from lib.generated_api_v4 import OPENAPI_DOCUMENT
    for path, item in OPENAPI_DOCUMENT['paths'].items():
        if path in paths:
            for method, operation in item.items():
                if method in paths[path]:
                    paths[path][method] = deepcopy(operation)
    return spec
