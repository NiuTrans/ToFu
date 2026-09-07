#!/usr/bin/env python3
"""Generate Python/OpenAPI and browser artifacts from conversation-sync v3."""

from __future__ import annotations

import argparse
import json
import os
import pprint
import re
import sys
from typing import Any
from urllib.parse import urlencode

import yaml


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
SOURCE = os.path.join(ROOT, 'contracts', 'conversation_sync_v3.yaml')
PYTHON_OUTPUT = os.path.join(
    ROOT, 'lib', 'conversation_sync', 'generated_contract.py')
TYPESCRIPT_OUTPUT = os.path.join(
    ROOT, 'frontend', 'src', 'api', 'conversation-sync.generated.ts')


class _UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that turns duplicate contract keys into a hard failure."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            mark = key_node.start_mark
            raise ValueError(
                f'duplicate contract key {key!r} at line {mark.line + 1}'
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_contract() -> dict[str, Any]:
    with open(SOURCE, encoding='utf-8') as handle:
        document = yaml.load(handle, Loader=_UniqueKeyLoader)
    if not isinstance(document, dict):
        raise ValueError('conversation sync contract must be an object')
    if document.get('openapi') != '3.1.0':
        raise ValueError('conversation sync contract must use OpenAPI 3.1.0')
    contract_id = document.get('x-tofu-contract')
    if contract_id != 'tofu.conversation-sync/v3':
        raise ValueError('unexpected conversation sync contract id')
    schemas = document.get('components', {}).get('schemas')
    if not isinstance(schemas, dict) or not schemas:
        raise ValueError('conversation sync contract has no schemas')
    operations = _client_operations(document)
    event_operation = next(
        (item for item in operations if item['client_name'] == 'eventsUrl'),
        None,
    )
    if event_operation is None:
        raise ValueError('conversation sync contract must declare eventsUrl')
    if document.get('x-tofu-stream', {}).get('eventSource') != event_operation['path']:
        raise ValueError('x-tofu-stream.eventSource must equal the eventsUrl path')
    _command_retry_policy(document)
    _validate_turn_document_owners(schemas)
    return document


def _validate_turn_document_owners(schemas: dict[str, Any]) -> None:
    """Keep projection and settlement vocabulary behind named schemas."""
    expected_references = (
        ("TurnRecord", "projection", "TurnProjection"),
        ("TurnRecord", "settlement", "TurnSettlement"),
        ("TurnRuntimeStateChange", "settlement", "TurnSettlement"),
        ("UpdateTurnRequest", "projection", "TurnProjection"),
    )
    for schema_name, property_name, expected_name in expected_references:
        schema = schemas.get(schema_name)
        properties = schema.get("properties") if isinstance(schema, dict) else None
        child = properties.get(property_name) if isinstance(properties, dict) else None
        actual_name = _schema_ref_name(
            child,
            context=f"{schema_name}.{property_name}",
        )
        if actual_name != expected_name:
            raise ValueError(
                f"{schema_name}.{property_name} must reference {expected_name}"
            )
    attempt_event = schemas.get("AttemptEvent")
    event_properties = (
        attempt_event.get("properties") if isinstance(attempt_event, dict) else None
    )
    payload = event_properties.get("payload") if isinstance(event_properties, dict) else None
    payload_properties = payload.get("properties") if isinstance(payload, dict) else None
    for property_name, expected_name in (
        ("projection", "TurnProjection"),
        ("settlement", "TurnSettlement"),
    ):
        child = (
            payload_properties.get(property_name)
            if isinstance(payload_properties, dict) else None
        )
        actual_name = _schema_ref_name(
            child,
            context=f"AttemptEvent.payload.{property_name}",
        )
        if actual_name != expected_name:
            raise ValueError(
                f"AttemptEvent.payload.{property_name} must reference {expected_name}"
            )


_HTTP_METHODS = ('get', 'post', 'put', 'patch', 'delete')


def _command_retry_policy(document: dict[str, Any]) -> dict[str, Any]:
    """Validate the one generated retry policy for idempotent commands."""
    policy = document.get('x-tofu-command-retry')
    required = {
        'maxAttempts', 'baseDelayMs', 'minDelayMs', 'maxDelayMs',
        'jitterRatio', 'httpStatuses', 'storageCodes', 'transportCodes',
    }
    if not isinstance(policy, dict) or set(policy) != required:
        raise ValueError(
            'x-tofu-command-retry must declare exactly: '
            + ', '.join(sorted(required))
        )
    for name in ('maxAttempts', 'baseDelayMs', 'minDelayMs', 'maxDelayMs'):
        value = policy[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f'x-tofu-command-retry.{name} must be a non-negative integer')
    if not 2 <= policy['maxAttempts'] <= 10:
        raise ValueError('x-tofu-command-retry.maxAttempts must be between 2 and 10')
    if not policy['minDelayMs'] <= policy['baseDelayMs'] <= policy['maxDelayMs']:
        raise ValueError('x-tofu-command-retry delay bounds are inconsistent')
    jitter = policy['jitterRatio']
    if isinstance(jitter, bool) or not isinstance(jitter, (int, float)) \
            or not 0 <= jitter <= 1:
        raise ValueError('x-tofu-command-retry.jitterRatio must be between 0 and 1')
    statuses = policy['httpStatuses']
    if (
        not isinstance(statuses, list)
        or not statuses
        or any(isinstance(item, bool) or not isinstance(item, int)
               or item < 400 or item > 599 for item in statuses)
        or len(statuses) != len(set(statuses))
    ):
        raise ValueError('x-tofu-command-retry.httpStatuses is invalid')
    for name in ('storageCodes', 'transportCodes'):
        values = policy[name]
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(item, str) or not item for item in values)
            or len(values) != len(set(values))
        ):
            raise ValueError(f'x-tofu-command-retry.{name} is invalid')
    return policy


