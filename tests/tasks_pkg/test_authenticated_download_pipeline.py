"""Real streamed run_command settlement preserves download-intent boundaries.

Only the provider transport and external command executor are replaced. Tool
assembly, routing, pre-spawn interception, round settlement and storage run
through the existing root-loop driver; no HTTP/download is issued.
"""
from __future__ import annotations

from importlib import import_module
import json

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit

_DIAGNOSTIC = '''python3 - <<'PY'
examples = ["example curl -s https://files.test/api", "python3 x.py >> out"]
print(examples)
PY
git grep -B2 "def reader" -- lib
'''
_BLOCKED = "cd /tmp && curl -b 'sid=synthetic' -o out https://files.test/data"


@pytest.mark.parametrize('with_project', [False, True], ids=['standalone', 'project'])
@pytest.mark.parametrize('blocked', [False, True], ids=['literal-diagnostic', 'real-download'])
def test_streamed_command_intent_survives_the_real_root_loop(
        tmp_path, monkeypatch, chat_sidecar, with_project, blocked):
    from tests.llm import test_llm_replay_scenarios as replay
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from lib.llm_dispatch import reset_dispatcher
    import lib.project_mod as project_mod
    import lib.tasks_pkg.handlers.search._core as acquisition

    source = _BLOCKED if blocked else _DIAGNOSTIC
    provider_calls, executions = [], []

    def execute(name, arguments, *args, **kwargs):
        assert name == 'run_command'
        assert arguments['command'] == source
        if blocked:
            pytest.fail('A real wrapped cookie download reached the subprocess boundary')
        executions.append(arguments['command'])
        return f'$ {source}\nNORMAL-RESULT\n[exit code: 0]'

    def forbidden(*args, **kwargs):
        pytest.fail('Unexpected HTTP acquisition or alternate provider transport')

    monkeypatch.setattr(project_mod, 'execute_standalone_command', execute)
    monkeypatch.setattr(project_mod, 'execute_tool', execute)
    monkeypatch.setattr(acquisition, 'download_url_to_server', forbidden)

    def stream(body, *, on_tool_call_ready=None, on_content=None, **kwargs):
        index = len(provider_calls)
        provider_calls.append(body)
        usage = {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}
        if index == 0:
            call = {'id': 'download-intent-boundary', 'type': 'function', 'function': {
                'name': 'run_command', 'arguments': json.dumps({'command': source})}}
            assert on_tool_call_ready is not None
            on_tool_call_ready(call)
            return ProviderStreamResult(
                message={'role': 'assistant', 'content': '', 'tool_calls': [call]},
                compatibility_finish_reason='tool_calls', provider_finish_reason='tool_calls',
                saw_done=True, saw_finish_reason=True, usage=usage,
                state=ProviderStreamState.PROVIDER_FINISHED)
        content = ('The wrapped download was blocked before execution. [END_TURN: blocked]'
                   if blocked else 'Verified the command boundary.')
        if on_content:
            on_content(content)
        return ProviderStreamResult(
            message={'role': 'assistant', 'content': content},
            compatibility_finish_reason='stop', provider_finish_reason='stop',
            saw_done=True, saw_finish_reason=True, usage=usage,
            state=ProviderStreamState.PROVIDER_FINISHED)

    def install(*args, **kwargs):
        monkeypatch.setattr('lib.llm.stream_chat', stream)
        monkeypatch.setattr('lib.llm.astream.async_stream_chat', forbidden)
        monkeypatch.setattr(import_module('lib.llm.chat'), 'chat', forbidden)
        monkeypatch.setattr('lib.llm.chat', forbidden)
        return 'controlled-provider-transport'

    def prepare(task):
        task['config']['autoApply'] = True

    monkeypatch.setattr(replay, '_install_transport', install)
    try:
        run = replay._run_loop_scenario(
            'download-intent-boundary', tmp_path=tmp_path, monkeypatch=monkeypatch,
            user_prompt='Check the shell command boundary and report the result.',
            with_project=with_project, prepare=prepare)
        task = run['task']
        assert len(provider_calls) == 2
        assert executions == ([] if blocked else [source])
        received = '\n'.join(str(row.get('content', '')) for row in provider_calls[1]['messages']
                             if row.get('role') == 'tool')
        if blocked:
            assert 'blocked before subprocess spawn' in received, received
            assert 'browser_download_url_to_server' in received
            assert 'synthetic' not in received
            assert 'NORMAL-RESULT' not in received
        else:
            assert 'NORMAL-RESULT' in received, received
            assert 'file download intercepted' not in received
        assert [row['status'] for row in task['toolRounds']] == [
            'error' if blocked else 'done']
        assert bool(task['toolRounds'][0].get('authenticatedDownloadRedirected')) is blocked
        assert task['finishReason'] == 'stop' and not task.get('error')
        replay._assert_invariants_clean(task)
    finally:
        reset_dispatcher()
