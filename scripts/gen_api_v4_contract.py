#!/usr/bin/env python3
"""Generate API v4 server types and three client-language contracts."""

from __future__ import annotations

import argparse
import json
import os
import pprint
import sys
from typing import Any

import yaml


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
SOURCE = os.path.join(ROOT, 'contracts', 'api_v4.yaml')
OUTPUTS = {
    'server_constants': os.path.join(
        ROOT, 'lib', 'api_v4_constants_generated.py'),
    'server': os.path.join(ROOT, 'lib', 'api_v4_generated.py'),
    'python': os.path.join(
        ROOT, 'clients', 'python', 'tofu_sdk', 'api_v4_generated.py'),
    'typescript': os.path.join(
        ROOT, 'clients', 'typescript', 'src', 'api-v4.generated.ts'),
    'kotlin': os.path.join(
        ROOT, 'android', 'app', 'src', 'main', 'java', 'com', 'tofu',
        'client', 'api', 'ApiV4Generated.kt'),
}


class _UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate keys instead of overwriting them."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
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
        raise ValueError('API v4 contract must be an object')
    if document.get('openapi') != '3.1.0':
        raise ValueError('API v4 contract must use OpenAPI 3.1.0')
    if document.get('x-tofu-contract') != 'tofu.api/v4':
        raise ValueError('unexpected API v4 contract id')
    if document.get('x-tofu-api-major') != 4:
        raise ValueError('API v4 contract must declare major 4')
    if document.get('x-tofu-release-stage') != 'bootstrap':
        raise ValueError(
            'API v4 must remain in bootstrap stage until product routes and '
            'all client preflight checks are generated')

    paths = document.get('paths')
    expected_operations = {
        '/api/v4/meta': 'getApiMeta',
        '/api/v4/openapi.json': 'getApiV4OpenApi',
    }
    if not isinstance(paths, dict) or set(paths) != set(expected_operations):
        raise ValueError('API v4 bootstrap paths must exactly match server routes')
    for path, operation_id in expected_operations.items():
        path_item = paths[path]
        operation = path_item.get('get') if isinstance(path_item, dict) else None
        if not isinstance(operation, dict) or operation.get('operationId') != operation_id:
            raise ValueError(f'{path} must declare GET {operation_id}')

    builds = document.get('x-tofu-minimum-builds')
    if not isinstance(builds, dict) or set(builds) != {'desktop', 'android'}:
        raise ValueError('minimum builds must declare desktop and android')
    if not isinstance(builds['desktop'], str) or not builds['desktop'].strip():
        raise ValueError('desktop minimum build must be a non-empty string')
    if any(
        not component.isdigit()
        for component in builds['desktop'].split('.')
    ):
        raise ValueError('desktop minimum build must be dotted numeric components')
    if isinstance(builds['android'], bool) or not isinstance(builds['android'], int):
        raise ValueError('android minimum build must be an integer version code')

    upgrade = document.get('x-tofu-legacy-upgrade')
    expected_upgrade_keys = {
        'activeByDefault', 'prefixes', 'status', 'upgradeUrl',
    }
    if not isinstance(upgrade, dict) or set(upgrade) != expected_upgrade_keys:
        raise ValueError('legacy upgrade policy has an invalid shape')
    if upgrade['activeByDefault'] is not False:
        raise ValueError('the incomplete v4 cutover must remain disabled by default')
    if upgrade['prefixes'] != ['/api/v1', '/api/v3']:
        raise ValueError('legacy prefixes must be /api/v1 and /api/v3')
    if upgrade['status'] != 426 or upgrade['upgradeUrl'] != '/api/v4/meta':
        raise ValueError('legacy clients must receive 426 and the v4 meta URL')

    schemas = document.get('components', {}).get('schemas')
    required_schemas = {'ApiMeta', 'ResponseMeta', 'ApiMetaResponse', 'Problem'}
    if not isinstance(schemas, dict) or not required_schemas.issubset(schemas):
        raise ValueError('API v4 contract is missing required schemas')
    return document


