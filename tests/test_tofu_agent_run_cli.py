"""Contract tests for the single-shot `tofu-agent run` subcommand."""

from __future__ import annotations

import json
import os

import pytest

from tofu_agent.models import AgentResult, AgentTimeoutError


class _FakeRuntime:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = []
        self.closed = False

    def run(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._result

    def close(self, *, abort=True, timeout_s=5.0):
        self.closed = True


def _result(status='done', **overrides):
    payload = dict(
        id='run-1',
        task_id='task-1',
        model='test-model',
        status=status,
        finish_reason='stop' if status == 'done' else 'error',
        content='final answer',
        thinking='',
        usage={'input_tokens': 10, 'output_tokens': 5},
        n_tool_rounds=3,
        error=None,
        tool_calls=(),
        provider_id='',
        trajectory_format='',
        trajectory=None,
        raw={},
    )
    payload.update(overrides)
    return AgentResult(**payload)


@pytest.fixture()
def runtime_slot(monkeypatch):
    slot = {}

    def _install(runtime):
        slot['runtime'] = runtime
        monkeypatch.setattr('tofu_agent.cli._runtime', lambda args: runtime)

    return slot, _install


def test_run_requires_a_task(monkeypatch, tmp_path):
    from tofu_agent.cli import main

    monkeypatch.delenv('TOFU_AGENT_RUN_TASK', raising=False)
    monkeypatch.delenv('TOFU_AGENT_RUN_TASK_FILE', raising=False)
    with pytest.raises(SystemExit) as excinfo:
        main(['--env-file', str(tmp_path / 'absent.env'), 'run'])
    assert excinfo.value.code == 2


def test_run_success_prints_json(runtime_slot, tmp_path, capsys):
    from tofu_agent.cli import main

    _slot, install = runtime_slot
    fake = _FakeRuntime(result=_result())
    install(fake)

    rc = main(['--env-file', str(tmp_path / 'absent.env'),
               'run', '--task', 'fix the bug'])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document['ok'] is True
    assert document['status'] == 'done'
    assert document['content'] == 'final answer'
    assert document['usage'] == {'input_tokens': 10, 'output_tokens': 5}
    assert document['n_tool_rounds'] == 3
    assert 'trajectory' not in document

    messages, kwargs = fake.calls[0]
    assert messages == [{'role': 'user', 'content': 'fix the bug'}]
    import os
    assert kwargs['config'] == {
        'project': os.getcwd(),
        'tools': '*',
        'tools.nativeExposure': 'full',
    }
    assert kwargs['trajectory'] == 'atif'
    assert fake.closed is True


def test_run_task_file_wins_and_forwards_config(runtime_slot, tmp_path, capsys):
    from tofu_agent.cli import main

    task_file = tmp_path / 'task.txt'
    task_file.write_text('from file\n', encoding='utf-8')
    _slot, install = runtime_slot
    fake = _FakeRuntime(result=_result(trajectory_format='tofu-native',
                                       trajectory=[{'role': 'user'}]))
    install(fake)

    rc = main(['--env-file', str(tmp_path / 'absent.env'),
               'run', '--task', 'inline', '--task-file', str(task_file),
               '--cwd', '/work/repo', '--tools', 'fetch,mcp',
               '--trajectory', 'tofu-native', '--timeout-s', '30'])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document['trajectory_format'] == 'tofu-native'
    assert document['trajectory'] == [{'role': 'user'}]

    messages, kwargs = fake.calls[0]
    assert messages == [{'role': 'user', 'content': 'from file\n'}]
    assert kwargs['config'] == {'project': '/work/repo',
                                'tools': ['fetch', 'mcp'],
                                'tools.nativeExposure': 'full'}
    assert kwargs['trajectory'] == 'tofu-native'
    assert kwargs['timeout_s'] == 30.0


def test_run_writes_output_file(runtime_slot, tmp_path, capsys):
    from tofu_agent.cli import main

    _slot, install = runtime_slot
    install(_FakeRuntime(result=_result()))
    output = tmp_path / 'result.json'

    rc = main(['--env-file', str(tmp_path / 'absent.env'),
               'run', '--task', 'x', '--output', str(output)])
    assert rc == 0
    assert capsys.readouterr().out == ''
    document = json.loads(output.read_text(encoding='utf-8'))
    assert document['ok'] is True


def test_run_agent_error_maps_to_exit_4(runtime_slot, tmp_path, capsys):
    from tofu_agent.cli import main

    _slot, install = runtime_slot
    install(_FakeRuntime(result=_result(
        status='error',
        error={'kind': 'provider', 'message': 'boom'},
    )))
    rc = main(['--env-file', str(tmp_path / 'absent.env'),
               'run', '--task', 'x'])
    assert rc == 4
    document = json.loads(capsys.readouterr().out)
    assert document['ok'] is False
    assert document['error']['kind'] == 'provider'

def test_run_retriable_upstream_error_maps_to_exit_6(
        runtime_slot, tmp_path, capsys):
    from tofu_agent.cli import main

    _slot, install = runtime_slot
    install(_FakeRuntime(result=_result(
        status='error',
        error={'kind': 'upstream_error', 'message': 'gateway 503 storm',
               'retryable': True},
    )))
    rc = main(['--env-file', str(tmp_path / 'absent.env'),
               'run', '--task', 'x'])
    assert rc == 6
    document = json.loads(capsys.readouterr().out)
    assert document['ok'] is False
    assert document['error']['kind'] == 'upstream_error'
    assert document['error']['retryable'] is True


def test_run_permanent_envelope_stays_exit_4(runtime_slot, tmp_path, capsys):
    from tofu_agent.cli import main

    _slot, install = runtime_slot
    install(_FakeRuntime(result=_result(
        status='error',
        error={'kind': 'bad_request', 'message': 'HTTP 400',
               'retryable': False},
    )))
    rc = main(['--env-file', str(tmp_path / 'absent.env'),
               'run', '--task', 'x'])
    assert rc == 4


def test_run_timeout_maps_to_exit_3(runtime_slot, tmp_path, capsys):
    from tofu_agent.cli import main

    _slot, install = runtime_slot
    install(_FakeRuntime(exc=AgentTimeoutError('too slow')))
    rc = main(['--env-file', str(tmp_path / 'absent.env'),
               'run', '--task', 'x', '--timeout-s', '5'])
    assert rc == 3
    document = json.loads(capsys.readouterr().out)
    assert document['ok'] is False
    assert document['status'] == 'timeout'
def test_run_timeout_maps_to_exit_3(runtime_slot, tmp_path, capsys):
    from tofu_agent.cli import main

    _slot, install = runtime_slot
    install(_FakeRuntime(exc=AgentTimeoutError('too slow')))
    rc = main(['--env-file', str(tmp_path / 'absent.env'),
               'run', '--task', 'x', '--timeout-s', '5'])
    assert rc == 3
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document['ok'] is False
    assert document['status'] == 'timeout'
    assert 'kind=timeout' in captured.err


def test_run_env_overrides_defaults(runtime_slot, tmp_path, monkeypatch):
    from tofu_agent.cli import main

    monkeypatch.setenv('TOFU_AGENT_RUN_TOOLS', 'fetch,mcp')
    monkeypatch.setenv('TOFU_AGENT_RUN_TRAJECTORY', '')
    monkeypatch.setenv('TOFU_AGENT_RUN_NATIVE_EXPOSURE', 'routed')
    _slot, install = runtime_slot
    fake = _FakeRuntime(result=_result())
    install(fake)

    rc = main(['--env-file', str(tmp_path / 'absent.env'),
               'run', '--task', 'x'])
    assert rc == 0
    _messages, kwargs = fake.calls[0]
    assert kwargs['config']['tools'] == ['fetch', 'mcp']
    assert kwargs['config']['tools.nativeExposure'] == 'routed'
    assert kwargs['trajectory'] is None


def test_run_disables_configured_slots_by_default(
        runtime_slot, tmp_path, monkeypatch):
    from tofu_agent.cli import main

    monkeypatch.delenv('TOFU_DISABLE_CONFIGURED_SLOTS', raising=False)
    _slot, install = runtime_slot
    install(_FakeRuntime(result=_result()))
    rc = main(['--env-file', str(tmp_path / 'absent.env'),
               'run', '--task', 'x'])
    assert rc == 0
    assert os.environ['TOFU_DISABLE_CONFIGURED_SLOTS'] == '1'

    monkeypatch.setenv('TOFU_DISABLE_CONFIGURED_SLOTS', '0')
    install(_FakeRuntime(result=_result()))
    rc = main(['--env-file', str(tmp_path / 'absent.env'),
               'run', '--task', 'x'])
    assert rc == 0
    assert os.environ['TOFU_DISABLE_CONFIGURED_SLOTS'] == '0'


def test_run_error_summary_goes_to_stderr(runtime_slot, tmp_path, capsys):
    from tofu_agent.cli import main

    _slot, install = runtime_slot
    install(_FakeRuntime(result=_result(
        status='error',
        error={'kind': 'network', 'message': 'LLM call failed',
               'detail': 'HTTP 403 from proxy'},
    )))
    rc = main(['--env-file', str(tmp_path / 'absent.env'),
               'run', '--task', 'x'])
    assert rc == 4
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document['ok'] is False
    assert 'kind=network' in captured.err
    assert 'HTTP 403 from proxy' in captured.err


def _envelope(*base_urls):
    return {'connections': [{'base_url': url} for url in base_urls]}


def test_self_heal_no_proxy_appends_missing_host(monkeypatch, capsys):
    from types import SimpleNamespace
    from tofu_agent.cli import _self_heal_no_proxy

    monkeypatch.setenv('http_proxy', 'http://proxy:3128')
    monkeypatch.delenv('https_proxy', raising=False)
    monkeypatch.delenv('HTTPS_PROXY', raising=False)
    monkeypatch.delenv('HTTP_PROXY', raising=False)
    monkeypatch.setenv('no_proxy', 'localhost,127.0.0.1')
    monkeypatch.delenv('NO_PROXY', raising=False)
    access = SimpleNamespace(document=_envelope('http://33.236.209.126:8081'))
    _self_heal_no_proxy(access)
    assert '33.236.209.126' in os.environ['no_proxy'].split(',')
    assert 'auto-appended 33.236.209.126' in capsys.readouterr().err


def test_self_heal_no_proxy_noop_when_covered_or_unproxied(
        monkeypatch, capsys):
    from types import SimpleNamespace
    from tofu_agent.cli import _self_heal_no_proxy

    access = SimpleNamespace(document=_envelope('http://33.236.209.126:8081'))

    monkeypatch.delenv('http_proxy', raising=False)
    monkeypatch.delenv('https_proxy', raising=False)
    monkeypatch.delenv('HTTP_PROXY', raising=False)
    monkeypatch.delenv('HTTPS_PROXY', raising=False)
    monkeypatch.setenv('no_proxy', 'localhost')
    _self_heal_no_proxy(access)
    assert os.environ['no_proxy'] == 'localhost'

    monkeypatch.setenv('http_proxy', 'http://proxy:3128')
    monkeypatch.setenv('no_proxy', 'localhost,33.236.209.126')
    _self_heal_no_proxy(access)
    assert os.environ['no_proxy'] == 'localhost,33.236.209.126'
    assert capsys.readouterr().err == ''


def test_self_heal_no_proxy_suffix_and_wildcard_match(monkeypatch):
    from tofu_agent.cli import _no_proxy_entry_matches

    assert _no_proxy_entry_matches('*', 'anything')
    assert _no_proxy_entry_matches('example.com', 'api.example.com')
    assert _no_proxy_entry_matches('example.com', 'example.com')
    assert _no_proxy_entry_matches('.example.com', 'api.example.com')
    assert not _no_proxy_entry_matches('example.com', 'notexample.com')
    assert not _no_proxy_entry_matches('', 'host')
