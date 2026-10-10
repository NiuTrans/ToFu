"""Provider authority, automatic discovery and secret-free recovery contracts."""
import json
import os
import sys
from pathlib import Path as Path

import pytest
import tofu_mtsso_auth as auth
from tofu_mtsso_auth import configuration

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    auth.clear()
    for name in tuple(os.environ):
        if name.startswith(('MTSSO_', 'AGENT_SSO_')):
            monkeypatch.delenv(name)
    monkeypatch.setenv('HOME', str(tmp_path))
    original = configuration.os.path.isfile
    monkeypatch.setattr(configuration.os.path, 'isfile',
                        lambda p: False if p == '/root/.openclaw/sso/agent_info' else original(p))
    monkeypatch.setattr(auth, '_run', lambda *a, **k: pytest.fail('local status must not spawn'))
    yield
    auth.clear()


def test_installed_cli_alone_keeps_existing_provider(monkeypatch):
    monkeypatch.setattr(auth, '_executable', lambda: '/installed/official')
    assert auth.enabled() is False
    status = auth.provider_status()
    assert status['auth_source'] == 'mcp_existing'
    assert status['official_installed'] is True
    assert status['identity_verified'] is False
    assert status['model_action'] == 'continue_original_tool'


@pytest.mark.parametrize('name', [
    'AGENT_SSO_CLIENT_ID', 'AGENT_SSO_CLIENT_SECRET', 'MTSSO_AGENT_CONFIG_PATH',
    'MTSSO_OFFICIAL_EXECUTABLE', 'MTSSO_GATEWAY_INTERCEPT_FEATURE',
])
def test_explicit_official_declaration_never_downgrades_even_without_cli(monkeypatch, name):
    monkeypatch.setenv(name, 'private-value')
    monkeypatch.setattr(auth, '_executable', lambda: None)
    assert auth.enabled() is True
    result = auth.provider_status()
    assert result['official_installed'] is False
    assert 'private-value' not in json.dumps(result)


def test_auto_pins_official_until_explicit_session_clear(monkeypatch):
    monkeypatch.setenv('AGENT_SSO_CLIENT_ID', 'registered-agent')
    assert auth.enabled() is True
    monkeypatch.delenv('AGENT_SSO_CLIENT_ID')
    assert auth.enabled() is True
    auth.clear()
    assert auth.enabled() is False


def test_auto_discovers_new_registration_without_model_intervention(tmp_path):
    assert auth.enabled() is False
    path = tmp_path / '.openclaw/sso/agent_info'
    path.parent.mkdir(parents=True)
    path.write_text('{"AGENT_SSO_CLIENT_ID":"private-agent"}')
    assert auth.enabled() is True


def test_workdir_override_matches_official_discovery(monkeypatch, tmp_path):
    workdir = tmp_path / 'custom'
    workdir.mkdir()
    (workdir / 'agent_info').write_text('{}')
    monkeypatch.setenv('MTSSO_WORKDIR', str(workdir))
    assert auth.enabled() is True


@pytest.mark.parametrize('content,expected', [
    ('# DEFAULT_MTSSO_AGENT_CONFIG_PATH=/ignored\n', False),
    ('export DEFAULT_MTSSO_AGENT_CONFIG_PATH = "/private/registered.json"\n', True),
    ("DEFAULT_MTSSO_AGENT_CONFIG_PATH=''\n", False),
    ('DEFAULT_MTSSO_AGENT_CONFIG_PATH=\nDEFAULT_MTSSO_AGENT_CONFIG_PATH=/ignored\n', False),
    ('OTHER_SECRET=private-secret\n', False),
])
def test_extended_agent_fallback_matches_official_parser(tmp_path, content, expected):
    path = tmp_path / '.mtsso/agents/.env'
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert auth.enabled() is expected
    assert 'private' not in json.dumps(auth.provider_status())


