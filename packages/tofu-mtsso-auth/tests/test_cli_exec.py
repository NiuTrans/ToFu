"""Ticket-injecting launcher: child-only tickets, no implicit login, unchanged status."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import tofu_mtsso_auth as auth
from tofu_mtsso_auth import __main__ as cli

pytestmark = pytest.mark.unit
SOURCE = Path(__file__).resolve().parents[1] / 'src'


class Started(Exception):
    pass


@pytest.fixture
def launched(monkeypatch):
    calls = []

    def execvpe(file, args, env):
        calls.append((file, list(args), dict(env)))
        raise Started

    monkeypatch.setattr(cli.os, 'execvpe', execvpe)
    return calls


def test_each_ticket_reaches_only_the_child_environment(monkeypatch, capsys, launched):
    minted = []

    def user_token(owner, audiences, *, environment=None):
        minted.append((owner, tuple(audiences), environment))
        return 'ticket-for-' + '+'.join(audiences)

    monkeypatch.setattr(cli, 'user_token', user_token)
    monkeypatch.setenv('KEEP_ME', 'parent-value')
    with pytest.raises(Started):
        cli.main(['exec', '--owner', 'tofu:1',
                  '--ticket', 'MEITUAN_SSO_TOKEN_XM_XAI=xm-xai',
                  '--ticket', 'FILE_SSOID=pan.internal.example.com,file.internal.example.com',
                  '--', 'node', 'cli/dx.mjs', 'whoami'])
    assert minted == [('tofu:1', ('xm-xai',), None),
                      ('tofu:1', ('pan.internal.example.com', 'file.internal.example.com'), None)]
    [(file, args, env)] = launched
    assert (file, args) == ('node', ['node', 'cli/dx.mjs', 'whoami'])
    assert env['MEITUAN_SSO_TOKEN_XM_XAI'] == 'ticket-for-xm-xai'
    assert env['FILE_SSOID'] == 'ticket-for-pan.internal.example.com+file.internal.example.com'
    assert env['KEEP_ME'] == 'parent-value'
    assert 'MEITUAN_SSO_TOKEN_XM_XAI' not in os.environ
    out, err = capsys.readouterr()
    assert 'ticket-for' not in out + err


def test_explicit_environment_is_forwarded(monkeypatch, launched):
    seen = []
    monkeypatch.setattr(cli, 'user_token',
                        lambda owner, audiences, *, environment=None: seen.append(environment) or 't')
    with pytest.raises(Started):
        cli.main(['exec', '--owner', 'tofu:1', '--environment', 'TEST',
                  '--ticket', 'USER_TICKET=aud', '--', 'true'])
    assert seen == ['TEST']


@pytest.mark.parametrize('code, user_action, exit_code, model_action', [
    ('SSO_LOGIN_NOT_APPROVED', True, 42, 'ask_user_to_approve_then_retry'),
    ('sub_access_denied', True, 42, 'stop_and_report'),
    ('SSO_EXCHANGE_FAILED', False, 1, 'stop_and_report'),
])
def test_sso_failure_reports_envelope_and_never_starts_command(
        monkeypatch, capsys, launched, code, user_action, exit_code, model_action):
    def user_token(owner, audiences, *, environment=None):
        raise auth.OfficialAuthError(code, 'official instruction', user_action=user_action)

    monkeypatch.setattr(cli, 'user_token', user_token)
    assert cli.main(['exec', '--owner', 'tofu:1', '--ticket', 'SSO_TOKEN=xm-xai',
                     '--', 'node', 'x.mjs']) == exit_code
    assert launched == []
    out, err = capsys.readouterr()
    assert out == ''
    envelope = json.loads(err)
    assert envelope['error_code'] == code
    assert envelope['model_action'] == model_action
    assert envelope['user_action_required'] is (exit_code == 42)


@pytest.mark.parametrize('argv', [
    ['--owner', 'tofu:1', '--ticket', 'SSO_TOKEN=aud'],
    ['--owner', 'tofu:1', '--ticket', 'SSO_TOKEN=aud', '--'],
    ['--ticket', 'SSO_TOKEN=aud', '--', 'true'],
    ['--owner', 'tofu:1', '--', 'true'],
    ['--owner', 'tofu:1', '--ticket', 'SSO_TOKEN', '--', 'true'],
    ['--owner', 'tofu:1', '--ticket', 'SSO_TOKEN=', '--', 'true'],
    ['--owner', 'tofu:1', '--ticket', 'SSO_TOKEN=a,,b', '--', 'true'],
    ['--owner', 'tofu:1', '--ticket', 'sso_token=aud', '--', 'true'],
    ['--owner', 'tofu:1', '--ticket', 'SSO_TOKEN=a', '--ticket', 'SSO_TOKEN=b', '--', 'true'],
    # Runtime-interpreted variables would echo a ticket into diagnostics.
    ['--owner', 'tofu:1', '--ticket', 'NODE_OPTIONS=aud', '--', 'true'],
    ['--owner', 'tofu:1', '--ticket', 'LD_PRELOAD=aud', '--', 'true'],
    ['--owner', 'tofu:1', '--ticket', 'PATH=aud', '--', 'true'],
])
def test_malformed_launch_is_usage_error_before_any_exchange(monkeypatch, launched, argv):
    monkeypatch.setattr(cli, 'user_token', lambda *a, **k: pytest.fail('usage errors must not exchange'))
    with pytest.raises(SystemExit) as raised:
        cli.main(['exec', *argv])
    assert raised.value.code == 2
    assert launched == []


def test_missing_executable_is_reported_without_ticket(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'user_token', lambda *a, **k: 'secret-ticket')
    assert cli.main(['exec', '--owner', 'tofu:1', '--ticket', 'SSO_TOKEN=aud',
                     '--', '/nonexistent/tofu-mtsso-missing-command']) == 127
    out, err = capsys.readouterr()
    assert 'secret-ticket' not in out + err
    assert json.loads(err)['error_code'] == 'COMMAND_NOT_STARTED'


def test_status_entry_point_is_unchanged(monkeypatch, capsys):
    probes = []
    monkeypatch.setattr(cli, 'provider_status',
                        lambda *, probe=False: probes.append(probe) or {'ok': True, 'auth_source': 'mcp_existing'})
    assert cli.main([]) == 0
    assert cli.main(['--probe']) == 0
    assert probes == [False, True]
    assert json.loads(capsys.readouterr().out.splitlines()[0])['auth_source'] == 'mcp_existing'


def _module_env(tmp_path, stub=None):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('MTSSO_', 'AGENT_SSO_'))}
    paths = [str(SOURCE)]
    if stub is not None:
        (tmp_path / 'sitecustomize.py').write_text(stub)
        paths.insert(0, str(tmp_path))
    environment['PYTHONPATH'] = os.pathsep.join(paths)
    return environment


def test_module_launch_replaces_process_with_ticket_only_in_child(tmp_path):
    stub = ("import tofu_mtsso_auth\n"
            "tofu_mtsso_auth.user_token = lambda owner, audiences, *, environment=None: 'fake-ticket'\n")
    child = "import os, sys; sys.exit(0 if os.environ.get('DX_SSO_TOKEN') == 'fake-ticket' else 3)"
    result = subprocess.run(
        [sys.executable, '-m', 'tofu_mtsso_auth', 'exec', '--owner', 'tofu:1',
         '--ticket', 'DX_SSO_TOKEN=xm-xai', '--', sys.executable, '-c', child],
        env=_module_env(tmp_path, stub), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert 'fake-ticket' not in result.stdout + result.stderr


def test_module_launch_failure_never_runs_command(tmp_path):
    result = subprocess.run(
        [sys.executable, '-m', 'tofu_mtsso_auth', 'exec', '--owner', 'tofu:1',
         '--ticket', 'SSO_TOKEN=a b', '--', sys.executable, '-c', 'raise SystemExit(99)'],
        env=_module_env(tmp_path), capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert json.loads(result.stderr)['error_code'] == 'SSO_AUDIENCE_INVALID'
