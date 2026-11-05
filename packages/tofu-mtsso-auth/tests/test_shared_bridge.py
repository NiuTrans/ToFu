"""Real private-pipe Node bridge with fake upstream APIs, strictly no network."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time

import pytest
import tofu_mtsso_auth as auth

pytestmark = [pytest.mark.unit, pytest.mark.skipif(not shutil.which('node'), reason='Node bridge fixture')]


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    auth.clear()
    for key in tuple(os.environ):
        if key.startswith(('MTSSO_', 'AGENT_SSO_', 'LLM_MIS', 'MLP_MIS', 'HOPE_USERNAME', 'XUECHENG_MIS')):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MTSSO_AUTH_MODE', 'shared')
    monkeypatch.setenv('MTSSO_MIS', 'alice')
    sdk = tmp_path / 'sdk/auth.mjs'
    sdk.parent.mkdir()
    sdk.write_text('''
import fs from 'node:fs';
globalThis.fetch = () => { throw new Error('network forbidden'); };
export class SsoAuth {
  constructor(options) { Object.assign(this, options); }
  getAccessToken() { throw new Error('implicit login forbidden'); }
  forceLogin() { throw new Error('implicit login forbidden'); }
  async refreshAccessToken(refresh) {
    if (refresh === 'bad-refresh') return null;
    const data = JSON.parse(fs.readFileSync(this.tokenFile, 'utf8'));
    data.access_token = 'fixture-refreshed';
    data.expires_at = Date.now() + 3600000;
    fs.writeFileSync(this.tokenFile, JSON.stringify(data), {mode:0o600});
    return data.access_token;
  }
  async loginWithCiba(mis) {
    fs.writeFileSync(this.tokenFile, JSON.stringify({mis,access_token:'fixture-login',
      expires_at:Date.now()+3600000}), {mode:0o600});
  }
  saveMis(mis) {
    const data = JSON.parse(fs.readFileSync(this.tokenFile, 'utf8'));
    fs.writeFileSync(this.tokenFile, JSON.stringify({...data,mis}), {mode:0o600});
  }
}
export const ssoAuth = new SsoAuth({clientId:'fixture-client',clientSecret:'fixture-client-secret'});
''')
    official = sdk.parent / 'node_modules/@mtfe/mtsso-auth-official'
    official.mkdir(parents=True)
    (official / 'package.json').write_text(json.dumps({'name': '@mtfe/mtsso-auth-official', 'type':'commonjs',
      'exports': {'./introspect-token':'./introspect.cjs','./token-exchange':'./exchange.cjs'}}))
    (official / 'introspect.cjs').write_text('''
exports.runIntrospectToken = async (p) => ({exitCode:0,responseJson:{active:true,
  mt_subject_type:p.token==='application'?'APPLICATION':'ACCOUNT',
  sub:p.token==='wrong-user'?'bob':'alice'}});
''')
    (official / 'exchange.cjs').write_text('''
exports.runTokenExchange = async (p) => {
  if (p.audience==='denied') return {exitCode:1,responseJson:{error:'act_access_denied',
    error_description:'请申请权限 fixture-client-secret fixture-source fixture-result',
    access_token:'fixture-result'}};
  if (p.audience==='exit-failed') return {exitCode:1,responseJson:{access_token:'not-success'}};
  console.log('SDK noise fixture-source');
  return {exitCode:0,responseJson:{access_token:'audience:'+p.audience,expires_in:3600}};
};
''')
    monkeypatch.setenv('MTSSO_SESSION_SDK', str(sdk))
    source = tmp_path / '.mlp-cli-token.json'
    def store(token='fixture-source', **extra):
        source.write_text(json.dumps({'mis':'alice','access_token':token,
                                     'expires_at':time.time()*1000+3600000, **extra}))
        source.chmod(0o600)
    store()
    yield source, store
    auth.clear()


def test_real_bridge_verifies_source_and_reuses_session(bridge):
    source, _ = bridge
    before = source.read_bytes()
    assert auth.user_token('tofu:1', ['mlp']) == 'audience:mlp'
    assert auth.verified_subject('tofu:1') == 'alice'
    assert source.read_bytes() == before
    status = auth.session_status('tofu:1', ['mlp'])
    assert status['identity_verified'] is True and status['subject'] == 'alice'
    assert status['approval']['session_file'] == '~/.mlp-cli-token.json'
    assert auth.verified_subject('tofu:2') is None


@pytest.mark.parametrize('token', ['wrong-user','application'])
def test_introspected_mismatch_is_not_an_authorization_subject(bridge, token):
    source, store = bridge
    store(token)
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('tofu:1', ['mlp'])
    assert caught.value.code == 'SSO_IDENTITY_MISMATCH'
    assert auth.verified_subject('tofu:1') is None
    assert caught.value.envelope()['auth_state'] == 'blocked'
    assert caught.value.login_attempted is False


def test_permission_denial_preserves_message_and_redacts_all_tickets(bridge):
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('tofu:1', ['denied'])
    result = caught.value.envelope()
    assert result['error_code'] == 'act_access_denied'
    assert result['error'].startswith('请申请权限')
    assert not result['retryable']
    assert 'fixture-' not in json.dumps(result)


def test_exit_failure_never_accepts_stdout_token(bridge):
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('tofu:1', ['exit-failed'])
    assert caught.value.code == 'SSO_EXCHANGE_FAILED'


def test_status_reports_expired_session_without_requesting_approval(bridge):
    source, store = bridge
    store(expires_at=1)
    before = source.read_bytes()
    status = auth.session_status('tofu:1', ['mlp'])
    assert status['ok'] is True and status['logged_in'] is False
    assert status['auth_state'] == 'login_required'
    assert source.read_bytes() == before


def test_business_call_logs_in_once_when_session_expired_without_refresh(bridge):
    source, store = bridge
    store(expires_at=1)
    assert auth.user_token('tofu:1', ['mlp']) == 'audience:mlp'
    assert json.loads(source.read_text())['access_token'] == 'fixture-login'
    assert auth.verified_subject('tofu:1') == 'alice'
    result = auth.login('tofu:1', ['mlp'], mis='alice')
    assert result['approved'] and result['login_performed'] is False


def test_refresh_updates_only_sdk_source_and_fences_old_cache(bridge):
    source, store = bridge
    store(expires_at=1, refresh_token='fixture-refresh')
    assert auth.user_token('tofu:1', ['mlp']) == 'audience:mlp'
    assert json.loads(source.read_text())['access_token'] == 'fixture-refreshed'
    assert auth.cache_status('tofu:1', ['mlp'])['logged_in']
    store('wrong-user')
    assert not auth.cache_status('tofu:1', ['mlp'])['logged_in']
    assert auth.verified_subject('tofu:1') is None
    with pytest.raises(auth.OfficialAuthError):
        auth.user_token('tofu:1', ['mlp'])


def test_dead_refresh_token_recovers_through_one_approval(bridge):
    source, store = bridge
    store(expires_at=1, refresh_token='bad-refresh')
    before = source.read_bytes()
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('tofu:1', ['mlp'], interactive=False)
    assert caught.value.code == 'SSO_SESSION_REQUIRED'
    assert source.read_bytes() == before
    result = auth.login('tofu:1', ['mlp'], mis='alice')
    assert result['approved'] and result['login_performed'] is True
    assert json.loads(source.read_text())['access_token'] == 'fixture-login'


@pytest.mark.parametrize('message, code, state', [
    ('CIBA 登录超时，请重新执行登录命令 --ciba', 'SSO_LOGIN_NOT_APPROVED', 'login_required'),
    ('授权请求已过期，请重新执行登录命令 --ciba', 'SSO_LOGIN_NOT_APPROVED', 'login_required'),
    ('用户拒绝了授权请求', 'MOA_USER_REJECTED', 'blocked'),
])
def test_unfinished_approval_reports_its_state(bridge, message, code, state):
    source, store = bridge
    store(expires_at=1)
    sdk = Path(os.environ['MTSSO_SESSION_SDK'])
    sdk.write_text(sdk.read_text().replace("async loginWithCiba(mis) {",
        "async loginWithCiba(mis) {\n    throw new Error(%s);" % json.dumps(message)))
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('tofu:1', ['mlp'])
    envelope = caught.value.envelope()
    assert envelope['error_code'] == code and envelope['auth_state'] == state
    assert envelope['login_attempted'] is True and envelope['user_action_required'] is True
    assert ('approval' in envelope) is (state != 'blocked')


@pytest.mark.parametrize('kind', ['symlink','fifo','public','oversize'])
def test_source_file_admission_is_local_and_fail_closed(monkeypatch, tmp_path, kind):
    source = tmp_path / 'source.json'
    if kind == 'symlink':
        target = tmp_path / 'real.json'; target.write_text('{}'); source.symlink_to(target)
    elif kind == 'fifo': os.mkfifo(source)
    else:
        source.write_text('x'*65537 if kind == 'oversize' else '{}')
        source.chmod(0o644 if kind == 'public' else 0o600)
    from tofu_mtsso_auth.session import SessionConfigurationError, local_metadata
    with pytest.raises(SessionConfigurationError): local_metadata(str(source))


def test_private_stdin_and_output_limits(monkeypatch):
    import sys
    status, stdout, _ = auth._run([sys.executable,'-c',
        'import sys;print(len(sys.stdin.read()))'], input_data='safe-input')
    assert status == 0 and stdout.strip() == '10'
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._run([sys.executable,'-c','import time;time.sleep(10)'], input_data='x'*65537)
    assert caught.value.code == 'SSO_INPUT_LIMIT'


@pytest.mark.parametrize('kind', ['symlink', 'public', 'fifo'])
def test_sdk_temporary_file_is_admitted_before_refresh(bridge, kind):
    source, store = bridge
    store(expires_at=1, refresh_token='fixture-refresh')
    before = source.read_bytes()
    temporary = source.with_name(source.name + '.tmp')
    sentinel = source.with_name('sentinel.txt')
    sentinel.write_text('preserve-user-content')
    if kind == 'symlink':
        temporary.symlink_to(sentinel)
    elif kind == 'fifo':
        os.mkfifo(temporary)
    else:
        temporary.write_text('preserve-stale-content')
        temporary.chmod(0o644)
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('tofu:1', ['mlp'])
    assert caught.value.code == 'SSO_SESSION_CONFIGURATION'
    assert source.read_bytes() == before
    assert sentinel.read_text() == 'preserve-user-content'
    if kind == 'public':
        assert temporary.read_text() == 'preserve-stale-content'


def test_sdk_plain_terminal_denial_is_safe_and_preserved(bridge):
    source, store = bridge
    store(expires_at=1)
    sdk = Path(os.environ['MTSSO_SESSION_SDK'])
    sdk.write_text(sdk.read_text().replace("async loginWithCiba(mis) {", """async loginWithCiba(mis) {
    const error = new Error('请申请权限 fixture-client-secret access_token=fixture-private');
    error.code = 'act_access_denied';
    throw error;
    """))
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.login('tofu:1', ['mlp'], mis='alice')
    assert caught.value.code == 'act_access_denied'
    assert caught.value.user_action
    assert caught.value.message.startswith('请申请权限')
    assert 'fixture-' not in json.dumps(caught.value.envelope())


def test_introspection_conflict_cannot_be_overridden_by_jwt_alias(bridge):
    import base64
    source, store = bridge
    claims = base64.urlsafe_b64encode(json.dumps({'sub': 'alice', 'mt_subject_type': 'ACCOUNT'}).encode()).decode().rstrip('=')
    store('header.' + claims + '.signature')
    sdk = Path(os.environ['MTSSO_SESSION_SDK'])
    (sdk.parent / 'node_modules/@mtfe/mtsso-auth-official/introspect.cjs').write_text('''
exports.runIntrospectToken = async () => ({exitCode:0,responseJson:{active:true,
  mt_subject_type:'ACCOUNT',sub:'bob'}});
''')
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('tofu:1', ['mlp'])
    assert caught.value.code == 'SSO_IDENTITY_MISMATCH'
    assert auth.verified_subject('tofu:1') is None


def test_rotated_refresh_ticket_is_redacted_from_official_instruction(bridge):
    source, store = bridge
    store(expires_at=1, refresh_token='fixture-refresh')
    sdk = Path(os.environ['MTSSO_SESSION_SDK'])
    sdk.write_text(sdk.read_text().replace("data.access_token = 'fixture-refreshed';", "data.access_token = 'fixture-refreshed'; data.refresh_token = 'fixture-rotated-private';"))
    (sdk.parent / 'node_modules/@mtfe/mtsso-auth-official/exchange.cjs').write_text('''
exports.runTokenExchange = async () => ({exitCode:42,responseJson:{code:'act_access_denied',
  message:'请申请权限 fixture-rotated-private'}});
''')
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth.user_token('tofu:1', ['mlp'])
    assert caught.value.code == 'act_access_denied'
    assert 'fixture-rotated-private' not in json.dumps(caught.value.envelope())


def test_server_revoked_but_locally_fresh_session_self_heals(bridge):
    # The reported incident: status said logged in, the business call then
    # failed with "call the explicit login tool" because introspection was inactive.
    source, store = bridge
    store('fixture-inactive')
    sdk = Path(os.environ['MTSSO_SESSION_SDK'])
    (sdk.parent / 'node_modules/@mtfe/mtsso-auth-official/introspect.cjs').write_text('''
exports.runIntrospectToken = async p => ({exitCode:0,responseJson:{active:p.token!=='fixture-inactive',
  mt_subject_type:'ACCOUNT',sub:'alice'}});
''')
    before = source.read_bytes()
    status = auth.session_status('tofu:1', ['mlp'])
    assert status['logged_in'] is False and status['auth_state'] == 'login_required'
    assert source.read_bytes() == before
    assert auth.user_token('tofu:1', ['mlp']) == 'audience:mlp'
    assert json.loads(source.read_text())['access_token'] == 'fixture-login'
    assert auth.session_status('tofu:1', ['mlp'])['auth_state'] == 'ready'


def test_failed_revalidation_revokes_previously_verified_identity(bridge):
    source, store = bridge
    auth.user_token('tofu:1', ['mlp'])
    auth.user_token('tofu:1', ['citadel'])
    assert auth.verified_subject('tofu:1') == 'alice'
    sdk = Path(os.environ['MTSSO_SESSION_SDK'])
    (sdk.parent / 'node_modules/@mtfe/mtsso-auth-official/introspect.cjs').write_text('''
exports.runIntrospectToken = async () => ({exitCode:0,responseJson:{active:false}});
''')
    with pytest.raises(auth.OfficialAuthError):
        auth.user_token('tofu:1', ['mlp'], force_refresh=True)
    assert auth.verified_subject('tofu:1') is None
    assert not auth.cache_status('tofu:1', ['mlp'])['logged_in']


def test_oversize_request_is_rejected_before_spawning_any_process(monkeypatch):
    monkeypatch.setattr(auth.subprocess, 'Popen', lambda *a, **kw: pytest.fail('invalid input must never spawn'))
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._run(['/synthetic/no-launch'], input_data='x'*65537)
    assert caught.value.code == 'SSO_INPUT_LIMIT'