def test_bad_extended_fallback_fails_closed_without_network(tmp_path):
    path = tmp_path / '.mtsso/agents/.env'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'x' * (configuration._CONFIG_LIMIT + 1))
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.enabled()
    result = caught.value.envelope()
    assert result['error_code'] == 'SSO_AGENT_CONFIGURATION_INVALID'
    assert result['recovery_action'] == 'repair_agent_configuration'
    assert result['model_action'] == 'stop_and_report'


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX regular-file admission')
def test_fallback_fifo_cannot_block_status(tmp_path):
    path = tmp_path / '.mtsso/agents/.env'
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    assert auth.provider_status()['error_code'] == 'SSO_AGENT_CONFIGURATION_INVALID'


@pytest.mark.parametrize('mode,expected', [('official', True), ('legacy', False)])
def test_explicit_provider_selection_is_authoritative(monkeypatch, mode, expected):
    monkeypatch.setenv('MTSSO_AUTH_MODE', mode)
    monkeypatch.setenv('AGENT_SSO_CLIENT_ID', 'private-agent')
    assert auth.enabled() is expected


def test_invalid_mode_has_terminal_public_instruction(monkeypatch):
    monkeypatch.setenv('MTSSO_AUTH_MODE', 'invented-mode')
    result = auth.provider_status()
    assert result['error_code'] == 'SSO_CONFIGURATION'
    assert result['model_action'] == 'stop_and_report'


@pytest.mark.parametrize('field', ['client_id', 'client_secret'])
def test_official_plain_configuration_errors_get_constant_safe_diagnosis(field):
    stderr = f'错误: 缺少 {field}（/private/user/path）\nBearer private-ticket'
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(1, '', stderr)
    result = caught.value.envelope()
    assert result['error_code'] == 'SSO_AGENT_CONFIGURATION_MISSING'
    assert result['login_required'] is False
    assert result['retryable'] is False
    assert result['recovery_action'] == 'repair_agent_configuration'
    assert field in result['error']
    assert 'private' not in json.dumps(result)


def test_permission_error_beats_incidental_plain_diagnostic():
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(42, '{"code":"act_access_denied","message":"申请权限"}', '错误: 缺少 client_id')
    assert caught.value.code == 'act_access_denied'
    assert caught.value.envelope()['recovery_action'] == 'follow_official_instruction'


def test_denial_cannot_activate_existing_provider(monkeypatch):
    monkeypatch.setenv('AGENT_SSO_CLIENT_ID', 'private-agent')
    monkeypatch.setattr(auth, '_executable', lambda: '/fake/official')
    calls = []
    def denied(argv):
        calls.append(argv)
        return 42, '{"code":"act_access_denied","message":"申请权限"}', ''
    monkeypatch.setattr(auth, '_run', denied)
    for _ in range(2):
        assert auth.enabled() is True
        with pytest.raises(auth.OfficialAuthError):
            auth.user_token('owner', ['audience'])
    assert len(calls) == 1
    assert '--auto_update' in calls[0]
    assert calls[0][-1] == 'false'
    assert 'private-agent' not in calls[0]


def test_real_cli_missing_identity_returns_specific_error(monkeypatch, tmp_path):
    script = tmp_path / 'official'
    script.write_text(f'#!{sys.executable}\nimport sys\nsys.stderr.write("错误: 缺少 client_id（private-config）")\nsys.exit(1)\n')
    script.chmod(0o755)
    monkeypatch.setenv('MTSSO_OFFICIAL_EXECUTABLE', str(script))
    monkeypatch.setattr(auth, '_run', _REAL_RUN)
    assert auth.enabled() is True
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('owner', ['audience'])
    assert caught.value.code == 'SSO_AGENT_CONFIGURATION_MISSING'
    assert 'private-config' not in json.dumps(caught.value.envelope())


_REAL_RUN = auth._run


def test_ticket_recovery_does_not_change_provider_authority(monkeypatch):
    monkeypatch.setenv('AGENT_SSO_CLIENT_ID', 'private-agent')
    assert auth.enabled()
    monkeypatch.delenv('AGENT_SSO_CLIENT_ID')
    auth.invalidate()
    assert auth.enabled()
    auth.clear()
    assert not auth.enabled()


