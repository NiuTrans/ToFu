"""Small derived HTTP schema port; canonical dispatch definitions stay in JSON."""
from .repository import CONTRACT, _validate_schema


def schema(name):
    from lib.openapi import inline_schema_definition
    return inline_schema_definition(CONTRACT, name)


def query_parameters(name='listQuery'):
    definition = schema(name)
    return [{'name': name, 'in': 'query', 'required': name in definition['required'],
             'schema': value} for name, value in definition['properties'].items()]


def request_body(name):
    return {'required': True, 'content': {'application/json': {'schema': schema(name)}}}


def parse(name):
    from lib.request_parser import parse_body
    body = parse_body(exact=tuple(CONTRACT['$defs'][name]['properties']))
    _validate_schema(body, name)
    return body
