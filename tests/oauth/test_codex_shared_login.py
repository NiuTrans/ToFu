"""Shared Codex login: account/device ownership, no rotation, logout and expiry."""
import base64
import json
import os
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from lib.oauth import codex_local_login as local
from lib.oauth import codex_shared_login as shared
from lib.oauth import token_store

pytestmark = pytest.mark.unit


def _jwt(claims):
    return 'header.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.signature'


def _auth(account='account-a', expire=None):
    claims = {'exp': expire if expire is not None else time.time() + 3600,
              'https://api.openai.com/auth': {'chatgpt_account_id': account,
                                            'chatgpt_plan_type': 'plus'}}
    return {'auth_mode': 'chatgpt', 'tokens': {
        'access_token': _jwt(claims), 'refresh_token': 'NEVER-COPY-THIS',
        'id_token': _jwt({'email': 'example@example.test'}), 'account_id': account}}


@pytest.fixture
def login(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.oauth.codex_command_login.command_context',
                        lambda **kwargs: {'shared_login_issue': 'codex_command_missing'})
    root = tmp_path / 'codex'
    root.mkdir()
    path = root / 'auth.json'
    path.write_text(json.dumps(_auth()))
    path.chmod(0o600)
    monkeypatch.setenv('CODEX_HOME', str(root))
    monkeypatch.setenv('TOFU_CODEX_SHARED_LOGIN', '1')
    monkeypatch.setattr('runtime_guards.load_deployment_configuration',
                        lambda: SimpleNamespace(mode='personal'))
    monkeypatch.setattr(token_store, '_config_path', lambda p: str(tmp_path / 'tofu' / p))
    monkeypatch.setattr(shared, '_agents', lambda owner: [])
    shared._remote.clear()
    yield path
    shared._remote.clear()


def test_local_reference_tracks_replacement_without_copying_refresh(login):
    shared.reconcile_shared_login(owner_user_id=1)
    stored = token_store.load_stored_token('codex')
    assert set(stored) == {'codex_shared_reference', '_saved_at'}
    token = token_store.load_token('codex')
    assert token['account_id'] == 'account-a'
    assert not {'refresh_token', 'id_token'} & token.keys()
    assert token_store.save_token('codex', token) is False
    replacement = _auth(expire=time.time() + 7200)
    next_file = login.with_suffix('.next')
    next_file.write_text(json.dumps(replacement)); next_file.chmod(0o600)
    os.replace(next_file, login)
    assert token_store.load_token('codex')['access_token'] == replacement['tokens']['access_token']
    assert 'NEVER-COPY-THIS' not in open(token_store.token_path('codex')).read()


def test_shared_refresh_never_calls_token_endpoint(login):
    shared.reconcile_shared_login(owner_user_id=1)
    from lib.oauth.codex import codex_refresh_token, codex_get_valid_token
    with mock.patch('lib.oauth.codex._codex_refresh_upstream') as upstream:
        assert codex_refresh_token('foreign-refresh')['access_token']
        assert codex_get_valid_token(user_id='1')
        assert codex_get_valid_token(user_id='2') is None
        upstream.assert_not_called()


@pytest.mark.parametrize('change,issue', [
    ('account', 'account_changed'), ('delete', 'codex_not_signed_in'),
    ('expire', 'codex_access_expired'), ('invalid', 'codex_credentials_unreadable')])
def test_shared_source_changes_fail_closed(login, change, issue):
    shared.reconcile_shared_login(owner_user_id=1)
    if change == 'account': login.write_text(json.dumps(_auth('account-b')))
    elif change == 'delete': login.unlink()
    elif change == 'expire': login.write_text(json.dumps(_auth(expire=time.time()-1)))
    else: login.write_text('{invalid NEVER-COPY-THIS')
    token = token_store.load_token('codex')
    assert not token.get('access_token')
    assert token['shared_login_issue'] == issue
    assert token['account_id'] == 'account-a'
    shared.reconcile_shared_login(owner_user_id=1)
    assert token_store.load_stored_token('codex')['codex_shared_reference']['account_id'] == 'account-a'


