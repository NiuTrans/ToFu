"""API v4 wire helpers and the dormant legacy-major release gate.

The generated module owns DTOs and constants. This module owns framework
integration: RFC 7807 serialization, application policy validation, and the
single pre-authentication hook used for an atomic v4 cutover.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from quart import Blueprint, Response, current_app, has_request_context, request

from lib.generated_api_v4_constants import (
    API_MAJOR,
    API_RELEASE_STAGE,
    LEGACY_API_PREFIXES,
    LEGACY_UPGRADE_STATUS,
    UPGRADE_URL,
)
from lib.log import req_id


ACTIVE_API_MAJOR_CONFIG = 'ACTIVE_API_MAJOR'
#: Dual-mounted v4 product routes. Default on now that product routes and
#: generated clients ship together; explicit config re-enables a staged rollout.
#: Independent of ACTIVE_API_MAJOR.
API_V4_ENABLED_CONFIG = 'API_V4_ENABLED'
PROBLEM_MEDIA_TYPE = 'application/problem+json'
_SUPPORTED_RELEASE_MAJORS = frozenset({1, API_MAJOR})


class NativeContractBlueprint(Blueprint):
    """A declared domain wire contract owns success and failure envelopes.

    The HTTP major still controls mounting/authentication/retirement. This
    narrow declaration prevents the generic v4 problem adapter from discarding
    native replay/retry/conflict receipts. It adds no routes or auth policy.
    """

    def __init__(self, name: str, import_name: str, *, contract_id: str):
        if not isinstance(contract_id, str) or not 1 <= len(contract_id) <= 128:
            raise ValueError('A native blueprint requires its canonical contract ID')
        super().__init__(name, import_name)
        self.native_contract_id = contract_id


def request_uses_native_response_contract() -> bool:
    """Use matched route ownership, never a path-prefix exemption."""
    blueprint = current_app.blueprints.get(request.blueprint or '')
    return isinstance(blueprint, NativeContractBlueprint)


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

    The canonical contract is in ``cutover`` and the generated ``API_MAJOR``
    is the release default; clients and generated DTOs ship the v4 envelope.
    ``ACTIVE_API_MAJOR=1`` (or the exported ``TOFU_API_MAJOR=1`` used by the
    legacy Python client until it is regenerated) is a per-process,
    assembly-time fallback that keeps legacy v1 serving while v4 remains mounted.
    """
    marker = 'tofu_api_version_policy'
    existing = app.extensions.get(marker)
    if isinstance(existing, Mapping):
        policy = cast(Mapping[str, object], existing)
        return _coerce_active_api_major(policy.get('activeApiMajor'))
    if existing is not None:
        raise RuntimeError('API version policy extension is corrupt')
    default_major = (
        1 if os.environ.get('TOFU_API_MAJOR', '').strip() == '1'
        else API_MAJOR)
    major = _coerce_active_api_major(
        app.config.get(ACTIVE_API_MAJOR_CONFIG, default_major))
    # The generated artifact is a literal; this boundary keeps the module
    # type-checking unchanged however the canonical contract stage advances.
    _configured_api_release_stage()
    v4_enabled = bool(app.config.get(API_V4_ENABLED_CONFIG, True))
    app.config[ACTIVE_API_MAJOR_CONFIG] = major
    app.config[API_V4_ENABLED_CONFIG] = v4_enabled
    app.extensions[marker] = MappingProxyType({
        'activeApiMajor': major,
        'legacyPrefixes': LEGACY_API_PREFIXES,
        'upgradeUrl': UPGRADE_URL,
        'v4Enabled': v4_enabled,
    })
    return major