def _server_constants_python(document: dict[str, Any]) -> str:
    """Render the dependency-free release gate used during ordinary boot."""
    builds = document['x-tofu-minimum-builds']
    upgrade = document['x-tofu-legacy-upgrade']
    return f'''# Generated by scripts/gen_api_v4_contract.py; DO NOT EDIT.
"""Dependency-free API v4 release constants for the server boot boundary."""

API_CONTRACT_ID = {document['x-tofu-contract']!r}
API_MAJOR = {document['x-tofu-api-major']!r}
API_RELEASE_STAGE = {document['x-tofu-release-stage']!r}
MIN_DESKTOP_BUILD = {builds['desktop']!r}
MIN_ANDROID_BUILD = {builds['android']!r}
LEGACY_API_PREFIXES = {tuple(upgrade['prefixes'])!r}
LEGACY_UPGRADE_STATUS = {upgrade['status']!r}
UPGRADE_URL = {upgrade['upgradeUrl']!r}
'''


def _server_python(document: dict[str, Any]) -> str:
    openapi_literal = pprint.pformat(
        document, width=88, sort_dicts=False, compact=False)
    return f'''# Generated by scripts/gen_api_v4_contract.py; DO NOT EDIT.
"""Typed server boundary and canonical OpenAPI document for API v4."""

from __future__ import annotations

from typing import Annotated, Any, Literal, NotRequired, TypedDict, cast

from pydantic import ConfigDict, Field, StringConstraints, TypeAdapter

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
NumericBuildString = Annotated[
    str, StringConstraints(pattern=r'^[0-9]+(?:\\.[0-9]+)*$')]
PositiveInteger = Annotated[int, Field(ge=1)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
ProblemStatus = Annotated[int, Field(ge=400, le=599)]
ProblemCode = Annotated[
    str, StringConstraints(pattern=r'^[a-z][a-z0-9_]*$')]


class ApiMeta(TypedDict):
    __pydantic_config__ = ConfigDict(extra='forbid', strict=True)

    apiMajor: Literal[4]
    schemaVersion: PositiveInteger
    serverBuild: NonEmptyString
    minDesktopBuild: NumericBuildString
    minAndroidBuild: PositiveInteger


class ResponseMeta(TypedDict):
    __pydantic_config__ = ConfigDict(extra='forbid', strict=True)

    requestId: NonEmptyString
    serverTimeMs: NonNegativeInteger


class ApiMetaResponse(TypedDict):
    __pydantic_config__ = ConfigDict(extra='forbid', strict=True)

    data: ApiMeta
    meta: ResponseMeta


class Problem(TypedDict):
    __pydantic_config__ = ConfigDict(extra='forbid', strict=True)

    type: NonEmptyString
    title: NonEmptyString
    status: ProblemStatus
    detail: NonEmptyString
    instance: NonEmptyString
    code: ProblemCode
    requestId: NonEmptyString
    upgradeUrl: NotRequired[NonEmptyString]


API_META_RESPONSE_ADAPTER = TypeAdapter(ApiMetaResponse)
PROBLEM_ADAPTER = TypeAdapter(Problem)


def validate_api_meta_response(value: Any) -> ApiMetaResponse:
    """Validate one response at the HTTP boundary and reject extra fields."""
    return cast(ApiMetaResponse, API_META_RESPONSE_ADAPTER.validate_python(value))


def validate_problem(value: Any) -> Problem:
    """Validate one RFC 7807 body at the HTTP boundary."""
    return cast(Problem, PROBLEM_ADAPTER.validate_python(value))


OPENAPI_DOCUMENT: dict[str, Any] = {openapi_literal}
'''