def test_tofu_logout_does_not_log_out_codex_or_rediscover(login):
    before = login.read_bytes()
    shared.reconcile_shared_login(owner_user_id=1)
    assert token_store.delete_token('codex') is True
    shared.reconcile_shared_login(owner_user_id=1)
    assert token_store.load_token('codex') is None
    assert login.read_bytes() == before


def test_existing_tofu_login_wins(login):
    token_store.save_token('codex', {'access_token': 'independent', 'refresh_token': 'independent-refresh'})
    shared.reconcile_shared_login(owner_user_id=1)
    assert token_store.load_token('codex')['access_token'] == 'independent'


@pytest.mark.parametrize('mode', ['keyring', 'auto', 'ephemeral'])
def test_never_reads_leftover_file_for_other_credential_modes(login, mode):
    config = login.parent / 'config.toml'
    config.write_text(f'cli_auth_credentials_store = "{mode}"')
    config.chmod(0o600)
    assert local.read_local_login() == {'shared_login_issue': 'credential_store_unsupported'}


@pytest.mark.parametrize('value', [None, True, '123', float('inf'), -1])
def test_unknown_expiry_is_not_a_permanent_bearer(login, value):
    doc = _auth()
    doc['tokens']['access_token'] = _jwt({'exp': value})
    login.write_text(json.dumps(doc))
    assert not local.read_local_login().get('access_token')


def test_distributed_or_wrong_owner_never_borrows_host_identity(login, monkeypatch):
    assert shared.discover_reference(owner_user_id=2) is None
    monkeypatch.setattr('runtime_guards.load_deployment_configuration',
                        lambda: SimpleNamespace(mode='distributed'))
    with mock.patch.object(local, 'read_local_login') as read:
        assert shared.discover_reference(owner_user_id=1) is None
        read.assert_not_called()


