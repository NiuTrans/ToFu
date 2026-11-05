"""One official SDK session, explicit owner/identity, self-healing business calls."""
import json
import os
import threading
import time
import pytest
import tofu_mtsso_auth as auth


def success(request, token='private-value', **extra):
    return json.dumps({'access_token': token, 'expires_in': 3600,
                      'subject': request['mis'], 'identity_verified': True,
                      'session_fingerprint': request['fingerprint'], **extra})


def required():
    return json.dumps({'code': 'SSO_SESSION_REQUIRED', 'message': '需要用户在大象中批准一次登录。'})


def waiting_on_flight(count):
    deadline = time.monotonic() + 5
    while len(auth._CHANGED._waiters) < count:
        assert time.monotonic() < deadline, 'caller never joined the in-flight approval'
        time.sleep(0.01)


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    auth.clear()
    for key in tuple(os.environ):
        if key.startswith(('MTSSO_', 'AGENT_SSO_')):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MTSSO_AUTH_MODE', 'shared')
    monkeypatch.setenv('MTSSO_MIS', 'alice')
    sdk = tmp_path / 'auth.js'
    sdk.write_text('export const ssoAuth = {};')
    from tofu_mtsso_auth import configuration
    original = configuration.os.path.isfile
    monkeypatch.setattr(configuration.os.path, 'isfile',
                        lambda p: False if p == '/root/.openclaw/sso/agent_info' else original(p))
    monkeypatch.setenv('MTSSO_SESSION_SDK', str(sdk))
    monkeypatch.setattr(auth, '_node_command', lambda executable: ['/node', executable])
    yield
    auth.clear()