def _python_client(document: dict[str, Any]) -> str:
    builds = document['x-tofu-minimum-builds']
    return f'''# Generated by scripts/gen_api_v4_contract.py; DO NOT EDIT.
"""Dependency-free API v4 bootstrap DTOs for Python/desktop clients."""

from __future__ import annotations

from typing import Any, Literal, TypedDict, cast


API_V4_MAJOR = 4
MIN_DESKTOP_BUILD = {builds['desktop']!r}
MIN_ANDROID_BUILD = {builds['android']!r}
API_META_PATH = '/api/v4/meta'


class ApiMeta(TypedDict):
    apiMajor: Literal[4]
    schemaVersion: int
    serverBuild: str
    minDesktopBuild: str
    minAndroidBuild: int


class ResponseMeta(TypedDict):
    requestId: str
    serverTimeMs: int


class ApiMetaResponse(TypedDict):
    data: ApiMeta
    meta: ResponseMeta


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{{field}} must be an integer')
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f'{{field}} must be a non-empty string')
    return value


def _numeric_build(build: str) -> tuple[int, ...]:
    components = build.split('.')
    if not components or any(
        not component or not component.isdigit()
        for component in components
    ):
        raise ValueError('build must contain only dotted numeric components')
    return tuple(int(component) for component in components)


def parse_api_meta_response(value: Any) -> ApiMetaResponse:
    """Fail closed when a server does not speak the generated v4 contract."""
    if not isinstance(value, dict) or set(value) != {{'data', 'meta'}}:
        raise ValueError('API v4 meta response must contain exactly data and meta')
    data = value.get('data')
    meta = value.get('meta')
    expected_data = {{
        'apiMajor', 'schemaVersion', 'serverBuild',
        'minDesktopBuild', 'minAndroidBuild',
    }}
    if not isinstance(data, dict) or set(data) != expected_data:
        raise ValueError('API v4 data has an invalid shape')
    if not isinstance(meta, dict) or set(meta) != {{'requestId', 'serverTimeMs'}}:
        raise ValueError('API v4 response meta has an invalid shape')
    if _require_int(data.get('apiMajor'), 'apiMajor') != API_V4_MAJOR:
        raise ValueError('server API major is not compatible with this client')
    if _require_int(data.get('schemaVersion'), 'schemaVersion') < 1:
        raise ValueError('schemaVersion must be positive')
    _require_string(data.get('serverBuild'), 'serverBuild')
    _numeric_build(_require_string(
        data.get('minDesktopBuild'), 'minDesktopBuild'))
    if _require_int(data.get('minAndroidBuild'), 'minAndroidBuild') < 1:
        raise ValueError('minAndroidBuild must be positive')
    _require_string(meta.get('requestId'), 'requestId')
    if _require_int(meta.get('serverTimeMs'), 'serverTimeMs') < 0:
        raise ValueError('serverTimeMs must be non-negative')
    return cast(ApiMetaResponse, value)


def desktop_build_is_compatible(
    current_build: str,
    minimum_build: str = MIN_DESKTOP_BUILD,
) -> bool:
    current = _numeric_build(current_build)
    minimum = _numeric_build(minimum_build)
    width = max(len(current), len(minimum))
    return current + (0,) * (width - len(current)) >= (
        minimum + (0,) * (width - len(minimum)))


def require_desktop_api_compatibility(
    value: Any,
    current_build: str,
) -> ApiMetaResponse:
    """Validate live metadata and reject a desktop build below its minimum."""
    parsed = parse_api_meta_response(value)
    minimum_build = parsed['data']['minDesktopBuild']
    if not desktop_build_is_compatible(current_build, minimum_build):
        raise ValueError(
            f'desktop build {{current_build!r}} is below server minimum '
            f'{{minimum_build!r}}')
    return parsed


def android_build_is_compatible(
    current_version_code: int,
    minimum_version_code: int = MIN_ANDROID_BUILD,
) -> bool:
    return current_version_code >= minimum_version_code
'''


