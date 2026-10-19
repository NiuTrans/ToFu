"""Closed HTTP contract port for project continuity; schemas remain canonical.

Entry points: document, schema, validate, request_body and parse. Dependencies:
standard library and lazily loaded JSON Schema. No runtime or storage is started.
Only committed, explicitly named local authorities can resolve references.
"""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path


AUTHORITIES = {
    'scope_changes': 'project_scope_change_v1.schema.json',
    'responsibilities': 'project_responsibility_v1.schema.json',
    'shared_files': 'project_shared_files_v1.schema.json',
    'dispatch': 'project_dispatch_v1.schema.json',

    'automation': 'automation_v1.schema.json',
}
ROOT = Path(__file__).resolve().parents[2] / 'contracts'


@lru_cache(maxsize=len(AUTHORITIES))
def document(domain):
    if domain not in AUTHORITIES:
        raise ValueError('Unknown project continuity contract')
    return json.loads((ROOT / AUTHORITIES[domain]).read_text(encoding='utf-8'))


def schema(domain, name):
    """Inline only fixed local resources, rejecting cyclic or remote references."""
    def expand(value, owner, ancestors=()):
        if isinstance(value, dict):
            if '$ref' in value:
                reference = value['$ref']
                resource, separator, pointer = reference.partition('#')
                target = owner
                if resource:
                    matches = [key for key, path in AUTHORITIES.items() if resource == path]
                    if len(matches) != 1:
                        raise ValueError('Project continuity references must use a declared local authority')
                    target = matches[0]
                identity = (target, pointer)
                if not separator or not pointer.startswith('/') or identity in ancestors:
                    raise ValueError('Invalid or recursive project continuity reference')
                referred = document(target)
                for part in pointer[1:].split('/'):
                    referred = referred[part.replace('~1', '/').replace('~0', '~')]
                resolved = expand(referred, target, (*ancestors, identity))
                siblings = {key: expand(item, owner, ancestors) for key, item in value.items() if key != '$ref'}
                return {'allOf': [resolved, siblings]} if siblings else resolved
            return {key: expand(item, owner, ancestors) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item, owner, ancestors) for item in value]
        return value
    return expand(deepcopy(document(domain)['$defs'][name]), domain)


def validate(domain, name, value):
    from jsonschema import Draft202012Validator
    error = next(Draft202012Validator(schema(domain, name)).iter_errors(value), None)
    if error is not None:
        raise ValueError('Invalid project continuity ' + name + ': ' + error.message)
    return value


def request_body(domain, name):
    return {'required': True, 'content': {'application/json': {'schema': schema(domain, name)}}}


def parse(domain, name):
    from lib.request_parser import parse_body
    definition = schema(domain, name)
    body = parse_body(strict=True, exact=tuple(definition.get('properties', {})))
    return validate(domain, name, body)
