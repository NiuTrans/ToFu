"""Small derived HTTP schema port; canonical dispatch definitions stay in JSON."""
from copy import deepcopy
from .repository import CONTRACT, _validate_schema


def schema(name):
    def inline(value):
        if isinstance(value, dict):
            if '$ref' in value:
                reference = value['$ref']
                if not reference.startswith('#/$defs/'):
                    raise ValueError('Dispatch contract references must remain local')
                target = CONTRACT
                for key in reference.removeprefix('#/').split('/'):
                    target = target[key.replace('~1', '/').replace('~0', '~')]
                return inline(target)
            return {key: inline(item) for key, item in value.items()}
        if isinstance(value, list):
            return [inline(item) for item in value]
        return value
    return inline(deepcopy(CONTRACT['$defs'][name]))


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