def _typescript_client(document: dict[str, Any]) -> str:
    builds = document['x-tofu-minimum-builds']
    desktop = json.dumps(builds['desktop'])
    return f'''// Generated by scripts/gen_api_v4_contract.py; DO NOT EDIT.
// Canonical API v4 bootstrap DTOs and compatibility probe.

export const API_V4_MAJOR = 4 as const;
export const MIN_DESKTOP_BUILD = {desktop};
export const MIN_ANDROID_BUILD = {builds['android']};
export const API_META_PATH = '/api/v4/meta';

export interface ApiMeta {{
  apiMajor: typeof API_V4_MAJOR;
  schemaVersion: number;
  serverBuild: string;
  minDesktopBuild: string;
  minAndroidBuild: number;
}}

export interface ResponseMeta {{
  requestId: string;
  serverTimeMs: number;
}}

export interface ApiMetaResponse {{
  data: ApiMeta;
  meta: ResponseMeta;
}}

function isRecord(value: unknown): value is Record<string, unknown> {{
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {{
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}}

export function parseApiMetaResponse(value: unknown): ApiMetaResponse {{
  if (!isRecord(value) || !hasExactKeys(value, ['data', 'meta'])) {{
    throw new Error('API v4 meta response must contain exactly data and meta');
  }}
  const data = value.data;
  const meta = value.meta;
  if (!isRecord(data) || !hasExactKeys(data, [
    'apiMajor', 'schemaVersion', 'serverBuild', 'minDesktopBuild', 'minAndroidBuild',
  ])) {{
    throw new Error('API v4 data has an invalid shape');
  }}
  if (!isRecord(meta) || !hasExactKeys(meta, ['requestId', 'serverTimeMs'])) {{
    throw new Error('API v4 response meta has an invalid shape');
  }}
  if (data.apiMajor !== API_V4_MAJOR || !Number.isInteger(data.schemaVersion)
      || (data.schemaVersion as number) < 1
      || typeof data.serverBuild !== 'string' || data.serverBuild.length === 0
      || typeof data.minDesktopBuild !== 'string' || data.minDesktopBuild.length === 0
      || !Number.isInteger(data.minAndroidBuild) || (data.minAndroidBuild as number) < 1
      || typeof meta.requestId !== 'string' || meta.requestId.length === 0
      || !Number.isInteger(meta.serverTimeMs) || (meta.serverTimeMs as number) < 0) {{
    throw new Error('API v4 meta response contains invalid field values');
  }}
  numericBuild(data.minDesktopBuild as string);
  return value as unknown as ApiMetaResponse;
}}

function numericBuild(build: string): number[] {{
  const components = build.split('.');
  if (components.length === 0 || components.some((component) => !/^\\d+$/.test(component))) {{
    throw new Error('build must contain only dotted numeric components');
  }}
  const values = components.map((component) => Number.parseInt(component, 10));
  if (values.some((component) => !Number.isSafeInteger(component))) {{
    throw new Error('build contains an unsafe numeric component');
  }}
  return values;
}}

export function desktopBuildIsCompatible(
  currentBuild: string,
  minimumBuild = MIN_DESKTOP_BUILD,
): boolean {{
  const current = numericBuild(currentBuild);
  const minimum = numericBuild(minimumBuild);
  const length = Math.max(current.length, minimum.length);
  for (let index = 0; index < length; index += 1) {{
    const difference = (current[index] ?? 0) - (minimum[index] ?? 0);
    if (difference !== 0) return difference > 0;
  }}
  return true;
}}

export function requireDesktopApiCompatibility(
  value: unknown,
  currentBuild: string,
): ApiMetaResponse {{
  const parsed = parseApiMetaResponse(value);
  if (!desktopBuildIsCompatible(currentBuild, parsed.data.minDesktopBuild)) {{
    throw new Error(
      `desktop build ${{currentBuild}} is below server minimum ${{parsed.data.minDesktopBuild}}`,
    );
  }}
  return parsed;
}}

export function androidBuildIsCompatible(
  currentVersionCode: number,
  minimumVersionCode = MIN_ANDROID_BUILD,
): boolean {{
  return Number.isInteger(currentVersionCode) && currentVersionCode >= minimumVersionCode;
}}

export async function fetchApiMeta(
  baseUrl: string,
  init: RequestInit = {{}},
): Promise<ApiMetaResponse> {{
  const response = await fetch(`${{baseUrl.replace(/\\/+$/, '')}}${{API_META_PATH}}`, {{
    ...init,
    method: 'GET',
    headers: {{ Accept: 'application/json', ...(init.headers ?? {{}}) }},
  }});
  if (!response.ok) throw new Error(`API v4 meta request failed with ${{response.status}}`);
  return parseApiMetaResponse(await response.json());
}}

export async function fetchCompatibleApiMeta(
  baseUrl: string,
  currentBuild: string,
  init: RequestInit = {{}},
): Promise<ApiMetaResponse> {{
  return requireDesktopApiCompatibility(
    await fetchApiMeta(baseUrl, init),
    currentBuild,
  );
}}
'''


