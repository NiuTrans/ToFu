"""Shared program admission, evidence and handoff contract.

The WASM JavaScript runtime uses these bounds, typed failures and evidence
checks. Its scheduling never overrides catalog, permission or approval policy.
"""
from __future__ import annotations
import json
from typing import Any, Callable

MAX_SOURCE_BYTES = 32 * 1024
MAX_TOOL_CALLS = 16
MAX_OUTPUT_BYTES = 1_048_576


class ProgramExecutionError(ValueError):
    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {'code': self.code, 'message': str(self), **self.detail}


def catalog_search_arguments(args: list[Any]) -> list[Any]:
    """Adapt the local builtin without coercing or widening the search schema."""
    from lib.tools.gateway import search_tools_schema

    schema = search_tools_schema()['function']['parameters']
    properties = schema['properties']
    names = ('query', 'namespace', 'limit', 'cursor')
    retry_hint = (
        'Retry catalog.search({query:"tool capability", namespace:"", '
        f'limit:{properties["limit"]["default"]}, cursor:""}}), or '
        'catalog.search(query, namespace, limit, cursor). '
        'Use only these keys; query must be a nonblank string, namespace and '
        'cursor strings, and limit an integer from '
        f'{properties["limit"]["minimum"]} to {properties["limit"]["maximum"]}.')

    def reject(message: str) -> None:
        raise ProgramExecutionError(
            'invalid_search_arguments', f'catalog.search: {message}',
            retry_hint=retry_hint)

    if not args:
        reject('query is required')
    object_form = isinstance(args[0], dict)
    if object_form:
        if len(args) != 1:
            reject('pass one options object, not an object plus positional arguments')
        values = args[0]
        if any(key not in properties for key in values):
            reject('unknown options key; allowed keys are query, namespace, limit, cursor')
    else:
        if len(args) > len(names):
            reject('expected at most four positional arguments')
        values = dict(zip(names, args))
    if 'query' not in values:
        reject('query is required')
    for name in names:
        if name not in values:
            continue
        value, rule = values[name], properties[name]
        if rule['type'] == 'string':
            if not isinstance(value, str):
                reject(f'{name} must be a string')
            if len(value) > rule['maxLength']:
                reject(f'{name} must contain at most {rule["maxLength"]} characters')
            if name == 'query' and not value.strip():
                reject('query must be a nonblank string')
        elif (not isinstance(value, int) or isinstance(value, bool)
              or not rule['minimum'] <= value <= rule['maximum']):
            reject(f'{name} must be an integer from {rule["minimum"]} '
                   f'to {rule["maximum"]} (not a boolean)')
    if not object_form:
        return args
    return [values['query'], values.get('namespace', ''),
            values.get('limit', properties['limit']['default']),
            values.get('cursor', '')]


def observation_payload(reason: Any, data: Any) -> dict[str, Any]:
    if not isinstance(reason, str) or not reason.strip():
        raise ProgramExecutionError('type_error',
                              'tools.observe expects a nonblank reason')
    if len(reason.encode('utf-8')) > 2048:
        raise ProgramExecutionError('input_limit',
                              'tools.observe reason exceeds 2048 bytes')
    return {'kind': 'observation', 'reason': reason, 'data': data}


def receipt_value(receipt: Any, observe: Callable[[Any, Any], None]) -> Any:
    if not isinstance(receipt, dict):
        raise ProgramExecutionError('type_error',
                              'tools.value expects a tool receipt')
    if receipt.get('status') != 'done':
        observe('tool_not_done', receipt)
    if receipt.get('output_truncated') is not False:
        observe('incomplete_tool_output', receipt)
    if 'output' not in receipt:
        observe('missing_tool_output', receipt)
    output = receipt['output']
    if isinstance(output, str):
        if len(output.encode('utf-8')) > MAX_OUTPUT_BYTES:
            raise ProgramExecutionError('input_limit',
                                  'tools.value input exceeds 1 MiB')
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output
    return output


def program_backend(run: dict[str, Any]) -> str:
    """Identify actual execution; preserve backend identity of historical runs."""
    declared = run.get('backend')
    if isinstance(declared, str) and declared in {'native_openai', 'local_javascript', 'local_toolscript'}:
        return declared
    return ('local_toolscript' if run.get('source') in (
        'execute_program', 'local_toolscript') else 'native_openai')
