"""Structured, templated delegation contracts for ``spawn_agents``.

Responsibility: expand per-agent template references into ordinary agent
definitions (a template is a shared preset whose objective and context
precede each agent's own, so same-shape workers differ only in a sorted
parameter block), validate model-supplied result schemas, render the
worker-facing result contract, and parse/validate a worker's final JSON
answer.

Entry points: ``expand_agent_templates``, ``validate_output_schema``,
``output_contract_text``, ``parse_structured_answer``, ``canonical_json`` and
``SpawnContractError``.

Dependencies: ``jsonschema`` (Draft 2020-12) only. Pure functions: no task,
session, storage or provider state.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match

OUTPUT_SCHEMA_MAX_CHARS = 16384
CONTRACT_ERROR_MAX_CHARS = 300
_TEMPLATE_KEYS = frozenset({'objective', 'context', 'role', 'tools', 'output_schema'})
_FENCED_JSON = re.compile(r'```(?:json)?[ \t]*\n(.*?)```', re.DOTALL | re.IGNORECASE)


class SpawnContractError(ValueError):
    """A rejected template or result-schema argument, with its correction."""

    def __init__(self, code: str, message: str, next_action: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_action = next_action


def _reject(message: str, next_action: str) -> SpawnContractError:
    return SpawnContractError('invalid_spawn_contract', message, next_action)


def _validate_templates(templates: Any) -> dict:
    if templates is None:
        return {}
    if not isinstance(templates, dict):
        raise _reject('templates must be an object mapping names to templates.',
                      'Pass templates as {"<name>": {"objective": "..."}}.')
    for name, template in templates.items():
        if not isinstance(template, dict):
            raise _reject(f'templates.{name} must be an object.',
                          'Give every template an object of shared agent fields.')
        unknown = sorted(set(template) - _TEMPLATE_KEYS)
        if unknown:
            raise _reject(
                f'templates.{name} has unsupported field(s): {", ".join(unknown)}.',
                'Use only objective, context, role, tools and output_schema in a template.')
        for field in ('objective', 'context', 'role'):
            if field in template and not isinstance(template[field], str):
                raise _reject(f'templates.{name}.{field} must be a string.',
                              f'Pass {field} as a string or omit it.')
        tools = template.get('tools')
        if 'tools' in template and (
                not isinstance(tools, list) or not all(isinstance(t, str) for t in tools)):
            raise _reject(f'templates.{name}.tools must be an array of tool names.',
                          'Pass tools as an array of strings or omit it.')
        if 'output_schema' in template and not isinstance(template['output_schema'], dict):
            raise _reject(f'templates.{name}.output_schema must be a JSON Schema object.',
                          'Pass output_schema as an object with "type": "object".')
    return templates


def _parameter_block(params: dict) -> str:
    if not params:
        return ''
    lines = [
        f'- {key}: '
        + (value if isinstance(value, str)
           else json.dumps(value, ensure_ascii=False, sort_keys=True))
        for key, value in sorted(params.items())
    ]
    return '\n\n## Parameters\n' + '\n'.join(lines)


def expand_agent_templates(templates: Any, agents: list) -> list:
    """Return agent definitions with every ``template`` reference expanded.

    A template is a shared preset. A templated agent's objective is the
    template objective verbatim, then the agent's own objective, then its
    sorted parameter block (blank parts omitted), so same-shape workers differ
    only in that block (and the scheduler's objective de-duplication still sees
    distinct work). Context composes the same way, template first; agent-level
    ``role``/``tools``/``output_schema`` override the template's. Agents
    without ``template`` are returned unchanged; non-object entries are left
    for the caller's shape validation.
    """
    catalog = _validate_templates(templates)
    expanded: list = []
    for index, agent in enumerate(agents):
        if not isinstance(agent, dict):
            expanded.append(agent)
            continue
        if 'template' not in agent:
            if 'params' in agent:
                raise _reject(f'agents[{index}] has params without a template.',
                              'Name the template the params fill, or remove params.')
            expanded.append(agent)
            continue
        name = agent['template']
        if not isinstance(name, str) or name not in catalog:
            raise _reject(
                f'agents[{index}].template {name!r} is not defined in templates.',
                'Reference a name defined in the top-level templates object.')
        params = agent.get('params', {})
        if not isinstance(params, dict):
            raise _reject(f'agents[{index}].params must be a JSON object.',
                          'Pass params as {"<key>": <value>}.')
        template = catalog[name]
        for field in ('objective', 'context'):
            if agent.get(field) is not None and not isinstance(agent[field], str):
                raise _reject(f'agents[{index}].{field} must be a string.',
                              f'Pass {field} as a string or omit it.')
        objective = '\n\n'.join(
            part for part in (template.get('objective'), agent.get('objective'))
            if part and part.strip())
        if not objective:
            raise _reject(
                f'agents[{index}] has no objective: template {name!r} has none '
                'and the agent sets none.',
                'Give the template a shared objective or give this agent its own.')
        agent_context = agent.get('context')
        result = {key: copy.deepcopy(template[key])
                  for key in ('role', 'tools', 'output_schema') if key in template}
        result.update({key: value for key, value in agent.items()
                       if key not in ('template', 'params')})
        result['objective'] = objective + _parameter_block(params)
        context = '\n\n'.join(part for part in (template.get('context'), agent_context) if part)
        if context:
            result['context'] = context
        else:
            result.pop('context', None)
        expanded.append(result)
    return expanded


def validate_output_schema(schema: Any) -> dict:
    """Admit a model-supplied result schema: local, bounded, well-formed."""
    if not isinstance(schema, dict) or schema.get('type') != 'object':
        raise _reject('output_schema must be a JSON Schema object whose top-level type is "object".',
                      'Pass output_schema like {"type": "object", "properties": {...}, "required": [...]}.')
    if len(json.dumps(schema)) > OUTPUT_SCHEMA_MAX_CHARS:
        raise _reject(f'output_schema exceeds {OUTPUT_SCHEMA_MAX_CHARS} characters.',
                      'Keep the schema to the fields the next decision needs; '
                      'reference bulky material by path or artifact key.')
    pending: list = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if any(key in node for key in ('$id', '$anchor', '$dynamicAnchor')):
                raise _reject('output_schema cannot declare $id, $anchor or $dynamicAnchor.',
                              'Remove resource identifiers; use local "#/$defs/..." references.')
            for key in ('$ref', '$dynamicRef'):
                if key in node and (not isinstance(node[key], str)
                                    or not node[key].startswith('#/')):
                    raise _reject(f'output_schema {key} must be a local "#/..." reference.',
                                  'Inline the definition or reference "#/$defs/<name>".')
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise _reject(f'output_schema is not a valid JSON Schema: {error.message}'[:CONTRACT_ERROR_MAX_CHARS],
                      'Fix the schema so it validates as JSON Schema draft 2020-12.') from error
    return schema


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def output_contract_text(schema: dict) -> str:
    """The worker-facing result instruction for one validated schema."""
    return ('Finish with exactly one JSON object and nothing else (no prose, no code '
            'fence) that validates against this JSON Schema: '
            f'{canonical_json(schema)}. Put evidence inside the object; store bulky '
            'material in files or store_artifact and reference it.')


def _pointer(path) -> str:
    return '/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1') for part in path)


def parse_structured_answer(text: Any, schema: dict) -> tuple[dict | None, str]:
    """Decode and validate a final answer; return ``(value, '')`` or ``(None, error)``.

    Accepts raw JSON, one fenced ```json block, or prose around one outermost
    ``{...}`` object. The error is ``'<json pointer>: <message>'``, bounded.
    """
    stripped = text.strip() if isinstance(text, str) else ''
    candidates = [stripped]
    fenced = _FENCED_JSON.findall(stripped)
    if len(fenced) == 1:
        candidates.append(fenced[0].strip())
    start, end = stripped.find('{'), stripped.rfind('}')
    if 0 <= start < end:
        candidates.append(stripped[start:end + 1])
    value, decode_error = None, 'no JSON object found in the final answer'
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except ValueError as error:
            decode_error = f'invalid JSON: {error}'
            continue
        if isinstance(decoded, dict):
            value = decoded
            break
        decode_error = f'the final answer must be a JSON object, not {type(decoded).__name__}'
    if value is None:
        return None, f'/: {decode_error}'[:CONTRACT_ERROR_MAX_CHARS]
    error = best_match(Draft202012Validator(schema).iter_errors(value))
    if error is not None:
        return None, f'{_pointer(error.absolute_path)}: {error.message}'[:CONTRACT_ERROR_MAX_CHARS]
    return value, ''
