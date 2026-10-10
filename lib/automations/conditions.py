"""Bounded data-only conditions and JSON Pointer references.

Entry points: resolve_source, resolve_arguments, evaluate_condition. Missing
evidence is an explicit error except for `exists`; unavailable checks therefore
cannot silently become a false/no-update result. No expression is evaluated.
"""

from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any

from .errors import AutomationError
from .contract import LIMITS

MAX_JSON_DEPTH = LIMITS['jsonDepth']
MAX_JSON_NODES = LIMITS['jsonNodes']
_MISSING = object()


def check_json_bounds(value: Any, *, max_depth: int = MAX_JSON_DEPTH,
                      max_nodes: int = MAX_JSON_NODES) -> None:
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > max_depth or nodes > max_nodes:
            raise AutomationError('automation_document_too_complex', 'JSON exceeds depth or node budget')
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise AutomationError('invalid_json', 'JSON object keys must be strings')
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in {str, int, float, bool}:
            raise AutomationError('invalid_json', 'Only JSON values are accepted')
        elif isinstance(item, float) and not math.isfinite(item):
            raise AutomationError('invalid_json', 'JSON numbers must be finite')
        elif type(item) is int and abs(item) > LIMITS['safeIntegerMax']:
            raise AutomationError('invalid_json', 'JSON integers must round-trip exactly through browser clients')


def _pointer(value: Any, pointer: str) -> Any:
    if not isinstance(pointer, str) or len(pointer) > 512 or (pointer and not pointer.startswith('/')):
        raise AutomationError('invalid_source', 'Invalid JSON Pointer')
    if not pointer:
        return value
    for part in pointer[1:].split('/'):
        # RFC 6901 permits only ~0 and ~1 escapes. Do not interpret malformed
        # tokens as a different field or accept noncanonical array indexes.
        if re.search(r'~(?![01])', part):
            raise AutomationError('invalid_source', 'Invalid JSON Pointer escape')
        token = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict):
            value = value.get(token, _MISSING)
        elif isinstance(value, list) and re.fullmatch(r'0|[1-9][0-9]*', token):
            index = int(token)
            value = value[index] if index < len(value) else _MISSING
        else:
            return _MISSING
        if value is _MISSING:
            return value
    return value


def resolve_source(source: dict, evidence: dict, *, missing_ok: bool = False) -> Any:
    if not isinstance(source, dict) or set(source) != {'from', 'pointer'}:
        raise AutomationError('invalid_source', 'Sources require from and pointer')
    origin = source['from']
    if origin == 'event':
        value = evidence.get('event', _MISSING)
    elif isinstance(origin, str) and re.fullmatch(r'steps/[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', origin):
        value = evidence.get('steps', {}).get(origin[6:], _MISSING)
    else:
        raise AutomationError('invalid_source', 'Invalid evidence source')
    value = _pointer(value, source['pointer'])
    if value is _MISSING and not missing_ok:
        raise AutomationError('evidence_missing', f'No evidence for {origin}{source["pointer"]}')
    return value


def resolve_arguments(arguments: Any, evidence: dict) -> Any:
    check_json_bounds(arguments)

    def resolve(value):
        if isinstance(value, dict):
            if '$source' in value:
                if set(value) != {'$source'}:
                    raise AutomationError('invalid_source', 'A reference cannot also contain literal fields')
                return deepcopy(resolve_source(value['$source'], evidence))
            return {key: resolve(child) for key, child in value.items()}
        if isinstance(value, list):
            return [resolve(child) for child in value]
        return value

    return resolve(arguments)


def _same_json(left: Any, right: Any) -> bool:
    # JSON booleans are not numbers, even though Python equates True and 1.
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same_json(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right))
    return left == right


def evaluate_condition(condition: Any, evidence: dict) -> bool:
    check_json_bounds(condition)
    from jsonschema import Draft202012Validator
    from .contract import definition_schema
    validator = Draft202012Validator({'$ref': '#/$defs/condition',
                                      '$defs': definition_schema()['$defs']})
    error = next(validator.iter_errors(condition), None)
    if error is not None:
        raise AutomationError('invalid_condition', error.message)

    def evaluate(node):
        if type(node) is bool:
            return node
        if not isinstance(node, dict):
            raise AutomationError('invalid_condition', 'A condition must be data, not an expression')
        if set(node) == {'all'}:
            return all(evaluate(child) for child in node['all'])
        if set(node) == {'any'}:
            return any(evaluate(child) for child in node['any'])
        if set(node) == {'not'}:
            return not evaluate(node['not'])
        operator = node.get('operator')
        if operator == 'exists':
            return resolve_source(node['source'], evidence, missing_ok=True) is not _MISSING
        left = resolve_source(node['source'], evidence)
        right = node.get('value', _MISSING)
        if right is _MISSING:
            raise AutomationError('invalid_condition', 'Comparison requires a value')
        if operator in {'eq', 'ne'}:
            equal = _same_json(left, right)
            return equal if operator == 'eq' else not equal
        if operator == 'contains':
            if isinstance(left, list):
                return any(_same_json(item, right) for item in left)
            if isinstance(left, str) and isinstance(right, str):
                return right in left
            raise AutomationError('condition_type_mismatch', 'Contains requires an array or two strings')
        if operator not in {'gt', 'gte', 'lt', 'lte'}:
            raise AutomationError('invalid_condition', 'Unknown condition operator')
        if type(left) not in {int, float} or type(right) not in {int, float}:
            raise AutomationError('condition_type_mismatch', 'Ordered comparisons require numbers')
        return {'gt': left > right, 'gte': left >= right, 'lt': left < right, 'lte': left <= right}[operator]

    return evaluate(condition)