def _schema_ref_name(schema: Any, *, context: str) -> str:
    if not isinstance(schema, dict):
        raise ValueError(f'{context} must be a schema object')
    ref = schema.get('$ref')
    prefix = '#/components/schemas/'
    if not isinstance(ref, str) or not ref.startswith(prefix):
        raise ValueError(f'{context} must reference a named component schema')
    return ref[len(prefix):]


def _parameter(document: dict[str, Any], value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError('operation parameter must be an object')
    ref = value.get('$ref')
    if not ref:
        return value
    prefix = '#/components/parameters/'
    if not isinstance(ref, str) or not ref.startswith(prefix):
        raise ValueError(f'unsupported parameter reference {ref!r}')
    resolved = document['components'].get('parameters', {}).get(ref[len(prefix):])
    if not isinstance(resolved, dict):
        raise ValueError(f'unknown parameter reference {ref!r}')
    return resolved


def _client_operations(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Return validated client operations in canonical document order."""
    operations: list[dict[str, Any]] = []
    client_names: set[str] = set()
    operation_ids: set[str] = set()
    paths = document.get('paths')
    if not isinstance(paths, dict) or not paths:
        raise ValueError('conversation sync contract has no paths')
    for path, path_item in paths.items():
        if not isinstance(path, str) or not path.startswith('/api/v3/'):
            raise ValueError(f'conversation sync path must be versioned v3: {path!r}')
        if not isinstance(path_item, dict):
            raise ValueError(f'path item {path!r} must be an object')
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if operation is None:
                continue
            if not isinstance(operation, dict):
                raise ValueError(f'{method.upper()} {path} must be an object')
            client_name = operation.get('x-tofu-client')
            client_kind = operation.get('x-tofu-client-kind', 'json')
            operation_id = operation.get('operationId')
            if not isinstance(client_name, str) or not client_name:
                raise ValueError(f'{method.upper()} {path} has no x-tofu-client')
            if client_name in client_names:
                raise ValueError(f'duplicate x-tofu-client {client_name!r}')
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError(f'{method.upper()} {path} has no operationId')
            if operation_id in operation_ids:
                raise ValueError(f'duplicate operationId {operation_id!r}')
            if client_kind not in {'json', 'url'}:
                raise ValueError(
                    f'{method.upper()} {path} has invalid x-tofu-client-kind'
                )
            client_names.add(client_name)
            operation_ids.add(operation_id)

            parameters = [
                _parameter(document, value)
                for value in operation.get('parameters', [])
            ]
            path_names = re.findall(r'\{([A-Za-z][A-Za-z0-9_]*)\}', path)
            declared_path_names = {
                str(item.get('name')) for item in parameters
                if item.get('in') == 'path'
            }
            if set(path_names) != declared_path_names:
                raise ValueError(
                    f'{method.upper()} {path} path parameters do not match placeholders'
                )
            query_names = [
                str(item.get('name')) for item in parameters
                if item.get('in') == 'query'
            ]
            raw_fixed_query = operation.get('x-tofu-client-fixed-query', {})
            if not isinstance(raw_fixed_query, dict):
                raise ValueError(
                    f'{method.upper()} {path} x-tofu-client-fixed-query '
                    'must be an object'
                )
            fixed_query: dict[str, str] = {}
            query_parameters = {
                str(item.get('name')): item
                for item in parameters if item.get('in') == 'query'
            }
            for raw_name, raw_value in raw_fixed_query.items():
                name = str(raw_name)
                parameter = query_parameters.get(name)
                if parameter is None:
                    raise ValueError(
                        f'{method.upper()} {path} fixes undeclared query {name!r}'
                    )
                if not isinstance(raw_value, str):
                    raise ValueError(
                        f'{method.upper()} {path} fixed query {name!r} '
                        'must be a string'
                    )
                enum = parameter.get('schema', {}).get('enum')
                if not isinstance(enum, list) or raw_value not in enum:
                    raise ValueError(
                        f'{method.upper()} {path} fixed query {name!r} '
                        'must select a declared enum value'
                    )
                fixed_query[name] = raw_value
            request_body = operation.get('requestBody')
            request_schema = None
            request_body_present = isinstance(request_body, dict)
            if request_body_present and request_body.get('required'):
                request_schema = _schema_ref_name(
                    request_body.get('content', {}).get(
                        'application/json', {},
                    ).get('schema'),
                    context=f'{method.upper()} {path} request body',
                )

            responses = operation.get('responses', {})
            success = responses.get('200') if isinstance(responses, dict) else None
            content = success.get('content', {}) if isinstance(success, dict) else {}
            is_event_stream = 'text/event-stream' in content
            if client_kind == 'url':
                binary_media = [
                    media_type
                    for media_type, media_value in content.items()
                    if media_type.startswith('image/')
                    and isinstance(media_value, dict)
                    and media_value.get('schema', {}).get('format') == 'binary'
                ]
                if (
                    method != 'get'
                    or request_body_present
                    or is_event_stream
                    or not binary_media
                ):
                    raise ValueError(
                        f'{method.upper()} {path} URL client must be a GET '
                        'with binary image responses and no request body'
                    )
                response_schema = None
            else:
                response_schema = _schema_ref_name(
                    content.get(
                        'text/event-stream'
                        if is_event_stream
                        else 'application/json',
                        {},
                    ).get('schema'),
                    context=f'{method.upper()} {path} response',
                )
            operations.append({
                'client_name': client_name,
                'client_kind': client_kind,
                'operation_id': operation_id,
                'path': path,
                'method': method.upper(),
                'path_names': path_names,
                'query_names': query_names,
                'fixed_query': fixed_query,
                'request_schema': request_schema,
                'request_body_present': request_body_present,
                'response_schema': response_schema,
                'event_stream': is_event_stream,
                'idempotent_retry': bool(operation.get('x-tofu-idempotent-retry')),
                'timeout_ms': operation.get('x-tofu-client-timeout-ms'),
            })
    return operations


def render_python(document: dict[str, Any]) -> str:
    schemas = document['components']['schemas']
    parameters = document['components'].get('parameters', {})
    retry_policy = _command_retry_policy(document)
    return '\n'.join((
        '"""AUTO-GENERATED by scripts/gen_conversation_sync_contract.py.',
        '',
        'Canonical source: contracts/conversation_sync_v3.yaml.',
        'DO NOT EDIT BY HAND.',
        '"""',
        '',
        'from __future__ import annotations',
        '',
        f"CONTRACT_ID = {document['x-tofu-contract']!r}",
        f"STREAM_POLICY = {pprint.pformat(document.get('x-tofu-stream', {}), sort_dicts=True, width=100)}",
        f"COMMAND_RETRY_POLICY = {pprint.pformat(retry_policy, sort_dicts=True, width=100)}",
        f"OPENAPI_SCHEMAS = {pprint.pformat(schemas, sort_dicts=True, width=100)}",
        f"OPENAPI_PARAMETERS = {pprint.pformat(parameters, sort_dicts=True, width=100)}",
        f"OPENAPI_PATHS = {pprint.pformat(document.get('paths', {}), sort_dicts=True, width=100)}",
        '',
        "__all__ = ['COMMAND_RETRY_POLICY', 'CONTRACT_ID', 'OPENAPI_PARAMETERS',",
        "           'OPENAPI_PATHS', 'OPENAPI_SCHEMAS', 'STREAM_POLICY']",
        '',
    ))


