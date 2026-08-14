"""Request uploads are finite without shortening long-lived SSE responses."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def policy_module():
    """Load the native owner without importing the whole application."""
    import lib.http_body_policy as module
    return module


def _server_source():
    return (Path(__file__).parents[1] / 'server.py').read_text()


def _policy_source():
    return (Path(__file__).parents[1] / 'lib/http_body_policy.py').read_text()


def test_body_timeout_is_finite_but_response_timeout_stays_unbounded():
    source = _policy_source()
    assert "app.config['RESPONSE_TIMEOUT'] = None" in source
    assert "app.config['BODY_TIMEOUT'] = policy.body_timeout" in source
    assert "app.config['BODY_TIMEOUT'] = None" not in source
    server_source = _server_source()
    assembly_source = (
        Path(__file__).parents[1] / 'lib/app_assembly.py').read_text()
    assert 'body_policy=_HTTP_BODY_POLICY' in server_source
    assert 'register_http_body_policy(app, policy)' in assembly_source


def test_personal_install_defaults_allow_slow_large_uploads():
    source = _policy_source()
    assert "'TOFU_HTTP_BODY_TIMEOUT', 300" in source
    assert "'TOFU_HTTP_UPLOAD_BODY_TIMEOUT', 1800" in source
    for route in (
        '/api/v1/videos/upload',
        '/api/images/upload',
        '/api/pdf/parse',
        '/api/paper/upload',
        '/api/v1/audio/transcribe',
        '/api/v1/project/upload',
    ):
        assert repr(route) in source


def test_timeout_parser_fails_closed_and_clamps(policy_module, monkeypatch):
    monkeypatch.setenv('TOFU_TEST_BODY_TIMEOUT', 'bad')
    assert policy_module.bounded_http_timeout_env(
        'TOFU_TEST_BODY_TIMEOUT', 300) == 300
    monkeypatch.setenv('TOFU_TEST_BODY_TIMEOUT', '0')
    assert policy_module.bounded_http_timeout_env(
        'TOFU_TEST_BODY_TIMEOUT', 300) == 30
    monkeypatch.setenv('TOFU_TEST_BODY_TIMEOUT', '999999')
    assert policy_module.bounded_http_timeout_env(
        'TOFU_TEST_BODY_TIMEOUT', 300) == 7200


def test_upload_timeout_can_never_be_shorter_than_normal_timeout(policy_module,
                                                                 monkeypatch):
    monkeypatch.setenv('TOFU_TEST_UPLOAD_TIMEOUT', '60')
    assert policy_module.bounded_http_timeout_env(
        'TOFU_TEST_UPLOAD_TIMEOUT', 1800, minimum=300) == 300