def test_same_user_session_serves_all_audiences_without_login(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        request = json.loads(kwargs['input_data'])
        calls.append(request)
        assert request['action'] == 'token'
        assert request['mis'] == 'alice'
        assert all('secret' not in a for a in argv)
        return 0, success(request, request['audiences'][0]), ''
    monkeypatch.setattr(auth, '_run', run)
    for audience in ('mlp', 'llm', 'citadel', 'friday'):
        assert auth.user_token('owner-1', [audience]) == audience
        assert auth.user_token('owner-1', [audience]) == audience
    assert len(calls) == 4
    assert auth.cache_status('owner-1', ['citadel'])['auth_source'] == 'mtsso_shared_session'


def test_identity_mismatch_is_terminal_and_never_native_fallback(monkeypatch):
    monkeypatch.setattr(auth, '_run', lambda *a, **k: (1, json.dumps({
        'error': 'SSO_IDENTITY_MISMATCH', 'message': 'wrong user', 'access_token': 'private-value'}), ''))
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('owner-1', ['citadel'])
    envelope = caught.value.envelope()
    assert envelope['retryable'] is False
    assert envelope['auth_state'] == 'blocked'
    assert envelope['model_action'] == 'stop_and_report'
    # Adapter errors never ask an MCP to start its own (legacy) login path.
    assert 'login_required' not in envelope
    assert 'private-value' not in json.dumps(envelope)


def test_business_call_escalates_missing_session_to_one_approval(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        request = json.loads(kwargs['input_data'])
        calls.append(request['action'])
        if request['action'] == 'token':
            return 42, required(), ''
        return 0, success(request, 'after-login', login_performed=True), ''
    monkeypatch.setattr(auth, '_run', run)
    assert auth.user_token('owner-1', ['llm']) == 'after-login'
    assert calls == ['token', 'login']
    assert auth.verified_subject('owner-1') == 'alice'


def test_status_probe_never_requests_approval(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(json.loads(kwargs['input_data'])['action'])
        return 42, required(), ''
    monkeypatch.setattr(auth, '_run', run)
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('owner-1', ['llm'], interactive=False)
    assert caught.value.code == 'SSO_SESSION_REQUIRED'
    assert caught.value.envelope()['auth_state'] == 'login_required'
    status = auth.session_status('owner-1', ['llm'])
    assert status['ok'] is True and status['logged_in'] is False
    assert status['auth_state'] == 'login_required'
    assert status['approval']['login_target'] == 'alice'
    assert calls == ['token']


def test_status_failure_cannot_veto_business_self_heal(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        request = json.loads(kwargs['input_data'])
        calls.append(request['action'])
        if request['action'] == 'token':
            return 42, required(), ''
        return 0, success(request, login_performed=True), ''
    monkeypatch.setattr(auth, '_run', run)
    assert auth.session_status('owner-1', ['llm'])['auth_state'] == 'login_required'
    assert auth.user_token('owner-1', ['llm']) == 'private-value'
    status = auth.session_status('owner-1', ['llm'])
    assert status['auth_state'] == 'ready' and status['identity_verified'] is True
    assert calls == ['token', 'token', 'login']


def test_explicit_login_reuses_session_or_requests_one_approval(monkeypatch):
    calls = []
    usable = [True]
    def run(argv, **kwargs):
        request = json.loads(kwargs['input_data'])
        calls.append(request['action'])
        if request['action'] == 'token' and not usable[0]:
            return 42, required(), ''
        return 0, success(request, login_performed=request['action'] == 'login'), ''
    monkeypatch.setattr(auth, '_run', run)
    result = auth.login('owner-1', ['llm'], mis='alice')
    assert result['approved'] is True and result['login_performed'] is False
    assert result['subject'] == 'alice' and result['auth_state'] == 'ready'
    assert auth.user_token('owner-1', ['llm']) == 'private-value'
    assert calls == ['token']
    auth.invalidate()
    usable[0] = False
    result = auth.login('owner-1', ['llm'], mis='alice')
    assert result['login_performed'] is True
    assert result['approval']['channel'] == '大象'
    assert calls == ['token', 'token', 'login']


def test_status_reports_verified_identity_without_tickets(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        request = json.loads(kwargs['input_data'])
        calls.append(request['action'])
        return 0, success(request), ''
    monkeypatch.setattr(auth, '_run', run)
    status = auth.session_status('owner-1', ['llm'], mis='alice')
    assert status['logged_in'] is True and status['identity_verified'] is True
    assert status['subject'] == 'alice' and status['auth_state'] == 'ready'
    assert 'private-value' not in json.dumps(status)
    # A ready answer primed exactly the ticket the business call uses.
    assert auth.user_token('owner-1', ['llm']) == 'private-value'
    assert calls == ['token']
    mismatch = auth.session_status('owner-1', ['llm'], mis='bob')
    assert mismatch['logged_in'] is False
    assert mismatch['error_code'] == 'SSO_IDENTITY_MISMATCH'


def test_concurrent_business_calls_share_one_approval(monkeypatch):
    calls = []
    entered, release = threading.Event(), threading.Event()
    def run(argv, **kwargs):
        request = json.loads(kwargs['input_data'])
        calls.append(request['action'])
        if request['action'] == 'token':
            return 42, required(), ''
        entered.set()
        assert release.wait(5)
        return 0, success(request, 'approved', login_performed=True), ''
    monkeypatch.setattr(auth, '_run', run)
    results = []
    callers = [threading.Thread(target=lambda: results.append(auth.user_token('owner-1', ['llm'])))
               for _ in range(2)]
    callers[0].start()
    assert entered.wait(5)
    callers[1].start()
    waiting_on_flight(1)
    release.set()
    for caller in callers:
        caller.join(5)
    assert results == ['approved', 'approved']
    assert calls == ['token', 'login']


def test_unanswered_approval_is_shared_by_waiters_but_not_by_later_retries(monkeypatch):
    calls = []
    entered, release = threading.Event(), threading.Event()
    def run(argv, **kwargs):
        request = json.loads(kwargs['input_data'])
        calls.append(request['action'])
        if request['action'] == 'token':
            return 42, required(), ''
        entered.set()
        assert release.wait(5)
        return 42, json.dumps({'code': 'SSO_LOGIN_NOT_APPROVED', 'message': '大象登录授权未在有效期内获批'}), ''
    monkeypatch.setattr(auth, '_run', run)
    envelopes = []
    def call():
        try:
            auth.user_token('owner-1', ['llm'])
        except auth.OfficialAuthError as error:
            envelopes.append(error.envelope())
    callers = [threading.Thread(target=call) for _ in range(2)]
    callers[0].start()
    assert entered.wait(5)
    callers[1].start()
    waiting_on_flight(1)
    release.set()
    for caller in callers:
        caller.join(5)
    assert calls == ['token', 'login']
    assert len(envelopes) == 2
    for envelope in envelopes:
        assert envelope['error_code'] == 'SSO_LOGIN_NOT_APPROVED'
        assert envelope['auth_state'] == 'login_required' and envelope['login_attempted'] is True
        assert envelope['model_action'] == 'ask_user_to_approve_then_retry'
        assert envelope['approval']['login_target'] == 'alice'
    call()
    assert calls == ['token', 'login', 'token', 'login']


def test_rejected_approval_is_not_requested_again(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        request = json.loads(kwargs['input_data'])
        calls.append(request['action'])
        if request['action'] == 'token':
            return 42, required(), ''
        return 42, json.dumps({'code': 'MOA_USER_REJECTED', 'message': '您已拒绝本次授权'}), ''
    monkeypatch.setattr(auth, '_run', run)
    for _ in range(2):
        with pytest.raises(auth.OfficialAuthError) as caught:
            auth.user_token('owner-1', ['llm'])
        envelope = caught.value.envelope()
        assert envelope['auth_state'] == 'blocked' and envelope['model_action'] == 'stop_and_report'
        assert envelope['login_attempted'] is True and envelope['user_action_required'] is True
    assert calls == ['token', 'login']


def test_business_calls_wait_out_another_process_approval_on_the_source_lock(monkeypatch):
    from tofu_mtsso_auth import shared
    waits = []
    real = shared._source_lock
    def spy(path, timeout):
        waits.append(timeout)
        return real(path, timeout)
    monkeypatch.setattr(shared, '_source_lock', spy)
    monkeypatch.setattr(auth, '_run', lambda argv, **kw: (0, success(json.loads(kw['input_data'])), ''))
    auth.user_token('owner-1', ['a'])
    auth.session_status('owner-2', ['a'])
    assert waits == [auth._TIMEOUT + auth._LOGIN_TIMEOUT, auth._TIMEOUT]


def test_explicit_official_remains_authoritative_over_shared(monkeypatch):
    monkeypatch.setenv('MTSSO_AUTH_MODE', 'auto')
    monkeypatch.setenv('MTSSO_SHARED_SESSION', '1')
    monkeypatch.setenv('AGENT_SSO_CLIENT_ID', 'registered-agent')
    assert auth.enabled() is True
    assert auth.provider_status()['auth_source'] == 'mtsso_official'


def test_shared_requires_identity_and_installed_sdk(monkeypatch):
    monkeypatch.delenv('MTSSO_MIS')
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('owner-1', ['a'])
    assert caught.value.code == 'SSO_IDENTITY_REQUIRED'


def test_denied_login_is_not_replayed_by_force_refresh(monkeypatch):
    calls = []
    def denied(argv, **kwargs):
        calls.append(argv)
        return 42, '{"error":"sub_access_denied","message":"请申请权限"}', ''
    monkeypatch.setattr(auth, '_run', denied)
    for force in (False, True):
        with pytest.raises(auth.OfficialAuthError):
            auth.user_token('owner-1', ['a'], force_refresh=force)
    assert len(calls) == 1


def test_source_lock_and_session_fencing(monkeypatch):
    def run(argv, **kwargs):
        auth.clear()
        return 0, success(json.loads(kwargs['input_data']), 'old-session'), ''
    monkeypatch.setattr(auth, '_run', run)
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('owner-1', ['a'])
    assert caught.value.code == 'SSO_SESSION_CHANGED'
    assert auth.cache_status('owner-1', ['a'])['logged_in'] is False


@pytest.mark.parametrize('remaining', [None, {'mis': 'alice'}])
def test_auto_selected_shared_never_downgrades_after_source_loss(monkeypatch, tmp_path, remaining):
    from tofu_mtsso_auth import session
    source = tmp_path / '.mlp-cli-token.json'
    source.write_text(json.dumps({'mis': 'alice', 'access_token': 'fixture-source'}))
    source.chmod(0o600)
    monkeypatch.setenv('MTSSO_AUTH_MODE', 'auto')
    monkeypatch.delenv('MTSSO_SESSION_SDK')
    monkeypatch.setattr(session, 'sdk_path', lambda: '/installed/auth.js')
    assert auth.provider_status()['auth_source'] == 'mtsso_shared_session'
    if remaining is None:
        source.unlink()
    else:
        source.write_text(json.dumps(remaining))
    auth.invalidate()
    assert auth.enabled()
    assert auth.provider_status()['auth_source'] == 'mtsso_shared_session'
    auth.clear()
    assert auth.provider_status()['auth_source'] == 'mcp_existing'


def test_shared_business_target_is_bound_before_exchange(monkeypatch):
    monkeypatch.setattr(auth, '_run', lambda *a, **k: pytest.fail('wrong identity must not exchange'))
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('owner-1', ['a'], mis='bob')
    assert caught.value.code == 'SSO_IDENTITY_MISMATCH'


def test_denial_after_sdk_refresh_is_not_replayed(monkeypatch, tmp_path):
    source = tmp_path / '.mlp-cli-token.json'
    source.write_text(json.dumps({'mis': 'alice', 'access_token': 'fixture-source'}))
    source.chmod(0o600)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        source.write_text(json.dumps({'mis': 'alice', 'access_token': 'fixture-refreshed'}))
        return 42, '{"code":"act_access_denied","message":"请申请权限"}', ''
    monkeypatch.setattr(auth, '_run', run)
    for force in (False, True):
        with pytest.raises(auth.OfficialAuthError) as caught:
            auth.user_token('owner-1', ['a'], force_refresh=force)
        assert caught.value.code == 'act_access_denied'
    assert len(calls) == 1


def test_changed_sdk_cannot_adopt_a_result_from_old_sdk(monkeypatch, tmp_path):
    replacement = tmp_path / 'replacement.js'
    replacement.write_text('export const ssoAuth = {};')
    def run(argv, **kwargs):
        request = json.loads(kwargs['input_data'])
        monkeypatch.setenv('MTSSO_SESSION_SDK', str(replacement))
        return 0, success(request), ''
    monkeypatch.setattr(auth, '_run', run)
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('owner-1', ['a'])
    assert caught.value.code == 'SSO_SESSION_CHANGED'
    assert not auth.cache_status('owner-1', ['a'])['logged_in']


def test_verified_identity_is_environment_bound(monkeypatch):
    monkeypatch.setattr(auth, '_run', lambda argv, **kw: (0, success(json.loads(kw['input_data'])), ''))
    auth.user_token('owner-1', ['a'])
    assert auth.verified_subject('owner-1') == 'alice'
    monkeypatch.setenv('MTSSO_ENV', 'TEST')
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.verified_subject('owner-1')
    assert caught.value.code == 'SSO_ENV_INVALID'


def test_node_resolution_counts_against_shared_exchange_deadline(monkeypatch):
    from tofu_mtsso_auth import shared, session
    clock = [0.0]
    monkeypatch.setattr(shared.time, 'monotonic', lambda: clock[0])
    def resolve(path):
        clock[0] = 0.08
        return ['/synthetic/node', path]
    with pytest.raises(auth.OfficialAuthError) as caught:
        shared.exchange(session.configuration(), ['a'], environment='PROD', login=False,
            timeout=0.05, node_command=resolve,
            run=lambda *a, **kw: pytest.fail('expired deadline must never launch'))
    assert caught.value.code == 'SSO_TIMEOUT'


def test_auto_shared_source_is_pinned_when_second_local_file_exists(monkeypatch, tmp_path):
    from tofu_mtsso_auth import session
    first = tmp_path / '.mlp-cli-token.json'
    second = tmp_path / '.llm-cli-token.json'
    for path in (first, second):
        path.write_text(json.dumps({'mis': 'alice', 'access_token': 'fixture-source'}))
        path.chmod(0o600)
    monkeypatch.setenv('MTSSO_AUTH_MODE', 'auto')
    monkeypatch.delenv('MTSSO_SESSION_SDK')
    monkeypatch.setattr(session, 'sdk_path', lambda: '/installed/auth.js')
    monkeypatch.setattr(auth, '_run', lambda argv, **kw: (0, success(json.loads(kw['input_data'])), ''))
    assert auth.session_status('owner-1', ['a'])['logged_in']
    first.unlink()
    assert not auth.session_status('owner-1', ['a'])['logged_in']
    monkeypatch.setattr(auth, '_run', lambda *a, **kw: pytest.fail('pinned source may not switch to second file'))
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('owner-1', ['a'])
    assert caught.value.code == 'SSO_SESSION_CHANGED'


def test_explicit_login_environment_overrides_ambient_environment(monkeypatch):
    monkeypatch.setenv('MTSSO_ENV', 'TEST')
    monkeypatch.setattr(auth, '_run', lambda argv, **kw: (0, success(json.loads(kw['input_data'])), ''))
    result = auth.login('owner-1', ['a'], environment='PROD', mis='alice')
    assert result['approved']
    assert result['subject'] == 'alice'
    assert auth.verified_subject('owner-1', environment='PROD') == 'alice'