def _typescript_type(schema: Any) -> str:
    if not isinstance(schema, dict) or not schema:
        return 'unknown'
    ref = schema.get('$ref')
    if isinstance(ref, str):
        return ref.rsplit('/', 1)[-1]
    if 'const' in schema:
        return json.dumps(schema['const'], ensure_ascii=False)
    enum = schema.get('enum')
    if isinstance(enum, list) and enum:
        return ' | '.join(json.dumps(value, ensure_ascii=False) for value in enum)
    variants = schema.get('oneOf') or schema.get('anyOf')
    if isinstance(variants, list):
        return ' | '.join(f'({_typescript_type(item)})' for item in variants)
    kind = schema.get('type')
    if kind == 'string':
        return 'string'
    if kind in {'integer', 'number'}:
        return 'number'
    if kind == 'boolean':
        return 'boolean'
    if kind == 'null':
        return 'null'
    if kind == 'array':
        return f'ReadonlyArray<{_typescript_type(schema.get("items", {}))}>'
    if kind == 'object' or 'properties' in schema:
        properties = schema.get('properties') or {}
        required = set(schema.get('required') or [])
        members = []
        for name, child in properties.items():
            optional = '' if name in required else '?'
            members.append(
                f'  {json.dumps(str(name))}{optional}: {_typescript_type(child)};')
        additional = schema.get('additionalProperties')
        if additional is True:
            members.append('  [key: string]: unknown;')
        elif isinstance(additional, dict):
            members.append(
                f'  [key: string]: {_typescript_type(additional)};')
        return '{\n' + '\n'.join(members) + '\n}'
    return 'unknown'


