import json
import sys
import pytest
import tofu_mtsso_auth as auth

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean():
    auth.clear()
    yield
    auth.clear()


@pytest.mark.parametrize("status,body,code", [
    (1, {"access_token": "secret"}, "SSO_EXCHANGE_FAILED"),
    (42, {"code": "ric_feedback_required", "message": "请确认授权"}, "ric_feedback_required"),
    (1, {"error": "sub_access_denied", "error_description": "请申请权限"}, "sub_access_denied"),
    (0, {}, "SSO_TOKEN_MISSING"),
    (0, {"success": False, "error": {"code": "MOA_AUTH_REQUEST_PENDING", "message": "已有授权卡片"}}, "MOA_AUTH_REQUEST_PENDING"),
])
def test_failure_never_accepts_ticket(status, body, code):
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(status, json.dumps(body))
    result = caught.value.envelope()
    assert result["error_code"] == code
    assert result["login_required"] is False
    assert result["retryable"] is False
    assert "secret" not in json.dumps(result)


def test_official_human_message_preserved():
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(42, '{"code":"act_access_denied","message":"请到UAC申请权限"}')
    assert caught.value.envelope()["error"] == "请到UAC申请权限"
    assert caught.value.envelope()["user_action_required"] is True


def test_gateway_placeholder_requires_verified_http_bridge():
    with pytest.raises(auth.OfficialAuthError, match="gateway"):
        auth._parse(0, "AT_FOR_GW_BASE64_secret")


def test_cache_scopes_owner_env_and_audience_and_invalidates(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "_executable", lambda: "/official")
    def run(argv):
        calls.append(argv)
        return 0, json.dumps({"access_token": str(len(calls)), "expires_in": 10800}), ""
    monkeypatch.setattr(auth, "_run", run)
    assert auth.user_token("alice", ["b", "a"]) == "1"
    assert auth.user_token("alice", ["a", "b"]) == "1"
    assert auth.user_token("bob", ["a", "b"]) == "2"
    assert auth.user_token("alice", ["a", "b"], environment="TEST") == "3"
    assert auth.user_token("alice", ["a"]) == "4"
    assert len(calls) == 4
    assert auth.cache_status("alice", ["a"])["logged_in"] is True
    auth.clear()
    assert auth.cache_status("alice", ["a"])["logged_in"] is False
    assert auth.user_token("alice", ["a"]) == "5"
    assert auth.user_token("alice", ["a"], force_refresh=True) == "6"


@pytest.mark.parametrize("audiences", [[], ["a b"], [1], [str(i) for i in range(6)], "a"])
def test_invalid_audiences_never_spawn(monkeypatch, audiences):
    monkeypatch.setattr(auth, "_run", lambda _: pytest.fail("must not run"))
    with pytest.raises(auth.OfficialAuthError):
        auth.user_token("alice", audiences)


def test_unknown_expiry_is_not_cached(monkeypatch):
    monkeypatch.setattr(auth, "_executable", lambda: "/official")
    monkeypatch.setattr(auth, "_run", lambda _: (0, '{"access_token":"secret"}', ""))
    auth.user_token("alice", ["a"])
    assert not auth.cache_status("alice", ["a"])["logged_in"]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_overflow_is_bounded_and_reaped(monkeypatch, stream):
    monkeypatch.setattr(auth, "_MAX_OUTPUT", 100)
    with pytest.raises(auth.OfficialAuthError, match="budget"):
        auth._run([sys.executable, "-c", f"import sys;sys.{stream}.write('x'*10000)"])


def test_timeout_kills_and_reaps(monkeypatch):
    monkeypatch.setattr(auth, "_TIMEOUT", 0.1)
    with pytest.raises(auth.OfficialAuthError, match="budget"):
        auth._run([sys.executable, "-c", "import time;time.sleep(20)"])


def test_clear_during_exchange_cannot_repopulate_session(monkeypatch):
    monkeypatch.setattr(auth, "_executable", lambda: "/official")
    def run(argv):
        auth.clear()
        return 0, '{"access_token":"old","expires_in":3600}', ""
    monkeypatch.setattr(auth, "_run", run)
    with pytest.raises(auth.OfficialAuthError, match="cleared"):
        auth.user_token("alice", ["a"])
    assert not auth.cache_status("alice", ["a"])["logged_in"]


def test_unrelated_cache_hit_not_blocked_by_exchange(monkeypatch):
    import threading
    monkeypatch.setattr(auth, "_executable", lambda: "/official")
    monkeypatch.setattr(auth, "_run", lambda _: (0, '{"access_token":"cached","expires_in":3600}', ""))
    auth.user_token("alice", ["a"])
    started, finish = threading.Event(), threading.Event()
    def slow(argv):
        started.set()
        assert finish.wait(2)
        return 0, '{"access_token":"fresh","expires_in":3600}', ""
    monkeypatch.setattr(auth, "_run", slow)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(auth.user_token, "bob", ["b"])
        assert started.wait(1)
        try:
            assert auth.user_token("alice", ["a"]) == "cached"
        finally:
            finish.set()
        assert pending.result() == "fresh"


