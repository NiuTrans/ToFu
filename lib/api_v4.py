"""API v4 wire helpers and the dormant legacy-major release gate.

The generated module owns DTOs and constants. This module owns framework
integration: RFC 7807 serialization, application policy validation, and the
single pre-authentication hook used for an atomic v4 cutover.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from quart import Response, current_app, request

from lib.api_v4_constants_generated import (
    API_MAJOR,
    API_RELEASE_STAGE,
    LEGACY_API_PREFIXES,
    LEGACY_UPGRADE_STATUS,
    UPGRADE_URL,
)
from lib.log import req_id


ACTIVE_API_MAJOR_CONFIG = 'ACTIVE_API_MAJOR'
PROBLEM_MEDIA_TYPE = 'application/problem+json'
_SUPPORTED_RELEASE_MAJORS = frozenset({1, API_MAJOR})


def _configured_api_release_stage() -> str:
    """Read the generated stage through a stable runtime-typed boundary."""
    return str(API_RELEASE_STAGE)


def _coerce_active_api_major(value: Any) -> int:
    if type(value) is not int:
        raise ValueError(f'{ACTIVE_API_MAJOR_CONFIG} must be 1 or {API_MAJOR}')
    major = value
    if major not in _SUPPORTED_RELEASE_MAJORS:
        raise ValueError(f'{ACTIVE_API_MAJOR_CONFIG} must be 1 or {API_MAJOR}')
    return major


def configure_api_version_policy(app: Any) -> int:
    """Validate and freeze the app-local API release-major policy.

    Major 1 is the transitional default while product routes and clients move
    to v4. A release assembly may explicitly set ``ACTIVE_API_MAJOR=4`` only
    after that migration; no environment variable is read here.
    """
    marker = 'tofu_api_version_policy'
    existing = app.extensions.get(marker)
    if isinstance(existing, Mapping):
        policy = cast(Mapping[str, object], existing)
        return _coerce_active_api_major(policy.get('activeApiMajor'))
    if existing is not None:
        raise RuntimeError('API version policy extension is corrupt')
    major = _coerce_active_api_major(
        app.config.get(ACTIVE_API_MAJOR_CONFIG, 1))
    # The generated bootstrap artifact is intentionally a literal today, but
    # this policy module must also type-check unchanged when the release
    # contract advances to ``cutover``.
    release_stage = _configured_api_release_stage()
    if major == API_MAJOR and release_stage != 'cutover':
        raise RuntimeError(
            f'API v{API_MAJOR} cutover is locked while the canonical contract '
            f'release stage is {release_stage!r}')
    app.config[ACTIVE_API_MAJOR_CONFIG] = major
    app.extensions[marker] = MappingProxyType({
        'activeApiMajor': major,
        'legacyPrefixes': LEGACY_API_PREFIXES,
        'upgradeUrl': UPGRADE_URL,
    })
    return major


def active_api_major(app: Any) -> int:
    raw_policy = app.extensions.get('tofu_api_version_policy')
    if isinstance(raw_policy, Mapping):
        policy = cast(Mapping[str, object], raw_policy)
        return _coerce_active_api_major(policy.get('activeApiMajor'))
    return configure_api_version_policy(app)


def legacy_api_requires_upgrade(path: str, *, active_major: int) -> bool:
    """Return whether a path belongs to a retired native API major."""
    if active_major < API_MAJOR:
        return False
    return any(
        path == prefix or path.startswith(prefix + '/')
        for prefix in LEGACY_API_PREFIXES
    )


def problem_response(
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    instance: str,
    problem_type: str | None = None,
    upgrade_url: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Serialize a validated RFC 7807 response for the v4 boundary."""
    # Strict DTO construction is paid only by an actual v4 response. Ordinary
    # v1 boot/request paths need the generated release constants, not Pydantic's
    # validator graph or the canonical OpenAPI document.
    from lib.api_v4_generated import validate_problem

    request_id = req_id() or 'unassigned'
    payload: dict[str, Any] = {
        'type': problem_type or f'urn:tofu:problem:{code}',
        'title': title,
        'status': status,
        'detail': detail,
        'instance': instance,
        'code': code,
        'requestId': request_id,
    }
    if upgrade_url:
        payload['upgradeUrl'] = upgrade_url
    validated = validate_problem(payload)
    response_headers = {'Cache-Control': 'no-store'}
    if headers:
        response_headers.update(headers)
    return Response(
        json.dumps(validated, ensure_ascii=False, separators=(',', ':')),
        status=status,
        headers=response_headers,
        content_type=PROBLEM_MEDIA_TYPE,
    )


def legacy_upgrade_response(path: str) -> Response:
    return problem_response(
        status=LEGACY_UPGRADE_STATUS,
        code='api_version_upgrade_required',
        title='API client upgrade required',
        detail=(
            'This API major has been retired. Read the v4 metadata endpoint '
            'and install a compatible client before retrying.'
        ),
        instance=path,
        upgrade_url=UPGRADE_URL,
        headers={'Link': f'<{UPGRADE_URL}>; rel="latest-version"'},
    )


async def legacy_api_upgrade_before_request() -> Response | None:
    """Return 426 before auth/storage work once the v4 release latch is set."""
    app = cast(Any, current_app)._get_current_object()
    major = active_api_major(app)
    if not legacy_api_requires_upgrade(request.path, active_major=major):
        return None
    return legacy_upgrade_response(request.path)


def is_api_v4_path(path: str) -> bool:
    return path == '/api/v4' or path.startswith('/api/v4/')


__all__ = [
    'ACTIVE_API_MAJOR_CONFIG',
    'PROBLEM_MEDIA_TYPE',
    'active_api_major',
    'configure_api_version_policy',
    'is_api_v4_path',
    'legacy_api_requires_upgrade',
    'legacy_api_upgrade_before_request',
    'legacy_upgrade_response',
    'problem_response',
]