def _client_source(document: dict[str, Any]) -> str:
    runtime = r'''
type ContractSchema = Record<string, unknown>;

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function schemaRefName(ref: string): string {
  const prefix = '#/components/schemas/';
  if (!ref.startsWith(prefix)) throw new Error(`Unsupported contract reference ${ref}`);
  return ref.slice(prefix.length);
}

function jsonValuesEqual(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => jsonValuesEqual(item, right[index]));
  }
  const leftObject = record(left);
  const rightObject = record(right);
  if (!leftObject || !rightObject) return false;
  const fields = Object.keys(leftObject);
  return fields.length === Object.keys(rightObject).length
    && fields.every((field) => Object.prototype.hasOwnProperty.call(rightObject, field)
      && jsonValuesEqual(leftObject[field], rightObject[field]));
}

function isValidNode(schemaValue: unknown, value: unknown): boolean {
  const schema = record(schemaValue) ?? {};
  const ref = typeof schema.$ref === 'string' ? schema.$ref : '';
  if (ref) {
    const name = schemaRefName(ref);
    const target = (CONVERSATION_SYNC_SCHEMAS as Record<string, unknown>)[name];
    return target !== undefined && isValidNode(target, value);
  }
  if (Array.isArray(schema.oneOf)) {
    let matches = 0;
    for (const item of schema.oneOf) {
      if (isValidNode(item, value)) {
        matches += 1;
        if (matches > 1) return false;
      }
    }
    return matches === 1;
  }
  if (Array.isArray(schema.anyOf)) {
    return schema.anyOf.some((item) => isValidNode(item, value));
  }
  if (Object.prototype.hasOwnProperty.call(schema, 'const')
      && !jsonValuesEqual(value, schema.const)) {
    return false;
  }
  if (Array.isArray(schema.enum)
      && !schema.enum.some((choice) => jsonValuesEqual(value, choice))) return false;

  const kind = schema.type;
  if (kind === 'null') return value === null;
  if (kind === 'string') {
    return typeof value === 'string'
      && (typeof schema.minLength !== 'number' || value.length >= schema.minLength)
      && (typeof schema.maxLength !== 'number' || value.length <= schema.maxLength)
      && (typeof schema.pattern !== 'string' || (new RegExp(schema.pattern)).test(value));
  }
  if (kind === 'integer' || kind === 'number') {
    return typeof value === 'number'
      && Number.isFinite(value)
      && (kind !== 'integer' || Number.isInteger(value))
      && (typeof schema.minimum !== 'number' || value >= schema.minimum)
      && (typeof schema.maximum !== 'number' || value <= schema.maximum);
  }
  if (kind === 'boolean') return typeof value === 'boolean';
  if (kind === 'array') {
    if (!Array.isArray(value)) return false;
    if (typeof schema.maxItems === 'number' && value.length > schema.maxItems) {
      return false;
    }
    return value.every((item) => isValidNode(schema.items, item));
  }
  if (kind === 'object' || schema.properties) {
    const object = record(value);
    if (!object) return false;
    const fields = Object.keys(object);
    if (typeof schema.minProperties === 'number'
        && fields.length < schema.minProperties) return false;
    if (typeof schema.maxProperties === 'number'
        && fields.length > schema.maxProperties) return false;

    const propertyNames = record(schema.propertyNames);
    if (propertyNames
        && fields.some((field) => !isValidNode(propertyNames, field))) return false;
    const required = Array.isArray(schema.required) ? schema.required : [];
    for (const field of required) {
      if (typeof field === 'string'
          && !Object.prototype.hasOwnProperty.call(object, field)) return false;
    }

    const properties = record(schema.properties) ?? {};
    const additionalSchema = record(schema.additionalProperties);
    for (const field of fields) {
      if (Object.prototype.hasOwnProperty.call(properties, field)) {
        if (!isValidNode(properties[field], object[field])) return false;
      } else if (schema.additionalProperties === false) {
        return false;
      } else if (additionalSchema && !isValidNode(additionalSchema, object[field])) {
        return false;
      }
    }
    return true;
  }
  return true;
}

function validateNode(schemaValue: unknown, value: unknown, path: string): string[] {
  const schema = record(schemaValue) ?? {};
  const ref = typeof schema.$ref === 'string' ? schema.$ref : '';
  if (ref) {
    const name = schemaRefName(ref);
    const target = (CONVERSATION_SYNC_SCHEMAS as Record<string, unknown>)[name];
    return target ? validateNode(target, value, path) : [`${path}: unknown schema ${name}`];
  }
  if (Array.isArray(schema.oneOf)) {
    const matches = schema.oneOf.filter((item) => validateNode(item, value, path).length === 0);
    return matches.length === 1 ? [] : [`${path}: expected exactly one contract variant`];
  }
  if (Array.isArray(schema.anyOf)) {
    return schema.anyOf.some((item) => validateNode(item, value, path).length === 0)
      ? [] : [`${path}: expected a contract variant`];
  }
  if (Object.prototype.hasOwnProperty.call(schema, 'const')
      && !jsonValuesEqual(value, schema.const)) {
    return [`${path}: expected ${JSON.stringify(schema.const)}`];
  }
  if (Array.isArray(schema.enum)
      && !schema.enum.some((choice) => jsonValuesEqual(value, choice))) {
    return [`${path}: value is outside the declared vocabulary`];
  }
  const kind = schema.type;
  if (kind === 'null') return value === null ? [] : [`${path}: expected null`];
  if (kind === 'string') {
    if (typeof value !== 'string') return [`${path}: expected string`];
    if (typeof schema.minLength === 'number' && value.length < schema.minLength) {
      return [`${path}: string is shorter than ${schema.minLength}`];
    }
    if (typeof schema.maxLength === 'number' && value.length > schema.maxLength) {
      return [`${path}: string is longer than ${schema.maxLength}`];
    }
    if (typeof schema.pattern === 'string' && !(new RegExp(schema.pattern)).test(value)) {
      return [`${path}: string does not match the declared pattern`];
    }
    return [];
  }
  if (kind === 'integer' || kind === 'number') {
    if (typeof value !== 'number' || !Number.isFinite(value)
        || (kind === 'integer' && !Number.isInteger(value))) {
      return [`${path}: expected ${kind}`];
    }
    if (typeof schema.minimum === 'number' && value < schema.minimum) {
      return [`${path}: number is below ${schema.minimum}`];
    }
    if (typeof schema.maximum === 'number' && value > schema.maximum) {
      return [`${path}: number is above ${schema.maximum}`];
    }
    return [];
  }
  if (kind === 'boolean') return typeof value === 'boolean' ? [] : [`${path}: expected boolean`];
  if (kind === 'array') {
    if (!Array.isArray(value)) return [`${path}: expected array`];
    if (typeof schema.maxItems === 'number' && value.length > schema.maxItems) {
      return [`${path}: array exceeds ${schema.maxItems} items`];
    }
    const errors: string[] = [];
    value.forEach((item, index) => errors.push(...validateNode(schema.items, item, `${path}[${index}]`)));
    return errors;
  }
  if (kind === 'object' || schema.properties) {
    const object = record(value);
    if (!object) return [`${path}: expected object`];
    const properties = record(schema.properties) ?? {};
    const required = Array.isArray(schema.required) ? schema.required : [];
    const errors: string[] = [];
    if (typeof schema.minProperties === 'number'
        && Object.keys(object).length < schema.minProperties) {
      errors.push(`${path}: object has too few properties`);
    }
    if (typeof schema.maxProperties === 'number'
        && Object.keys(object).length > schema.maxProperties) {
      errors.push(`${path}: object has too many properties`);
    }
    const propertyNames = record(schema.propertyNames);
    if (propertyNames) {
      for (const field of Object.keys(object)) {
        errors.push(...validateNode(propertyNames, field, `${path}.${field}`));
      }
    }
    for (const field of required) {
      if (typeof field === 'string' && !Object.prototype.hasOwnProperty.call(object, field)) {
        errors.push(`${path}.${field}: required`);
      }
    }
    for (const [field, child] of Object.entries(properties)) {
      if (Object.prototype.hasOwnProperty.call(object, field)) {
        errors.push(...validateNode(child, object[field], `${path}.${field}`));
      }
    }
    if (schema.additionalProperties === false) {
      for (const field of Object.keys(object)) {
        if (!Object.prototype.hasOwnProperty.call(properties, field)) {
          errors.push(`${path}.${field}: undeclared field`);
        }
      }
    } else {
      const additionalSchema = record(schema.additionalProperties);
      if (additionalSchema) {
        for (const [field, child] of Object.entries(object)) {
          if (!Object.prototype.hasOwnProperty.call(properties, field)) {
            errors.push(...validateNode(additionalSchema, child, `${path}.${field}`));
          }
        }
      }
    }
    return errors;
  }
  return [];
}

export class ConversationSyncContractError extends Error {
  readonly schemaName: string;
  readonly violations: readonly string[];

  constructor(schemaName: string, violations: readonly string[]) {
    super(`${schemaName} contract violation: ${violations.slice(0, 3).join('; ')}`);
    this.name = 'ConversationSyncContractError';
    this.schemaName = schemaName;
    this.violations = violations;
  }
}

export function assertConversationSyncSchema<T>(schemaName: string, value: unknown): T {
  const schema = (CONVERSATION_SYNC_SCHEMAS as Record<string, unknown>)[schemaName];
  if (!schema) throw new ConversationSyncContractError(schemaName, ['schema is not registered']);
  if (isValidNode(schema, value)) return value as T;
  const violations = validateNode(schema, value, '$');
  if (violations.length) throw new ConversationSyncContractError(schemaName, violations);
  return value as T;
}

function snapshotConversationCommand<T>(schemaName: string, value: unknown): T {
  const validated = assertConversationSyncSchema<T>(schemaName, value);
  // Retry the exact accepted document even if application code mutates its
  // original object while the first network attempt is in flight.
  return JSON.parse(JSON.stringify(validated)) as T;
}

export const decodeConversationSyncSnapshot = (value: unknown): ConversationSyncSnapshot =>
  assertConversationSyncSchema<ConversationSyncSnapshot>('ConversationSyncSnapshot', value);

export const decodeConversationSyncEvent = (value: unknown): ConversationSyncEvent =>
  assertConversationSyncSchema<ConversationSyncEvent>('ConversationSyncEvent', value);

export const decodeConversationInvalidation = (value: unknown): ConversationInvalidation =>
  assertConversationSyncSchema<ConversationInvalidation>('ConversationInvalidation', value);

const RETRYABLE_COMMAND_HTTP_STATUSES = new Set<number>(
  CONVERSATION_SYNC_COMMAND_RETRY_POLICY.httpStatuses,
);
const RETRYABLE_COMMAND_STORAGE_CODES = new Set<string>(
  CONVERSATION_SYNC_COMMAND_RETRY_POLICY.storageCodes,
);
const RETRYABLE_COMMAND_TRANSPORT_CODES = new Set<string>(
  CONVERSATION_SYNC_COMMAND_RETRY_POLICY.transportCodes,
);

function commandFailureCode(error: unknown): string {
  const failure = record(error) ?? {};
  const body = record(failure.body) ?? {};
  const bodyError = record(body.error);
  const envelope = record(failure.envelope);
  return String(envelope?.storageCode ?? bodyError?.storageCode
    ?? body.storageCode ?? envelope?.kind ?? bodyError?.kind ?? '');
}

function retryableCommandFailure(
  error: unknown, options: RequestOptions,
): boolean {
  if (options.signal?.aborted) return false;
  const failure = record(error) ?? {};
  const status = Number(failure.status || 0);
  const transportCode = String(failure.code || '');
  return RETRYABLE_COMMAND_HTTP_STATUSES.has(status)
    || RETRYABLE_COMMAND_STORAGE_CODES.has(commandFailureCode(error))
    || RETRYABLE_COMMAND_TRANSPORT_CODES.has(transportCode);
}

function abortReason(signal?: AbortSignal): Error {
  if (signal?.reason instanceof Error) return signal.reason;
  const error = new Error('The conversation command was aborted.');
  error.name = 'AbortError';
  return error;
}

async function waitForRetry(delayMs: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) throw abortReason(signal);
  await new Promise<void>((resolve, reject) => {
    const finish = (): void => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    };
    const timer = setTimeout(finish, delayMs);
    const onAbort = (): void => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      reject(abortReason(signal));
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

async function idempotentCommand<T>(
  send: () => Promise<T>, body: { commandId: string }, options: RequestOptions,
): Promise<T> {
  const policy = CONVERSATION_SYNC_COMMAND_RETRY_POLICY;
  for (let attempt = 0; attempt < policy.maxAttempts; attempt += 1) {
    try {
      return await send();
    } catch (error) {
      const failure = record(error) ?? {};
      if (!retryableCommandFailure(error, options)
          || attempt + 1 >= policy.maxAttempts) throw error;
      const bodyValue = record(failure.body) ?? {};
      const declaredRetryAfter = Number(bodyValue.retryAfterMs || 0)
        || Number(bodyValue.retryAfter || 0) * 1000;
      const exponentialDelay = policy.baseDelayMs * (2 ** attempt);
      const jitter = 1 + ((Math.random() * 2 - 1) * policy.jitterRatio);
      const delay = declaredRetryAfter > 0
        ? declaredRetryAfter
        : Math.round(exponentialDelay * jitter);
      await waitForRetry(
        Math.max(policy.minDelayMs, Math.min(policy.maxDelayMs, delay)),
        options.signal,
      );
    }
  }
  throw new Error(`Command ${body.commandId} retry loop exited unexpectedly`);
}
'''
    return runtime.strip() + '\n\n' + _render_typescript_client(document)