def v4_routes_enabled(app: Any) -> bool:
    """Whether the dual-mounted v4 product routes are exposed."""
    raw_policy = app.extensions.get('tofu_api_version_policy')
    if isinstance(raw_policy, Mapping):
        return bool(raw_policy.get('v4Enabled'))
    return bool(app.config.get(API_V4_ENABLED_CONFIG, False))


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
    extra_members: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Serialize a validated RFC 7807 response for the v4 boundary."""
    # Strict DTO construction is paid only by an actual v4 response. Ordinary
    # v1 boot/request paths need the generated release constants, not Pydantic's
    # validator graph or the canonical OpenAPI document.
    from lib.generated_api_v4 import validate_problem

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
    if extra_members:
        # RFC 7807 extension members (e.g. lifecycle restart receipts). The
        # generated DTO rejects anything not declared in contracts/api_v4.yaml,
        # so an undeclared member fails closed at validation below.
        payload.update(extra_members)
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


# ── Product-route projection (dual-mount) ─────────────────────────────
#
# v1 route modules declare absolute ``/api/v1/...`` paths on bare blueprints,
# so a plain ``url_prefix`` remount would wrongly yield ``/api/v4/api/v1/...``.
# v4 therefore *copies* a v1 blueprint, rewriting every registered rule's path
# prefix and selecting the v4 envelope at the boundary via an after_request
# hook. Domain handlers stay on ``api_ok``/``api_error`` until cutover — only
# the wire shape differs.

V1_PATH_PREFIX = '/api/v1'
V4_PATH_PREFIX = '/api/v4'

#: Only routes registered by ``@bp.route``/``@bp.get``/... are projected.
#: Quart records these as a single-arg lambda whose closure is
#: ``(provide_automatic_options, options, endpoint, rule, view_func)`` — the
#: v1 prefix always sits in the ``rule`` cell (index 3) for this shape.
_ROUTE_LAMBDA_CELLS = 5


def _rewrite_path(rule: str) -> str:
    """Swap a leading ``/api/v1`` prefix for ``/api/v4`` (anchored)."""
    if rule == V1_PATH_PREFIX:
        return V4_PATH_PREFIX
    if rule.startswith(V1_PATH_PREFIX + '/'):
        return V4_PATH_PREFIX + rule[len(V1_PATH_PREFIX):]
    return rule


def _is_route_lambda(func: Any) -> bool:
    return (
        getattr(func, '__name__', '') == '<lambda>'
        and func.__code__.co_varnames[:1] == ('s',)
        and len(func.__closure__ or ()) == _ROUTE_LAMBDA_CELLS
        and isinstance(func.__closure__[3].cell_contents, str)
    )


def copy_blueprint_to_v4(source: Any, *, name: str) -> Any:
    """Project a v1 blueprint onto ``/api/v4`` with rewritten rule paths.

    Quart records route registrations as opaque one-arg closures on
    ``Blueprint.deferred_functions`` that capture the original (v1-prefixed)
    rule, so prefix remounting is impossible. We clone by decoding each route
    closure back into its ``add_url_rule`` arguments, swapping the path prefix,
    and re-registering with a ``.v4``-suffixed endpoint to stay unique. Every
    other attribute (methods, defaults, the SAME view function, strict-slashes)
    is preserved verbatim, so v1 and v4 execute identical domain logic and only
    the wire path/envelope differ.

    Non-route deferred registrations (error handlers, nested blueprints, app
    hooks) are NOT projected; they are copied only when they are blueprint-
    scoped hooks expressed as ordinary ``record`` callbacks. The v4 envelope is
    then attached by :func:`attach_v4_envelope`.
    """
    from quart import Blueprint

    target = Blueprint(name, source.import_name)
    for index, deferred in enumerate(source.deferred_functions):
        if not _is_route_lambda(deferred):
            # Non-route deferred setup: replay verbatim onto the copy so
            # blueprint-scoped hooks (before/after, error handlers) survive.
            target.record(deferred)
            continue
        cells = [c.cell_contents for c in deferred.__closure__]
        provide_automatic_options, options, endpoint, rule, view_func = cells
        new_rule = _rewrite_path(rule)
        # Endpoint must be unique per rule: several rules may share one view
        # function (e.g. v1's multi-path ``_abort``), so suffix with the rule
        # index, not just the blueprint, to avoid a mapping overwrite.
        base = endpoint if isinstance(endpoint, str) and endpoint else view_func.__name__
        new_endpoint = f'{base}_{index}'
        target.add_url_rule(
            new_rule,
            new_endpoint,
            view_func,
            provide_automatic_options=provide_automatic_options,
            **options,
        )
    return target


def success_envelope(data: Any, *, request_id: str, server_time_ms: int) -> dict:
    """Project one domain JSON value without inventing a domain wrapper."""
    body = dict(data) if isinstance(data, dict) else data
    if isinstance(body, dict) and body.get('ok') is True:
        body.pop('ok')
    return {
        'data': body,
        'meta': {'requestId': request_id, 'serverTimeMs': server_time_ms},
    }


def _is_product_json_success(response: Any) -> bool:
    """The declared HTTP boundary, not domain keys, selects the envelope.

    Any body-bearing 2xx JSON success is a candidate — v1 helpers
    emit ``{ok: true}`` on 201 (``api_created``) too, and a narrower 200-only
    gate would leak the v1 shape on those."""
    content_type = (response.content_type or '').split(';', 1)[0].strip()
    return (200 <= response.status_code < 300 and response.status_code not in (204, 205)
            and content_type == 'application/json')


def raw_success_response(handler: Any) -> Any:
    """Declare a file/text endpoint whose successful bytes own their wire shape.

    This per-handler declaration leaves authentication and failure projection
    intact. Runtime and OpenAPI read it; MIME or filename heuristics cannot
    distinguish a JSON download from a product JSON result.
    """
    handler.tofu_raw_success_response = True
    return handler


async def translate_v4_envelope(response: Any) -> Any:
    """after_request: convert a v1 payload into the v4 ``{data, meta}`` shape.

    Errors are left alone — the framework error boundary already renders
    RFC 7807 for v4 paths via ``_v4_problem``. Streaming/SSE and non-JSON
    bodies pass through unmodified.
    """
    import time as _time

    if has_request_context():
        handler = current_app.view_functions.get(request.endpoint)
        if getattr(handler, 'tofu_raw_success_response', False):
            return response
    if not _is_product_json_success(response):
        return response
    try:
        payload = json.loads(await response.get_data())
    except (ValueError, TypeError):
        return response
    enveloped = success_envelope(
        payload,
        request_id=req_id() or 'unassigned',
        server_time_ms=int(_time.time() * 1000),
    )
    response.set_data(
        json.dumps(enveloped, ensure_ascii=False, separators=(',', ':')))
    return response


def attach_v4_envelope(blueprint: Any) -> Any:
    """Register the v4 success-envelope translation on a projected blueprint."""
    if isinstance(blueprint, NativeContractBlueprint):
        raise ValueError('A native response contract cannot use the generic v4 envelope')
    if getattr(blueprint, 'tofu_v4_success_envelope', False):
        return blueprint
    blueprint.tofu_v4_success_envelope = True
    blueprint.after_request(translate_v4_envelope)
    return blueprint


__all__ = [
    'ACTIVE_API_MAJOR_CONFIG',
    'API_V4_ENABLED_CONFIG',
    'PROBLEM_MEDIA_TYPE',
    'NativeContractBlueprint',
    'request_uses_native_response_contract',
    'active_api_major',
    'attach_v4_envelope',
    'configure_api_version_policy',
    'copy_blueprint_to_v4',
    'is_api_v4_path',
    'legacy_api_requires_upgrade',
    'legacy_api_upgrade_before_request',
    'legacy_upgrade_response',
    'problem_response',
    'raw_success_response',
    'success_envelope',
    'translate_v4_envelope',
    'v4_routes_enabled',
]
