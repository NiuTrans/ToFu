"""Executable contract for the staged API v4 bootstrap and release gate."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from clients.python.tofu_sdk.api_v4_generated import (
    android_build_is_compatible,
    desktop_build_is_compatible,
    parse_api_meta_response,
    require_desktop_api_compatibility,
)
from clients.python.tofu_sdk import Tofu
from lib.api_v4 import (
    ACTIVE_API_MAJOR_CONFIG,
    active_api_major,
    configure_api_version_policy,
    legacy_api_upgrade_before_request,
)
from lib.api_v4_generated import (
    OPENAPI_DOCUMENT,
    validate_api_meta_response,
)
from lib.api_response import api_unauthorized
from lib.app_assembly import create_application
from lib.app_factory import create_base_app
from lib.http_body_policy import HttpBodyPolicy
from lib.http_error_handlers import register_http_error_handlers
from lib.http_request_lifecycle import register_request_lifecycle
from lib.storage_sidecar.schema import SCHEMA_VERSION
from lib.version import __version__
from routes import ALL_BLUEPRINTS
from routes.api_v4 import api_v4_bp
from routes.api_v1.auth import _is_public


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _bootstrap_app(*, active_major: int = 1):
    app = create_base_app(
        f'api-v4-test-{active_major}',
        {'TESTING': True, 'ACTIVE_API_MAJOR': active_major},
    )
    # Exercise the post-migration behavior without weakening the production
    # bootstrap-stage lock asserted separately below.
    release_stage = 'cutover' if active_major == 4 else 'bootstrap'
    with patch('lib.api_v4.API_RELEASE_STAGE', release_stage):
        configure_api_version_policy(app)
    register_request_lifecycle(
        app,
        before_storage_write_fence=(legacy_api_upgrade_before_request,),
    )
    app.register_blueprint(api_v4_bp)
    register_http_error_handlers(app)
    return app


def test_generated_artifacts_are_current():
    result = subprocess.run(
        [sys.executable, 'scripts/gen_api_v4_contract.py', '--check'],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_contract_is_openapi_31_and_matches_registered_v4_routes():
    assert OPENAPI_DOCUMENT['openapi'] == '3.1.0'
    assert OPENAPI_DOCUMENT['x-tofu-contract'] == 'tofu.api/v4'
    assert OPENAPI_DOCUMENT['x-tofu-release-stage'] == 'bootstrap'
    app = _bootstrap_app()
    registered = set()
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith('/api/v4/'):
            continue
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            registered.add((rule.rule, method.lower()))
    declared = {
        (path, method)
        for path, path_item in OPENAPI_DOCUMENT['paths'].items()
        for method in path_item
        if method in {'get', 'post', 'put', 'patch', 'delete'}
    }
    assert registered == declared == {
        ('/api/v4/meta', 'get'),
        ('/api/v4/openapi.json', 'get'),
    }
    assert api_v4_bp in ALL_BLUEPRINTS
    assert _is_public('/api/v4/meta') is True
    assert _is_public('/api/v4/openapi.json') is True


def test_meta_wire_shape_uses_live_server_and_storage_versions():
    app = _bootstrap_app()

    async def exercise():
        response = await app.test_client().get(
            '/api/v4/meta', headers={'X-Request-ID': 'v4-meta-test'})
        assert response.status_code == 200
        assert response.content_type == 'application/json'
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['X-Request-ID'] == 'v4-meta-test'
        payload = await response.get_json()
        assert set(payload) == {'data', 'meta'}
        assert payload['data'] == {
            'apiMajor': 4,
            'schemaVersion': SCHEMA_VERSION,
            'serverBuild': __version__,
            'minDesktopBuild': '0.16.0',
            'minAndroidBuild': 17,
        }
        assert payload['meta']['requestId'] == 'v4-meta-test'
        assert isinstance(payload['meta']['serverTimeMs'], int)

    asyncio.run(exercise())


def test_openapi_endpoint_returns_raw_canonical_document():
    app = _bootstrap_app()

    async def exercise():
        response = await app.test_client().get('/api/v4/openapi.json')
        assert response.status_code == 200
        assert response.content_type == 'application/json'
        assert await response.get_json() == OPENAPI_DOCUMENT

    asyncio.run(exercise())


def test_pydantic_boundary_rejects_wrong_major_and_extra_fields():
    valid = {
        'data': {
            'apiMajor': 4,
            'schemaVersion': 28,
            'serverBuild': '0.16.0',
            'minDesktopBuild': '0.16.0',
            'minAndroidBuild': 17,
        },
        'meta': {'requestId': 'request-1', 'serverTimeMs': 1},
    }
    assert validate_api_meta_response(valid) == valid
    wrong_major = {**valid, 'data': {**valid['data'], 'apiMajor': 3}}
    with pytest.raises(ValidationError):
        validate_api_meta_response(wrong_major)
    coerced_schema = {**valid, 'data': {**valid['data'], 'schemaVersion': '28'}}
    with pytest.raises(ValidationError):
        validate_api_meta_response(coerced_schema)
    with pytest.raises(ValidationError):
        validate_api_meta_response({**valid, 'unexpected': True})
    invalid_request_id = {**valid, 'meta': {**valid['meta'], 'requestId': ''}}
    with pytest.raises(ValidationError):
        validate_api_meta_response(invalid_request_id)
    invalid_minimum = {
        **valid,
        'data': {**valid['data'], 'minDesktopBuild': 'release-current'},
    }
    with pytest.raises(ValidationError):
        validate_api_meta_response(invalid_minimum)


@pytest.mark.parametrize('path', ['/api/v1', '/api/v1/tasks', '/api/v3/x'])
def test_v4_cutover_returns_problem_426_before_legacy_handlers(path):
    app = _bootstrap_app(active_major=4)

    @app.get(path)
    async def legacy_handler():
        return {'executed': True}

    async def exercise():
        response = await app.test_client().get(
            path, headers={'X-Request-ID': 'upgrade-test'})
        assert response.status_code == 426
        assert response.content_type == 'application/problem+json'
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['Link'] == (
            '</api/v4/meta>; rel="latest-version"')
        body = await response.get_json()
        assert body['status'] == 426
        assert body['code'] == 'api_version_upgrade_required'
        assert body['requestId'] == 'upgrade-test'
        assert body['upgradeUrl'] == '/api/v4/meta'
        assert body['instance'] == path

    asyncio.run(exercise())


def test_transitional_major_executes_v1_and_prefix_matches_are_anchored():
    app = _bootstrap_app(active_major=1)

    @app.get('/api/v1/probe')
    async def current_handler():
        return {'executed': True}

    async def exercise_current():
        response = await app.test_client().get('/api/v1/probe')
        assert response.status_code == 200
        assert await response.get_json() == {'executed': True}

    asyncio.run(exercise_current())

    cutover = _bootstrap_app(active_major=4)

    @cutover.get('/api/v10/probe')
    async def unrelated_handler():
        return {'executed': True}

    async def exercise_anchored():
        response = await cutover.test_client().get('/api/v10/probe')
        assert response.status_code == 200

    asyncio.run(exercise_anchored())


def test_invalid_active_major_fails_application_assembly_closed():
    app = create_base_app(
        'api-v4-invalid-major',
        {'TESTING': True, 'ACTIVE_API_MAJOR': 3},
    )
    with pytest.raises(ValueError, match='ACTIVE_API_MAJOR'):
        configure_api_version_policy(app)


@pytest.mark.parametrize('invalid_major', [True, False, '4', 4.0, None])
def test_release_latch_rejects_coercible_non_integer_values(invalid_major):
    app = create_base_app(
        'api-v4-invalid-major-type',
        {'TESTING': True, 'ACTIVE_API_MAJOR': invalid_major},
    )
    with pytest.raises(ValueError, match='ACTIVE_API_MAJOR'):
        configure_api_version_policy(app)


def test_bootstrap_contract_refuses_config_only_v4_cutover():
    app = create_base_app(
        'api-v4-premature-cutover',
        {'TESTING': True, 'ACTIVE_API_MAJOR': 4},
    )
    with pytest.raises(RuntimeError, match="release stage is 'bootstrap'"):
        configure_api_version_policy(app)
    assert 'tofu_api_version_policy' not in app.extensions


def test_release_policy_is_frozen_after_application_assembly():
    app = create_base_app('api-v4-frozen-policy', {'TESTING': True})
    assert configure_api_version_policy(app) == 1
    app.config[ACTIVE_API_MAJOR_CONFIG] = 4
    assert active_api_major(app) == 1
    with pytest.raises(TypeError):
        app.extensions['tofu_api_version_policy']['activeApiMajor'] = 4


def test_release_gate_precedes_body_parsing_and_size_policy(tmp_path):
    static_dir = tmp_path / 'static'
    static_dir.mkdir()
    with patch('lib.api_v4.API_RELEASE_STAGE', 'cutover'):
        app = create_application(
            'api-v4-release-order',
            static_dir=str(static_dir),
            logger=logging.getLogger('test.api-v4-release-order'),
            secret_key='test-only',
            config={'TESTING': True, 'ACTIVE_API_MAJOR': 4},
            body_policy=HttpBodyPolicy(
                body_timeout=30,
                upload_body_timeout=30,
                route_caps=(),
                default_cap=1,
                long_upload_prefixes=(),
            ),
        )

    hooks = app.before_request_funcs[None]
    hook_names = [hook.__name__ for hook in hooks]
    assert hook_names.index('legacy_api_upgrade_before_request') < (
        hook_names.index('_enforce_http_body_policy'))
    assert hook_names.index('legacy_api_upgrade_before_request') < (
        hook_names.index('method_override'))

    async def exercise():
        response = await app.test_client().post(
            '/api/v1/retired-handler',
            data=b'"double encoded legacy body"',
            headers={
                'Content-Type': 'application/json',
                'Content-Length': '28',
                'X-Request-ID': 'release-order-test',
            },
        )
        assert response.status_code == 426
        assert response.content_type == 'application/problem+json'
        assert (await response.get_json())['requestId'] == 'release-order-test'

    asyncio.run(exercise())


def test_unknown_v4_route_uses_problem_json():
    app = _bootstrap_app()

    async def exercise():
        response = await app.test_client().get('/api/v4/not-declared')
        assert response.status_code == 404
        assert response.content_type == 'application/problem+json'
        body = await response.get_json()
        assert body['code'] == 'not_found'
        assert body['instance'] == '/api/v4/not-declared'

    asyncio.run(exercise())


def test_existing_auth_helpers_emit_problem_json_on_v4_paths():
    app = _bootstrap_app()

    @app.get('/api/v4/secure-test')
    async def secure_test():
        return api_unauthorized('A compatible bearer credential is required.')

    async def exercise():
        response = await app.test_client().get('/api/v4/secure-test')
        assert response.status_code == 401
        assert response.content_type == 'application/problem+json'
        body = await response.get_json()
        assert body['code'] == 'unauthorized'
        assert body['detail'] == 'A compatible bearer credential is required.'
        assert body['instance'] == '/api/v4/secure-test'

    asyncio.run(exercise())


def test_generated_python_client_validates_and_compares_builds():
    value = {
        'data': {
            'apiMajor': 4,
            'schemaVersion': 28,
            'serverBuild': '0.16.0',
            'minDesktopBuild': '0.16.0',
            'minAndroidBuild': 17,
        },
        'meta': {'requestId': 'client-test', 'serverTimeMs': 1},
    }
    assert parse_api_meta_response(value) == value
    with pytest.raises(ValueError):
        parse_api_meta_response({**value, 'unexpected': True})
    assert desktop_build_is_compatible('0.16.1') is True
    assert desktop_build_is_compatible('0.16') is True
    assert desktop_build_is_compatible('0.15.9') is False
    assert require_desktop_api_compatibility(value, '0.16.0') == value
    too_new = {
        **value,
        'data': {**value['data'], 'minDesktopBuild': '99.0.0'},
    }
    with pytest.raises(ValueError, match='below server minimum'):
        require_desktop_api_compatibility(too_new, '0.17.0')
    with pytest.raises(ValueError, match='dotted numeric'):
        desktop_build_is_compatible('current', '0.16.0')
    invalid_minimum = {
        **value,
        'data': {**value['data'], 'minDesktopBuild': 'latest'},
    }
    with pytest.raises(ValueError, match='dotted numeric'):
        parse_api_meta_response(invalid_minimum)
    assert android_build_is_compatible(17) is True
    assert android_build_is_compatible(16) is False


def test_python_sdk_meta_probe_uses_the_live_server_minimum(monkeypatch):
    client = Tofu(base_url='https://tofu.invalid')
    incompatible = {
        'data': {
            'apiMajor': 4,
            'schemaVersion': 28,
            'serverBuild': '99.0.0',
            'minDesktopBuild': '99.0.0',
            'minAndroidBuild': 99,
        },
        'meta': {'requestId': 'sdk-live-minimum', 'serverTimeMs': 1},
    }
    monkeypatch.setattr(client, '_json', lambda *_args, **_kwargs: incompatible)
    with pytest.raises(ValueError, match='below server minimum'):
        client.api_meta()