def _typescript_route(path: str) -> str:
    """Compile OpenAPI placeholders into one encoded template expression."""
    if '`' in path or '${' in path:
        raise ValueError(f'unsupported characters in API path {path!r}')
    rendered = re.sub(
        r'\{([A-Za-z][A-Za-z0-9_]*)\}',
        lambda match: '${segment(' + match.group(1) + ')}',
        path,
    )
    return f'`{rendered}`'


def _typescript_request_route(operation: dict[str, Any]) -> str:
    """Compile one request URL, including contract-owned fixed query values."""
    route = _typescript_route(str(operation['path']))
    fixed_query = operation.get('fixed_query')
    if not fixed_query:
        return route
    return route[:-1] + '?' + urlencode(fixed_query) + '`'


def _render_typescript_client(document: dict[str, Any]) -> str:
    """Generate method signatures and requests directly from OpenAPI paths."""
    operations = _client_operations(document)
    schemas = document['components']['schemas']
    interface_lines = ['export interface ConversationSyncApi {']
    implementation_lines = [
        'const segment = (value: string | number): string => encodeURIComponent(String(value));',
        '',
        'export const conversationSyncApi = Object.freeze<ConversationSyncApi>({',
    ]

    for operation in operations:
        name = str(operation['client_name'])
        if not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*', name):
            raise ValueError(f'x-tofu-client is not a TypeScript identifier: {name!r}')
        path_names = list(operation['path_names'])
        for parameter_name in path_names:
            if not re.fullmatch(
                r'[A-Za-z_$][A-Za-z0-9_$]*', parameter_name,
            ):
                raise ValueError(
                    f'path parameter is not a TypeScript identifier: {parameter_name!r}'
                )
        request_schema = operation['request_schema']
        query_names = list(operation['query_names'])
        fixed_query = dict(operation['fixed_query'])
        params = [f'{parameter_name}: string' for parameter_name in path_names]

        if operation['client_kind'] == 'url':
            if (
                name != 'turnImageUrl'
                or path_names != ['conversationId', 'turnId', 'imageIndex']
                or query_names != ['projectionRevision', 'ownerScope']
                or fixed_query
                or request_schema is not None
            ):
                raise ValueError(
                    'turnImageUrl must declare conversationId, turnId, '
                    'imageIndex, projectionRevision, and ownerScope in order'
                )
            params = [
                'conversationId: string',
                'turnId: string',
                'imageIndex: number',
                'projectionRevision: number',
                'ownerScope: string',
            ]
            interface_lines.append(
                f'  {name}({", ".join(params)}): string;'
            )
            implementation_lines.extend((
                f'  {name}(conversationId, turnId, imageIndex, projectionRevision, ownerScope) {{',
                f'    const base = resolvePath({_typescript_route(operation["path"])});',
                '    const query = [',
                '      `projectionRevision=${encodeURIComponent(String(projectionRevision))}`,',
                '      `ownerScope=${encodeURIComponent(ownerScope)}`,',
                "    ].join('&');",
                '    return `${base}?${query}`;',
                '  },',
            ))
            continue

        response_schema = str(operation['response_schema'])

        if operation['event_stream']:
            if fixed_query:
                raise ValueError('the EventSource operation cannot fix query values')
            expected_query_names = [
                'after', 'streamClientId', 'streamGeneration',
            ]
            if name != 'eventsUrl' or query_names != expected_query_names:
                raise ValueError(
                    'the EventSource operation must declare after, '
                    'streamClientId, and streamGeneration in order'
                )
            params.extend((
                'after?: string',
                'streamClientId?: string',
                'streamGeneration?: number',
            ))
            interface_lines.append(f'  {name}({", ".join(params)}): string;')
            implementation_params = [
                *path_names,
                "after = ''",
                "streamClientId = ''",
                'streamGeneration = 0',
            ]
            implementation_lines.extend((
                f'  {name}({", ".join(implementation_params)}) {{',
                f'    const base = resolvePath({_typescript_route(operation["path"])});',
                '    const query = [',
                "      after ? `after=${encodeURIComponent(after)}` : '',",
                '      streamClientId',
                "        ? `streamClientId=${encodeURIComponent(streamClientId)}` : '',",
                '      streamGeneration > 0',
                "        ? `streamGeneration=${encodeURIComponent(String(streamGeneration))}` : '',",
                "    ].filter(Boolean).join('&');",
                '    return query ? `${base}?${query}` : base;',
                '  },',
            ))
            continue

        dynamic_query_names = [
            query_name for query_name in query_names
            if query_name not in fixed_query
        ]
        if name == 'turnPage':
            expected_query_names = [
                'laneId', 'syncSeq', 'beforeOrdinal', 'limit', 'segmentPayload',
            ]
            if (query_names != expected_query_names
                    or fixed_query != {'segmentPayload': 'refs'}
                    or request_schema is not None):
                raise ValueError(
                    'turnPage must declare laneId, syncSeq, beforeOrdinal, '
                    'limit, and the fixed refs segmentPayload in order'
                )
            params.extend((
                'laneId: string',
                'syncSeq: number',
                'beforeOrdinal?: number',
                'limit?: number',
                'options?: RequestOptions',
            ))
            interface_lines.append(
                f'  {name}({", ".join(params)}): Promise<{response_schema}>;'
            )
            implementation_lines.extend((
                f'  async {name}({", ".join((*path_names, "laneId", "syncSeq", "beforeOrdinal = undefined", "limit = 64", "options = {}"))}) {{',
                f'    const base = resolvePath({_typescript_request_route(operation)});',
                '    const query = [',
                "      `laneId=${encodeURIComponent(laneId)}` ,",
                "      `syncSeq=${encodeURIComponent(String(syncSeq))}` ,",
                '      Number.isInteger(beforeOrdinal)',
                "        ? `beforeOrdinal=${encodeURIComponent(String(beforeOrdinal))}` : '',",
                '      Number.isInteger(limit)',
                "        ? `limit=${encodeURIComponent(String(limit))}` : '',",
                "    ].filter(Boolean).join('&');",
                '    const value = await request<unknown>(',
                '      `${base}&${query}`,',
                "      { ...options, method: 'GET' },",
                '    );',
                '    return '
                f"assertConversationSyncSchema<{response_schema}>('{response_schema}', value);",
                '  },',
            ))
            continue
        if dynamic_query_names:
            raise ValueError(
                f'non-stream client query parameters need an explicit generator policy: {name}'
            )
        if request_schema:
            params.append(f'body: {request_schema}')
        params.append('options?: RequestOptions')
        interface_lines.append(
            f'  {name}({", ".join(params)}): Promise<{response_schema}>;'
        )

        implementation_params = list(path_names)
        if request_schema:
            implementation_params.append('body')
        implementation_params.append('options = {}')
        implementation_lines.append(
            f'  async {name}({", ".join(implementation_params)}) {{'
        )
        if request_schema:
            validator = (
                'snapshotConversationCommand'
                if operation['idempotent_retry']
                else 'assertConversationSyncSchema'
            )
            implementation_lines.append(
                f'    const command = {validator}<{request_schema}>('
                f"'{request_schema}', body);"
            )

        request_options = [
            '...options', f"method: '{operation['method']}'",
        ]
        if request_schema:
            request_options.append('json: command')
        elif operation['request_body_present']:
            request_options.append('json: {}')
        timeout_ms = operation['timeout_ms']
        if timeout_ms is not None:
            if not isinstance(timeout_ms, int) or timeout_ms < 0:
                raise ValueError(f'{name} has an invalid x-tofu-client-timeout-ms')
            request_options.append(f'timeout: {timeout_ms}')
        request_lines = [
            'request<unknown>(',
            f'      {_typescript_request_route(operation)},',
            '      { ' + ', '.join(request_options) + ' },',
            '    )',
        ]
        if operation['idempotent_retry']:
            if not request_schema or 'commandId' not in set(
                schemas[request_schema].get('required') or []
            ):
                raise ValueError(
                    f'{name} requests idempotent retry without required commandId'
                )
            implementation_lines.append(
                '    const value = await idempotentCommand(() => ' + request_lines[0]
            )
            implementation_lines.extend(request_lines[1:-1])
            implementation_lines.append(
                '    ), command, options);'
            )
        else:
            implementation_lines.append(
                '    const value = await ' + request_lines[0]
            )
            implementation_lines.extend(request_lines[1:-1])
            implementation_lines.append('    );')
        implementation_lines.extend((
            '    return '
            f"assertConversationSyncSchema<{response_schema}>('{response_schema}', value);",
            '  },',
        ))

    interface_lines.append('}')
    implementation_lines.append('});')
    return '\n'.join((*interface_lines, '', *implementation_lines))