def test_structured_stderr_error_beats_stdout_ticket_and_redacts_both_sources():
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(42, '{"access_token":"first-private-ticket"}', json.dumps({
            'code': 'ric_feedback_required',
            'message': '请确认 first-private-ticket second-private-ticket third-private-secret',
            'subject_token': 'second-private-ticket',
            'details': [{'client_secret': 'third-private-secret'}],
        }))
    result = caught.value.envelope()
    assert result['error_code'] == 'ric_feedback_required'
    assert 'private' not in json.dumps(result)
    assert result['user_action_required']


def test_plain_missing_identity_is_preserved_with_stdout_json():
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(1, '{"access_token":"private-ticket"}', '错误: 缺少 client_id')
    assert caught.value.code == 'SSO_AGENT_CONFIGURATION_MISSING'


@pytest.mark.parametrize('status,support,selected,code', [
    (1, 'none', False, None),
    (1, 'none', True, 'SSO_ROUTE_UNAVAILABLE'),
    (0, 'extend_agent', True, None),
    (0, 'gateway_intercept_feature', True, 'SSO_GATEWAY_REQUIRED'),
    (0, 'invented-type', True, 'SSO_CAPABILITY_UNKNOWN'),
])
def test_explicit_capability_diagnosis_is_typed_and_secret_free(monkeypatch, tmp_path, status, support, selected, code):
    executable = tmp_path / 'mtsso-moa-local-exchange'
    probe = tmp_path / 'mtsso-moa-feature-probe'
    probe.write_text('#!/bin/sh\n')
    monkeypatch.setattr(auth, '_executable', lambda: str(executable))
    if selected:
        monkeypatch.setenv('MTSSO_AUTH_MODE', 'official')
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs['timeout'] <= 5
        return status, json.dumps({'support_type': support, 'reason': 'private-ticket'}), 'private-stderr'
    monkeypatch.setattr(auth, '_run', run)
    result = auth.provider_status(probe=True)
    assert len(calls) == 1
    assert 'private' not in json.dumps(result)
    assert result.get('error_code') == code
    assert result['ok'] is (code is None)


def test_force_refresh_cannot_repeat_terminal_authorization(monkeypatch):
    monkeypatch.setattr(auth, '_executable', lambda: '/fake/official')
    calls = []
    def denied(argv):
        calls.append(argv)
        return 42, '{"code":"MOA_AUTH_REQUEST_PENDING","message":"请确认"}', ''
    monkeypatch.setattr(auth, '_run', denied)
    for force in (False, True, True):
        with pytest.raises(auth.OfficialAuthError):
            auth.user_token('owner', ['audience'], force_refresh=force)
    assert len(calls) == 1


def test_error_without_code_cannot_accept_ticket():
    with pytest.raises(auth.OfficialAuthError):
        auth._parse(0, '{"access_token":"private-ticket","error":{"message":"failed"}}')


@pytest.mark.parametrize('expiry,ttl', [(True, 0), (10 ** 400, 7200), (float('inf'), 0), (-10, 0)])
def test_untrusted_expiry_is_bounded_without_overflow(expiry, ttl):
    assert auth._parse(0, json.dumps({'access_token': 'private-ticket', 'expires_in': expiry}))[1] == ttl


def test_public_authorization_message_redacts_embedded_assignments_and_basic():
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(42, json.dumps({
            'code': 'ric_feedback_required',
            'message': '请确认 https://example.test/?access_token=private-token&x=1 client_secret:private-secret Basic private-basic',
        }))
    assert 'private' not in json.dumps(caught.value.envelope())
    assert '请确认' in caught.value.message


def test_redaction_budget_cannot_leak_an_unvisited_credential():
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(42, json.dumps({
            'code': 'ric_feedback_required',
            'message': '请确认 private-value',
            'details': [{'client_secret': 'private-value'}] + [{} for _ in range(130)],
        }))
    assert caught.value.user_action
    assert 'private-value' not in json.dumps(caught.value.envelope())


def test_clear_during_selection_fences_provider_pin(monkeypatch):
    def declaration():
        auth.clear()
        return True
    monkeypatch.setattr(auth, '_official_configuration_declared', declaration)
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.enabled()
    assert caught.value.code == 'SSO_SESSION_CHANGED'
    monkeypatch.setattr(auth, '_official_configuration_declared', lambda: False)
    assert not auth.enabled()