def _kotlin_client(document: dict[str, Any]) -> str:
    builds = document['x-tofu-minimum-builds']
    desktop = json.dumps(builds['desktop'])
    return f'''// Generated by scripts/gen_api_v4_contract.py; DO NOT EDIT.
package com.tofu.client.api

object ApiV4Contract {{
    const val API_MAJOR: Int = 4
    const val MIN_DESKTOP_BUILD: String = {desktop}
    const val MIN_ANDROID_BUILD: Int = {builds['android']}
    const val META_PATH: String = "/api/v4/meta"
}}

data class ApiMeta(
    val apiMajor: Int,
    val schemaVersion: Int,
    val serverBuild: String,
    val minDesktopBuild: String,
    val minAndroidBuild: Int,
)

data class ResponseMeta(
    val requestId: String,
    val serverTimeMs: Long,
)

data class ApiMetaResponse(
    val data: ApiMeta,
    val meta: ResponseMeta,
)

fun desktopBuildIsCompatible(
    currentBuild: String,
    minimumBuild: String = ApiV4Contract.MIN_DESKTOP_BUILD,
): Boolean {{
    val current = numericBuild(currentBuild)
    val minimum = numericBuild(minimumBuild)
    val length = maxOf(current.size, minimum.size)
    for (index in 0 until length) {{
        val difference = current.getOrElse(index) {{ 0 }} - minimum.getOrElse(index) {{ 0 }}
        if (difference != 0) return difference > 0
    }}
    return true
}}

fun androidBuildIsCompatible(
    currentVersionCode: Int,
    minimumVersionCode: Int = ApiV4Contract.MIN_ANDROID_BUILD,
): Boolean = currentVersionCode >= minimumVersionCode

fun requireAndroidApiCompatibility(
    meta: ApiMetaResponse,
    currentVersionCode: Int,
): ApiMetaResponse {{
    require(meta.data.apiMajor == ApiV4Contract.API_MAJOR) {{
        "server API major is not compatible with this client"
    }}
    require(meta.data.schemaVersion >= 1)
    require(meta.data.serverBuild.isNotBlank())
    require(meta.data.minDesktopBuild.isNotBlank())
    require(meta.data.minAndroidBuild >= 1)
    require(meta.meta.requestId.isNotBlank())
    require(meta.meta.serverTimeMs >= 0)
    require(androidBuildIsCompatible(
        currentVersionCode,
        meta.data.minAndroidBuild,
    )) {{
        "Android build $currentVersionCode is below server minimum " +
            "${{meta.data.minAndroidBuild}}"
    }}
    return meta
}}

private fun numericBuild(build: String): List<Int> {{
    val components = build.split('.')
    require(components.isNotEmpty() && components.all {{ component ->
        component.isNotEmpty() && component.all(Char::isDigit)
    }}) {{ "build must contain only dotted numeric components" }}
    return components.map {{ component -> component.toInt() }}
}}
'''


def _render_outputs(document: dict[str, Any]) -> dict[str, str]:
    return {
        'server_constants': _server_constants_python(document),
        'server': _server_python(document),
        'python': _python_client(document),
        'typescript': _typescript_client(document),
        'kotlin': _kotlin_client(document),
    }


def _write_or_check(rendered: dict[str, str], *, check: bool) -> bool:
    stale: list[str] = []
    for name, expected in rendered.items():
        path = OUTPUTS[name]
        try:
            with open(path, encoding='utf-8') as handle:
                actual = handle.read()
        except FileNotFoundError:
            actual = ''
        if actual == expected:
            continue
        if check:
            stale.append(os.path.relpath(path, ROOT))
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(expected)
    if stale:
        print('stale API v4 generated artifacts:', file=sys.stderr)
        for path in stale:
            print(f'  {path}', file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--check', action='store_true',
        help='fail when generated artifacts differ without writing them',
    )
    args = parser.parse_args()
    document = _load_contract()
    return 0 if _write_or_check(_render_outputs(document), check=args.check) else 1


if __name__ == '__main__':
    raise SystemExit(main())