def render_typescript(document: dict[str, Any]) -> str:
    schemas = document['components']['schemas']
    retry_policy = _command_retry_policy(document)
    lines = [
        '/* AUTO-GENERATED by scripts/gen_conversation_sync_contract.py.',
        ' * Canonical source: contracts/conversation_sync_v3.yaml.',
        ' * DO NOT EDIT BY HAND.',
        ' */',
        "import { request, resolvePath, type RequestOptions } from './transport';",
        '',
        f"export const CONVERSATION_SYNC_CONTRACT_ID = {json.dumps(document['x-tofu-contract'])} as const;",
        f"export const CONVERSATION_SYNC_STREAM_POLICY = {json.dumps(document.get('x-tofu-stream', {}), ensure_ascii=False, indent=2)} as const;",
        f"export const CONVERSATION_SYNC_COMMAND_RETRY_POLICY = {json.dumps(retry_policy, ensure_ascii=False, indent=2)} as const;",
        f"export const CONVERSATION_SYNC_SCHEMAS = {json.dumps(schemas, ensure_ascii=False, sort_keys=True, indent=2)} as const;",
        '',
    ]
    for name, schema in schemas.items():
        lines.append(f'export type {name} = {_typescript_type(schema)};')
        lines.append('')
    lines.extend((_client_source(document), ''))
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    document = _load_contract()
    expected = {
        PYTHON_OUTPUT: render_python(document),
        TYPESCRIPT_OUTPUT: render_typescript(document),
    }
    if args.check:
        stale = []
        for path, content in expected.items():
            try:
                with open(path, encoding='utf-8') as handle:
                    actual = handle.read()
            except FileNotFoundError:
                actual = ''
            if actual != content:
                stale.append(os.path.relpath(path, ROOT))
        if stale:
            print(
                'Generated conversation sync contracts are stale: '
                + ', '.join(stale), file=sys.stderr)
            return 1
        print(f'OK: conversation sync contract is current ({len(document["components"]["schemas"])} schemas)')
        return 0
    for path, content in expected.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(content)
        print(os.path.relpath(path, ROOT))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
