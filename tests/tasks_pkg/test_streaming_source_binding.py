"""Streamed opt-in source reads retain canonical file witnesses and settlement."""
import json
import re
from types import SimpleNamespace

import pytest

from tests.memory.test_working_facts import facts as facts, artifact as artifact, storage as storage, turn_service, PRINCIPAL, query
from tests.tasks_pkg.test_native_gateway import task as gateway_task
from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
from lib.tasks_pkg.tool_dispatch._pipeline import execute_tool_pipeline
from lib.tasks_pkg.working_file_sources import file_witness_resolver
from lib.tools.project import READ_FILES_TOOL

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)


@pytest.mark.parametrize('bound', [False, True])
def test_streamed_file_read_keeps_source_semantics_through_real_pipeline(facts, tmp_path, bound):
    from lib.memory.working_facts import service
    from lib.tasks_pkg.handlers.project import _handle_project_tool
    path = tmp_path / 'basis.txt'
    path.write_text('Budget: 100\nRegion: east\n')
    task = gateway_task() | {'convId': facts['conv_id'], '_principalContext': PRINCIPAL.to_payload()}
    task['config'].update(projectPath=str(tmp_path), autoApply=True)
    task['_executable_tool_catalog'] = [READ_FILES_TOOL]
    task['_tool_env'] = SimpleNamespace(resolve=lambda name: _handle_project_tool,
        write_names=set(), idempotent_names={'read_files'}, confirmation_names=set())
    args = {'path': 'basis.txt', 'bind_sources': bound}
    call = {'id': 'streamed-source', 'type': 'function', 'function': {
        'name': 'read_files', 'arguments': json.dumps(args)}}
    accumulator = StreamingToolAccumulator(task, project_path=str(tmp_path))
    try:
        accumulator.on_tool_call_ready(call)
        accumulator.inject_into_cache(task)
        # An announced row is presentation only: it must not publish authority.
        assert not task.get('_working_file_sources')
        assistant = {'role': 'assistant', 'content': '', 'tool_calls': [call]}
        parsed, _ = parse_tool_calls(assistant, task, round_num=0,
            tool_round_num=accumulator.tool_round_num, project_enabled=True,
            early_announced=accumulator.announced_tc_map)
        messages = [assistant]
        assert execute_tool_pipeline(task, parsed, task['config'], str(tmp_path),
            True, [READ_FILES_TOOL], messages, [], 0, 'fixture') is False
        result = next(row['content'] for row in messages if row.get('role') == 'tool')
        assert 'Budget: 100' in result
        assert [row['status'] for row in task['toolRounds']] == ['done']
        source = re.search(r'\[File source: (\{.*\})\]', result)
        if not bound:
            assert source is None
            assert accumulator.submitted_count == 1
            assert not task.get('_working_file_sources')
            return
        assert source is not None, result
        descriptor = json.loads(source[1]) | {'quote': 'Budget: 100'}
        witness = file_witness_resolver(task)(descriptor)
        assert witness.call_id == call['id']
        saved = service.record_fact(facts['conv_id'], {'action': 'record',
            'revision': query(facts)['revision'], 'kind': 'observation',
            'text': 'Budget: 100', 'sources': [descriptor]}, principal=PRINCIPAL,
            task_id=task['id'], file_sources=file_witness_resolver(task))
        assert saved['facts'][0]['status'] == 'observed'
        assert saved['facts'][0]['producerTaskId'] == task['id']
        assert accumulator.submitted_count == 0
    finally:
        accumulator.close(wait=True)


def test_real_root_loop_records_a_streamed_file_observation(tmp_path, monkeypatch, chat_sidecar):
    """Only provider transport is replaced; routing, loop and tools stay real."""
    from tests.llm import test_llm_replay_scenarios as replay
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from lib.llm_dispatch import reset_dispatcher
    from lib.memory.working_facts import service

    calls = []

    def stream(body, *, on_tool_call_ready=None, on_content=None, **kwargs):
        index = len(calls)
        calls.append(body)
        if index == 0:
            name, args = 'read_files', {'path': 'basis.txt', 'bind_sources': True}
        elif index in (1, 2, 3):
            observed = '\n'.join(str(m.get('content', '')) for m in body['messages']
                                 if m.get('role') == 'tool')
            source = re.search(r'\[File source: (\{.*\})\]', observed)
            if source is None:
                pytest.fail(f'The real streamed model round received no bound file source: {observed}')
            name, args = 'record_working_fact', {'revision': 0, 'kind': 'observation',
                'text': 'Budget: 100', 'sources': [json.loads(source[1]) | {'quote': 'Budget: 100'}]}
            if index == 1:
                args['id'] = 'invented-new-id'
            elif index == 2:
                if 'working_fact_not_found' not in observed or 'omit id' not in observed:
                    pytest.fail('Missing actionable feedback for an invented fact identity')
                args['text'] = 'The file says Budget: 100'
            elif 'working_facts_invalid_request' not in observed:
                pytest.fail('Missing invalid-proposal feedback for a paraphrased observation')
        elif index == 4:
            content = 'Recorded the source-bound budget observation.'
            if on_content:
                on_content(content)
            return ProviderStreamResult(message={'role': 'assistant', 'content': content},
                compatibility_finish_reason='stop', provider_finish_reason='stop',
                saw_done=True, saw_finish_reason=True, usage={'prompt_tokens': 3,
                'completion_tokens': 2, 'total_tokens': 5}, state=ProviderStreamState.PROVIDER_FINISHED)
        else:
            pytest.fail('The bounded source scenario unexpectedly requested another model call')
        call = {'id': f'source-loop-{index}', 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(args)}}
        assert on_tool_call_ready is not None
        on_tool_call_ready(call)
        return ProviderStreamResult(message={'role': 'assistant', 'content': '', 'tool_calls': [call]},
            compatibility_finish_reason='tool_calls', provider_finish_reason='tool_calls',
            saw_done=True, saw_finish_reason=True, usage={'prompt_tokens': 3,
            'completion_tokens': 2, 'total_tokens': 5}, state=ProviderStreamState.PROVIDER_FINISHED)

    def forbidden(*args, **kwargs):
        pytest.fail('Unexpected provider transport in the bounded streaming scenario')

    def install(*args, **kwargs):
        from importlib import import_module
        monkeypatch.setattr('lib.llm.stream_chat', stream)
        monkeypatch.setattr('lib.llm.astream.async_stream_chat', forbidden)
        monkeypatch.setattr(import_module('lib.llm.chat'), 'chat', forbidden)
        monkeypatch.setattr('lib.llm.chat', forbidden)
        return 'controlled-provider-transport'

    def prepare(task):
        (tmp_path / 'project' / 'basis.txt').write_text('Budget: 100\n')
        task['config']['autoApply'] = True

    monkeypatch.setattr(replay, '_install_transport', install)
    try:
        run = replay._run_loop_scenario('streamed-file-source', tmp_path=tmp_path,
            monkeypatch=monkeypatch, user_prompt='Record the observed budget in basis.txt.',
            with_project=True, prepare=prepare)
        task = run['task']
        view = service.query_facts(task['convId'], principal=PRINCIPAL)
        assert len(calls) == 5
        assert [(f['text'], f['status'], f['producerTaskId']) for f in view['facts']] == [
            ('Budget: 100', 'observed', task['id'])]
        assert task['finishReason'] == 'stop' and not task.get('error')
        assert [row['status'] for row in task['toolRounds']] == ['done', 'error', 'error', 'done']
        assert view['facts'][0]['sources'][0]['kind'] == 'file'
    finally:
        reset_dispatcher()