def test_terminal_denial_is_coalesced_across_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "_executable", lambda: "/official")
    def denied(argv):
        calls.append(argv)
        return 42, '{"code":"ric_feedback_required","message":"请确认授权"}', ""
    monkeypatch.setattr(auth, "_run", denied)
    for _ in range(2):
        with pytest.raises(auth.OfficialAuthError):
            auth.user_token("alice", ["a"])
    assert len(calls) == 1


def test_stderr_retained_for_safe_runtime_diagnosis():
    status, stdout, stderr = auth._run([
        sys.executable, '-c',
        'import sys;sys.stderr.write("GLIBC_2.28 not found secret-ticket");sys.exit(1)',
    ])
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(status, stdout, stderr)
    assert caught.value.code == 'SSO_NODE_RUNTIME_INCOMPATIBLE'
    assert 'secret-ticket' not in json.dumps(caught.value.envelope())
    assert 'MTSSO_NODE_EXECUTABLE' in caught.value.message


def test_structured_stderr_preserves_authorization_failure():
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(42, '', json.dumps({
            'error': {'code': 'MOA_AUTH_REQUEST_PENDING', 'message': '已有授权卡片'},
            'access_token': 'secret-ticket',
        }))
    assert caught.value.code == 'MOA_AUTH_REQUEST_PENDING'
    assert caught.value.user_action
    assert 'secret-ticket' not in json.dumps(caught.value.envelope())


def test_arbitrary_stderr_never_leaves_adapter():
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(7, '', 'Bearer secret-ticket')
    assert caught.value.code == 'SSO_EXCHANGE_FAILED'
    assert '7' in caught.value.message
    assert 'secret-ticket' not in json.dumps(caught.value.envelope())


def test_node_selection_skips_broken_runtime_without_changing_cli(monkeypatch, tmp_path):
    script = tmp_path / 'official'
    script.write_text('#!/usr/bin/env node\n')
    monkeypatch.delenv('MTSSO_NODE_EXECUTABLE', raising=False)
    monkeypatch.setenv('PATH', '/broken:/working:/working')
    monkeypatch.setattr(auth.shutil, 'which', lambda name, path=None: path + '/node')
    calls = []
    def probe(argv, **kwargs):
        calls.append(argv)
        return (1, '', 'GLIBC_2.28 not found') if argv[0].startswith('/broken') else (0, 'v20.19.0\n', '')
    monkeypatch.setattr(auth, '_run', probe)
    assert auth._node_command(str(script)) == ['/working/node', str(script)]
    assert calls == [['/broken/node', '--version'], ['/working/node', '--version']]


def test_explicit_node_does_not_fall_back(monkeypatch, tmp_path):
    script = tmp_path / 'official'
    script.write_text('#!/usr/bin/env node\n')
    monkeypatch.setenv('MTSSO_NODE_EXECUTABLE', '/chosen/node')
    monkeypatch.setattr(auth.shutil, 'which', lambda name: name)
    monkeypatch.setattr(auth, '_run', lambda argv, **kw: (1, '', 'GLIBC_2.28 not found'))
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._node_command(str(script))
    assert caught.value.code == 'SSO_NODE_RUNTIME_INCOMPATIBLE'


def test_non_node_override_is_launched_unchanged(tmp_path):
    script = tmp_path / 'official'
    script.write_text('#!/bin/sh\n')
    assert auth._node_command(str(script)) == [str(script)]


def test_human_stderr_message_redacts_credentials():
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(42, '', json.dumps({
            'code': 'MOA_NOT_LOGGED_IN', 'message': '请授权 secret-ticket Bearer other-secret',
            'access_token': 'secret-ticket',
        }))
    assert caught.value.user_action
    assert 'secret-ticket' not in caught.value.message
    assert 'other-secret' not in caught.value.message


def test_code_cannot_echo_arbitrary_prose_or_ticket():
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(1, '', json.dumps({'code': 'Bearer secret-ticket'}))
    assert caught.value.code == 'SSO_EXCHANGE_FAILED'


@pytest.mark.parametrize('status', [0, 1])
def test_malformed_error_code_never_accepts_ticket(status):
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._parse(status, json.dumps({'code': 'Bearer secret-ticket',
                                      'access_token': 'secret-ticket'}))
    assert caught.value.code == 'SSO_EXCHANGE_FAILED'


def test_node_candidates_are_deduplicated_and_probes_bounded(monkeypatch, tmp_path):
    script = tmp_path / 'official'
    script.write_text('#!/usr/bin/env node\n')
    monkeypatch.delenv('MTSSO_NODE_EXECUTABLE', raising=False)
    monkeypatch.setenv('PATH', '/same:/same:/same')
    monkeypatch.setattr(auth.shutil, 'which', lambda name, path=None: '/same/node')
    calls = []
    def probe(argv, **kwargs):
        assert 0 < kwargs['timeout'] <= 2
        calls.append(argv)
        raise auth.OfficialAuthError('SSO_TIMEOUT', 'probe expired')
    monkeypatch.setattr(auth, '_run', probe)
    with pytest.raises(auth.OfficialAuthError) as caught:
        auth._node_command(str(script))
    assert caught.value.code == 'SSO_NODE_UNAVAILABLE'
    assert len(calls) == 1
