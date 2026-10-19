"""Shared local JSON Schema to TypeScript projection for generated domain DTOs.

Entry point: ts_type. Supports the explicit vocabulary used by artifact review
and paper builds; unsupported kinds fail generation instead of widening types.
"""
import json


def ts_type(rule: dict) -> str:
    if '$ref' in rule:
        return rule['$ref'].rsplit('/', 1)[1]
    if 'const' in rule:
        return json.dumps(rule['const'])
    if 'enum' in rule:
        return ' | '.join(json.dumps(v) for v in rule['enum'])
    if 'anyOf' in rule:
        return ' | '.join(ts_type(v) for v in rule['anyOf'])
    kind = rule['type']
    if isinstance(kind, list):
        return ' | '.join(ts_type({**rule, 'type': k}) for k in kind)
    if kind == 'array':
        return f'Array<{ts_type(rule["items"])}>'
    if kind == 'object':
        required = rule.get('required', [])
        return '{\n' + ''.join(
            f'  {k}{"" if k in required else "?"}: {ts_type(v)};\n'
            for k, v in rule['properties'].items()) + '}'
    return {'integer': 'number', 'number': 'number', 'string': 'string',
            'boolean': 'boolean', 'null': 'null'}[kind]