def test_relative_home_does_not_read_project_credentials(login, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', 'project')
    assert local.read_local_login()['shared_login_issue'] == 'invalid_codex_home'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file permissions')
def test_unsafe_file_or_symlink_rejected(login):
    login.chmod(0o666)
    assert not local.read_local_login().get('access_token')
    login.chmod(0o600)
    target = login.with_suffix('.target'); login.rename(target); login.symlink_to(target)
    assert not local.read_local_login().get('access_token')


def _device():
    return {'agent_id': 'device-a', 'user_id': '1', 'online': True,
            'capabilities': {'codex_login': 1}}


def test_remote_borrowing_is_addressed_and_access_only(login, monkeypatch):
    snapshot = local.read_local_login()
    login.unlink()
    monkeypatch.setattr(shared, '_agents', lambda owner: [_device()])
    with mock.patch('lib.desktop.bridge.send_desktop_command', return_value=(
            {**snapshot, 'refresh_token': 'NEVER-COPY-THIS'}, None)) as send:
        shared.reconcile_shared_login(owner_user_id=1)
        token = token_store.load_token('codex')
    assert token['credential_source'] == 'desktop_codex'
    assert 'refresh_token' not in token
    assert send.call_args.kwargs['user_id'] == '1'
    assert send.call_args.kwargs['target_agent_id'] == 'device-a'
    assert token_store.load_stored_token('codex')['codex_shared_reference']['agent_id'] == 'device-a'
    monkeypatch.setattr(shared, '_agents', lambda owner: [])
    assert not token_store.load_token('codex').get('access_token')


def test_multiple_remote_devices_are_not_guessed(login, monkeypatch):
    login.unlink()
    monkeypatch.setattr(shared, '_agents', lambda owner: [_device(), {**_device(), 'agent_id': 'device-b'}])
    with mock.patch('lib.desktop.bridge.send_desktop_command') as send:
        assert shared.discover_reference(owner_user_id=1) is None
        send.assert_not_called()


def test_remote_account_switch_does_not_rebind(login, monkeypatch):
    snapshot = local.read_local_login(); login.unlink()
    monkeypatch.setattr(shared, '_agents', lambda owner: [_device()])
    with mock.patch('lib.desktop.bridge.send_desktop_command', return_value=(snapshot, None)):
        shared.reconcile_shared_login(owner_user_id=1)
    with mock.patch('lib.desktop.bridge.send_desktop_command', return_value=({**snapshot, 'account_id':'account-b'}, None)):
        shared.reconcile_shared_login(owner_user_id=1)
    assert not token_store.load_token('codex').get('access_token')
    assert token_store.load_stored_token('codex')['codex_shared_reference']['account_id'] == 'account-a'


def test_agent_requires_dedicated_permission_and_refuses_paths(login):
    from lib.desktop_agent._dispatch import dispatch_command
    from lib.desktop_agent._permissions import build_permissions
    assert 'error' in dispatch_command('codex_login_read', {}, build_permissions(allow_egress=True))
    granted = build_permissions(allow_codex_login=True)
    assert 'error' in dispatch_command('codex_login_read', {'path': '/etc/passwd'}, granted)
    result = dispatch_command('codex_login_read', {}, granted)
    assert result['access_token']
    assert 'refresh_token' not in result


def test_public_status_reports_shared_source_without_secret(login):
    shared.reconcile_shared_login(owner_user_id=1)
    from lib.oauth.manager._flow import get_oauth_status
    with mock.patch('lib.oauth.outbound.managed_oauth_provider_status', return_value={}):
        status = get_oauth_status('codex', owner_user_id=1)
    assert status['credential_source'] == 'local_codex'
    assert status['authenticated'] is True
    assert not {'access_token', 'refresh_token', 'codex_shared_reference'} & status.keys()


def test_explicit_login_reconnects_after_tofu_logout_without_authorization(login):
    from lib.oauth.manager._flow import start_oauth_flow
    from lib.oauth.manager._device import start_device_flow
    shared.reconcile_shared_login(owner_user_id=1)
    token_store.delete_token('codex')
    with mock.patch('lib.oauth.manager._exchange._finalize_login_success',
                    return_value={'status': 'success'}) as finalize, \
         mock.patch('lib.oauth.codex.codex_build_auth_url') as build, \
         mock.patch('lib.oauth.codex.codex_device_request_user_code') as device:
        assert start_oauth_flow('codex', owner_user_id=1)['status'] == 'success'
        assert start_device_flow('codex', owner_user_id=1)['status'] == 'success'
        assert finalize.call_count == 2
        build.assert_not_called()
        device.assert_not_called()
    assert token_store.load_token('codex')['access_token']


def test_passive_remote_reads_do_not_enqueue_commands(login, monkeypatch):
    snapshot = local.read_local_login(); login.unlink()
    monkeypatch.setattr(shared, '_agents', lambda owner: [_device()])
    with mock.patch('lib.desktop.bridge.send_desktop_command', return_value=(snapshot, None)):
        shared.reconcile_shared_login(owner_user_id=1)
    shared._remote.clear()
    with mock.patch('lib.desktop.bridge.send_desktop_command') as send:
        assert not token_store.load_token('codex').get('access_token')
        send.assert_not_called()


def test_logout_waits_for_discovery_and_wins(login, monkeypatch):
    import threading
    snapshot = local.read_local_login(); login.unlink()
    monkeypatch.setattr(shared, '_agents', lambda owner: [_device()])
    started, release = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return snapshot, None
    with mock.patch('lib.desktop.bridge.send_desktop_command', side_effect=delayed):
        discover = threading.Thread(target=shared.reconcile_shared_login,
                                    kwargs={'owner_user_id': 1})
        discover.start()
        assert started.wait(3)
        logout = threading.Thread(target=token_store.delete_token, args=('codex',))
        logout.start()
        release.set()
        discover.join(3); logout.join(3)
        assert not discover.is_alive() and not logout.is_alive()
    shared.reconcile_shared_login(owner_user_id=1)
    assert token_store.load_token('codex') is None


def test_registry_filters_owner_and_explicit_capability(login, monkeypatch):
    monkeypatch.undo()
    with mock.patch('lib.desktop.bridge.list_agents', return_value=[
            _device(), {**_device(), 'user_id':'2'},
            {**_device(), 'online':False}, {**_device(), 'capabilities':{'egress':True}}]) as registry:
        assert shared._agents(1) == [_device()]
        registry.assert_called_once_with(user_id='1')


def test_wrong_owner_status_does_not_expose_borrowed_identity(login):
    shared.reconcile_shared_login(owner_user_id=1)
    from lib.oauth.manager._flow import get_oauth_status
    with mock.patch('lib.oauth.outbound.managed_oauth_provider_status', return_value={}):
        status = get_oauth_status('codex', owner_user_id=2)
    assert status['authenticated'] is False
    assert status['email'] == ''


def test_explicit_reconnect_can_borrow_same_account_after_native_refresh_failure(login):
    token_store.save_token('codex', {'account_id': 'account-a',
        'access_token':'expired', 'expire':time.time()-1, 'refresh_invalidated_at':time.time()})
    shared.reconcile_shared_login(owner_user_id=1)
    assert not token_store.load_stored_token('codex').get('codex_shared_reference')
    shared.reconcile_shared_login(owner_user_id=1, explicit=True)
    assert token_store.load_token('codex')['credential_source'] == 'local_codex'


def test_explicit_reconnect_never_substitutes_a_different_account(login):
    token_store.save_token('codex', {'account_id': 'account-b',
        'access_token':'expired', 'expire':time.time()-1})
    shared.reconcile_shared_login(owner_user_id=1, explicit=True)
    assert token_store.load_stored_token('codex')['account_id'] == 'account-b'
    assert not token_store.load_stored_token('codex').get('codex_shared_reference')


@pytest.mark.parametrize('owner', [None, True, '1', -1])
def test_corrupt_reference_cannot_infer_host_owner(login, owner):
    assert shared.resolve_reference({'kind':'local', 'account_id':'account-a',
                                     'owner_user_id':owner}) == {'shared_login_issue':'invalid_reference'}


@pytest.mark.parametrize('change,issue', [('missing', 'codex_not_signed_in'),
    ('expired', 'codex_access_expired'), ('symlink', 'codex_credentials_unreadable')])
def test_local_diagnosis_reports_only_redacted_issue(login, change, issue):
    if change == 'missing':
        login.unlink()
    elif change == 'expired':
        login.write_text(json.dumps(_auth(expire=time.time() - 1)))
    else:
        target = login.with_suffix('.real')
        login.rename(target)
        login.symlink_to(target)
    assert shared.local_login_issue(owner_user_id=1) == issue
    assert shared.local_login_issue(owner_user_id=2) == 'owner_unavailable'


def test_shared_request_recovers_once_and_retains_incident(login, monkeypatch, caplog):
    import logging
    from lib.oauth.codex import codex_get_valid_token
    shared.reconcile_shared_login(owner_user_id=1)
    original = local.read_local_login
    calls = []
    def read(**kwargs):
        calls.append(kwargs['refresh'])
        return original(**kwargs) if kwargs['refresh'] else {'shared_login_issue': 'codex_command_timeout'}
    monkeypatch.setattr(local, 'read_local_login', read)
    with caplog.at_level(logging.WARNING):
        assert codex_get_valid_token(user_id='1')
    assert calls == [False, True]
    events = [r for r in caplog.records if getattr(r, 'tofu_event_name', '') == 'codex_shared_login_recovery']
    assert len(events) == 1
    assert events[0].tofu_event_fields['initial_issue'] == 'codex_command_timeout'
    assert events[0].tofu_event_fields['outcome'] == 'recovered'
    assert isinstance(events[0].tofu_event_fields['elapsed_ms'], int)
    assert 'NEVER-COPY' not in caplog.text
    assert not token_store.load_stored_token('codex').get('access_token')


@pytest.mark.parametrize('issue,reauth,retryable', [
    ('codex_command_timeout', False, True),
    ('codex_command_unavailable', False, True),
    ('codex_credentials_unreadable', False, False),
    ('account_changed', False, False),
    ('codex_access_expired', True, False),
    ('codex_not_signed_in', True, False),
])
def test_shared_source_failure_keeps_reason_and_neutral_preparation(login, monkeypatch, caplog, issue, reauth, retryable):
    from lib.error_envelope import from_exception
    from lib.llm.llm_errors import LocalRequestPreparationError
    from lib.oauth.outbound import resolve_oauth_request
    shared.reconcile_shared_login(owner_user_id=1)
    monkeypatch.setattr(local, 'read_local_login', lambda **kwargs: {'shared_login_issue': issue})
    with pytest.raises(LocalRequestPreparationError) as raised:
        try:
            resolve_oauth_request('codex', {}, None, user_id='1')
        except Exception as cause:
            raise LocalRequestPreparationError(f'local request preparation failed: {cause}') from cause
    env = from_exception(raised.value, model='gpt-6.1-sol')
    assert raised.value.provider_reached is False
    assert (env['kind'] == 'oauth_reauth') is reauth
    assert env['retryable'] is retryable
    assert env['severity'] == 'warning'
    if not reauth:
        assert env['authIssue'] == issue
        assert env['credentialSource'] == 'local'
        assert env['providerReached'] is False
        assert 'titleKey' not in env and 'hintKey' not in env
    assert any(getattr(r, 'tofu_event_fields', {}).get('auth_issue') == issue for r in caplog.records)


def test_recovery_cannot_revive_logged_out_binding(login, monkeypatch):
    from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
    from lib.oauth.codex import codex_get_valid_token
    shared.reconcile_shared_login(owner_user_id=1)
    def read(**kwargs):
        token_store.delete_token('codex')
        return {'shared_login_issue': 'codex_command_timeout'}
    monkeypatch.setattr(local, 'read_local_login', read)
    with pytest.raises(SubscriptionCredentialUnavailableError) as raised:
        codex_get_valid_token(user_id='1')
    assert raised.value.reason == 'codex_source_changed'
    assert token_store.load_stored_token('codex') is None


def test_shared_failure_is_durable_and_correlated(login, monkeypatch, tmp_path):
    from lib.incident_journal import IncidentJournalHandler
    from lib.log import LogContextFilter, bind_log_context
    from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
    from lib.oauth.codex import codex_get_valid_token
    shared.reconcile_shared_login(owner_user_id=1)
    monkeypatch.setattr(local, 'read_local_login', lambda **kwargs: {'shared_login_issue': 'codex_command_timeout'})
    path = tmp_path / 'incident.jsonl'
    handler = IncidentJournalHandler(str(path))
    handler.addFilter(LogContextFilter())
    shared.logger.addHandler(handler)
    try:
        with bind_log_context(conversation_id='conv-test', task_id='task-test', trace_id='trace-test'):
            with pytest.raises(SubscriptionCredentialUnavailableError):
                codex_get_valid_token(user_id='1')
    finally:
        shared.logger.removeHandler(handler)
        handler.close()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    recovery = next(row for row in rows if row.get('event') == 'codex_shared_login_recovery')
    assert recovery['conversation_id'] == 'conv-test'
    assert recovery['task_id'] == 'task-test'
    assert recovery['trace_id'] == 'trace-test'
    assert recovery['fields']['initial_issue'] == 'codex_command_timeout'
    assert recovery['fields']['auth_issue'] == 'codex_command_timeout'
    assert recovery['fields']['outcome'] == 'failed'
    assert 'NEVER-COPY-THIS' not in path.read_text()
    assert 'example@example.test' not in path.read_text()
