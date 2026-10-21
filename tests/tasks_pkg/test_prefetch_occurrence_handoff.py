"""Completed speculative reads belong to one admitted model occurrence."""
import json
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_fetch_http_failure_evidence import (
    fetch_environment as fetch_environment, unavailable_source, dispatch_fetch,
)
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task

pytestmark = pytest.mark.api
pytest_plugins = ('tests._chat_sidecar',)


def streamed_call(task, arguments, *, number=0, call_id='streamed-fetch'):
    task.setdefault('toolRounds', [])
    call = {'id': call_id, 'type': 'function', 'function': {
        'name': 'fetch_url', 'arguments': json.dumps(arguments)}}
    assistant = {'role': 'assistant', 'content': '', 'tool_calls': [call]}
    accumulator = StreamingToolAccumulator(task, project_path=None, round_num=number)
    accumulator.on_tool_call_ready(call)
    accumulator.reconcile_announced_rounds(assistant)
    return accumulator, assistant


def dispatch_stream(task, accumulator, assistant, *, number=0):
    parsed, _ = parse_tool_calls(assistant, task, round_num=number,
        tool_round_num=accumulator.tool_round_num, project_enabled=False,
        early_announced=accumulator.announced_tc_map)
    settled, messages = [], [assistant]
    execute_tool_pipeline(task, parsed, cfg=task['config'], project_path=None,
        project_enabled=False, tool_list=[], messages=messages,
        all_search_results_text=[], round_num=number, model='fixture',
        internal_result_sink=SimpleNamespace(capture=lambda _id, value: settled.append(value)))
    return parsed[0][5], messages[-1]['content'], settled


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('batch', [False, 'mixed', 'failed'])
def test_failed_prefetch_delivers_once_and_a_new_call_can_recover(
        mode, batch, fetch_environment, observations):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url

    task = recovery_task(_handle_fetch_url, 'read_only', mode)
    with unavailable_source() as (url, requests):
        mixed = batch == 'mixed'
        args = {'urls': [url, url + '?ok' if mixed else url]} if batch else {'url': url}
        accumulator, assistant = streamed_call(task, args)
        try:
            assert accumulator.inject_into_cache(task) == 1
            before = list(requests)
            row, content, settled = dispatch_stream(task, accumulator, assistant)
        finally:
            accumulator.close(wait=True)
        assert requests == before, 'formal dispatch repeated a completed speculative HTTP request'
        assert row['_cacheableResult'] is False
        assert settled[0].execution_status == ('ok' if mixed else 'error')
        assert '3600 seconds' in content
        if mixed:
            assert 'recovered-source' in content and len(row['results']) == 2
        elif not batch:
            assert tool_result_error(content).code == 'tool_http_unavailable'
        recovered, messages, _ = dispatch_fetch(task, {'url': url + '?ok'}, 1)
        assert recovered['status'] == 'done' and 'recovered-source' in messages[0]['content']
        assert len(requests) == len(before) + 1
    assert not task.get('_activeToolContexts')
    assert [entry['outcome'] for entry in observations[1]] == [('ok' if mixed else 'error'), 'ok']


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_later_same_url_read_observes_recovery(mode, fetch_environment, observations):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    task = recovery_task(_handle_fetch_url, 'read_only', mode)
    with unavailable_source(recover=True) as (url, requests):
        accumulator, assistant = streamed_call(task, {'url': url})
        try:
            accumulator.inject_into_cache(task)
            row, content, _ = dispatch_stream(task, accumulator, assistant)
        finally:
            accumulator.close(wait=True)
        assert requests == ['/source.json'] * 3  # Existing bounded transport retry.
        assert row['status'] == 'error' and tool_result_error(content).retryable
        recovered, messages, _ = dispatch_fetch(task, {'url': url}, 1)
        assert recovered['status'] == 'done' and 'recovered-source' in messages[0]['content']
        assert requests == ['/source.json'] * 4


@pytest.mark.parametrize('policy', ['idempotent', 'reconcile'])
def test_admitted_occurrence_owns_failure_policy(policy, fetch_environment, observations):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    task = recovery_task(_handle_fetch_url, policy)
    with unavailable_source() as (url, requests):
        accumulator, assistant = streamed_call(task, {'url': url})
        try:
            accumulator.inject_into_cache(task)
            row, content, _ = dispatch_stream(task, accumulator, assistant)
        finally:
            accumulator.close(wait=True)
    error = tool_result_error(content)
    assert requests == ['/source.json']
    assert error.retryable is (policy == 'idempotent')
    assert error.code == ('tool_http_unavailable' if policy == 'idempotent' else 'tool_effect_unknown')
    if policy == 'reconcile':
        assert row['executionEffect']['operationId'] in error.message


@pytest.mark.parametrize('change', ['owner', 'principal_scope', 'world', 'attempt',
                                  'task', 'conversation', 'arguments', 'browser', 'policy', 'question'])
def test_changed_identity_cannot_adopt_the_prior_read(change, fetch_environment, observations):
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    task = recovery_task(_handle_fetch_url, 'read_only')
    with unavailable_source() as (url, requests):
        accumulator, assistant = streamed_call(task, {'url': url})
        try:
            assert accumulator.inject_into_cache(task) == 1
            if change == 'owner':
                task['_userId'] = 2
                task['_principalContext'] = PrincipalContext.user(subject_id='user:2', owner_user_id=2).to_payload()
            elif change == 'principal_scope':
                task['_principalContext']['scopes'] = ['changed']
            elif change == 'arguments':
                assistant['tool_calls'][0]['function']['arguments'] = json.dumps({'url': url + '?ok'})
            elif change == 'browser':
                task['config']['browserClientId'] = 'replacement-browser'
            elif change == 'question':
                task['lastUserQuery'] = 'replacement question'
            elif change == 'policy':
                task['_toolContractDocumentsByName']['fetch_url']['effectRecovery'] = 'reconcile'
            else:
                field = {'world': '_worldVersion', 'attempt': '_attemptId',
                         'task': 'id', 'conversation': 'convId'}[change]
                task[field] = 'replacement'
            _, content, _ = dispatch_stream(task, accumulator, assistant)
        finally:
            accumulator.close(wait=True)
        assert len(requests) == 2, 'changed identity reused another scope\'s prefetch'
        if change == 'arguments':
            assert 'recovered-source' in content
    from lib.tasks_pkg.tool_dispatch._prefetch import PrefetchedToolResult
    assert not any(isinstance(value, PrefetchedToolResult)
                   for value in task['_tool_result_cache'].values())


@pytest.mark.parametrize('deny', ['stop', 'pre_hook'])
def test_ordinary_admission_can_refuse_completed_prefetch(
        deny, fetch_environment, observations, monkeypatch):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    task = recovery_task(_handle_fetch_url, 'read_only')
    with unavailable_source() as (url, requests):
        accumulator, assistant = streamed_call(task, {'url': url})
        try:
            accumulator.inject_into_cache(task)
            if deny == 'stop':
                task['aborted'] = True
            else:
                from lib.tasks_pkg.tool_hooks import HookResult
                monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._pipeline.run_pre_hooks',
                    lambda *_a, **_kw: HookResult('block', 'fixture admission refusal'))
            row, _, settled = dispatch_stream(task, accumulator, assistant)
        finally:
            accumulator.close(wait=True)
        assert requests == ['/source.json']
        assert row['status'] == ('aborted' if deny == 'stop' else 'rejected')
        assert settled[0].execution_status != 'ok'
    assert task['_tool_result_cache'] == {}


@pytest.mark.parametrize('retire', ['restart', 'orphan', 'stop'])
def test_retired_response_releases_completed_occurrence(retire, fetch_environment, observations):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    task = recovery_task(_handle_fetch_url, 'read_only')
    with unavailable_source() as (url, requests):
        accumulator, _ = streamed_call(task, {'url': url})
        try:
            assert accumulator.inject_into_cache(task) == 1
            if retire == 'restart':
                accumulator.on_provider_attempt_restart(reason='fixture retry')
            elif retire == 'orphan':
                accumulator.reconcile_announced_rounds({'tool_calls': []})
            else:
                task['aborted'] = True
                accumulator.close(wait=True)
            assert task['_tool_result_cache'] == {}
        finally:
            accumulator.close(wait=True)
        assert requests == ['/source.json']


def test_old_success_cache_cannot_mask_completed_failure(fetch_environment, observations):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key, _store_tool_result_cache_entry
    task = recovery_task(_handle_fetch_url, 'read_only')
    with unavailable_source() as (url, requests):
        args = {'url': url}
        _store_tool_result_cache_entry(task, _make_cache_key('fetch_url', args), ('OLD-SUCCESS', False))
        accumulator, assistant = streamed_call(task, args)
        try:
            accumulator.inject_into_cache(task)
            row, content, _ = dispatch_stream(task, accumulator, assistant)
        finally:
            accumulator.close(wait=True)
    assert requests == ['/source.json']
    assert row['status'] == 'error' and 'OLD-SUCCESS' not in content
    assert task['_tool_result_cache'] == {}


def test_admitted_handler_rechecks_arguments_after_pre_hook(fetch_environment, observations, monkeypatch):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    task = recovery_task(_handle_fetch_url, 'read_only')
    with unavailable_source() as (url, requests):
        accumulator, assistant = streamed_call(task, {'url': url})
        try:
            accumulator.inject_into_cache(task)
            def change_arguments(_name, args, _task):
                args['url'] = url + '?ok'  # Fault injection between preflight and entry.
            monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._pipeline.run_pre_hooks', change_arguments)
            row, content, _ = dispatch_stream(task, accumulator, assistant)
        finally:
            accumulator.close(wait=True)
    assert requests == ['/source.json', '/source.json?ok']
    assert row['status'] == 'done' and 'recovered-source' in content


def test_pending_source_results_share_existing_capacity_and_one_use_lifecycle():
    from lib.tasks_pkg.tool_dispatch._prefetch import (
        capture_prefetch, publish_prefetch, consume_prefetch, has_prefetch,
    )
    task = recovery_task(None, 'read_only')
    task['_tool_result_cache_capacity'] = 16
    rows = [{'roundNum': i, 'llmRound': 0} for i in range(32)]
    args = {'url': 'https://fixture.invalid/source'}
    for i, row in enumerate(rows):
        receipt = capture_prefetch(task, 'fetch_url', str(i), args, row, None).bind(('source', i))
        assert publish_prefetch(task, receipt, args, row, None)
    assert len(task['_tool_result_cache']) == 16
    assert not has_prefetch(task, 'fetch_url', '0', args, rows[0], None)
    row = rows[-1]
    assert consume_prefetch(task, 'fetch_url', '31', args, row, None) == ('source', 31)
    assert consume_prefetch(task, 'fetch_url', '31', args, row, None) is None


def test_newer_success_publication_survives_older_prefetch_failure(fetch_environment, observations):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key, _store_tool_result_cache_entry
    task = recovery_task(_handle_fetch_url, 'read_only')
    with unavailable_source() as (url, requests):
        args = {'url': url}
        key = _make_cache_key('fetch_url', args)
        _store_tool_result_cache_entry(task, key, ('OLDER-SUCCESS', False))
        accumulator, assistant = streamed_call(task, args)
        try:
            accumulator._futures['streamed-fetch'][0].result(timeout=5)
            newer = ('NEWER-SUCCESS', False)
            _store_tool_result_cache_entry(task, key, newer)
            accumulator.inject_into_cache(task)
            row, content, _ = dispatch_stream(task, accumulator, assistant)
        finally:
            accumulator.close(wait=True)
    assert requests == ['/source.json'] and row['status'] == 'error'
    assert 'NEWER-SUCCESS' not in content and task['_tool_result_cache'][key] is newer


def test_pending_prefetch_is_handed_off_after_its_worker_finishes(fetch_environment, observations, monkeypatch):
    import threading
    from lib.tasks_pkg.handlers.search import _core
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    entered, release = threading.Event(), threading.Event()
    original = _core._fetch_url_one
    def waiting(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'owned test worker was not released'
        return original(*args, **kwargs)
    monkeypatch.setattr(_core, '_fetch_url_one', waiting)
    task = recovery_task(_handle_fetch_url, 'read_only')
    with unavailable_source() as (url, requests):
        accumulator, assistant = streamed_call(task, {'url': url})
        timer = threading.Timer(.05, release.set)
        try:
            assert entered.wait(5)
            assert not accumulator._futures['streamed-fetch'][0].done()
            timer.start()
            assert accumulator.inject_into_cache(task) == 1
            row, _, _ = dispatch_stream(task, accumulator, assistant)
        finally:
            release.set()
            if timer.ident is not None:
                timer.join()
            accumulator.close(wait=True)
    assert row['status'] == 'error' and requests == ['/source.json']


def test_withdrawn_collection_cannot_turn_handoff_into_another_http_request(fetch_environment, observations, monkeypatch):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    from lib.tasks_pkg.tool_dispatch import _prefetch
    from lib.tasks_pkg.tool_runtime import close_tool_result_publication
    original = _prefetch.consume_prefetch
    def withdrawn(task, name, call_id, args, row, project_path):
        _prefetch.discard_prefetch(task, name, row)
        close_tool_result_publication(task, round_num=row['roundNum'], tool_call_id=call_id,
                                      round_entry=row, terminal_status='error')
        return original(task, name, call_id, args, row, project_path)
    monkeypatch.setattr(_prefetch, 'consume_prefetch', withdrawn)
    task = recovery_task(_handle_fetch_url, 'read_only')
    with unavailable_source() as (url, requests):
        accumulator, assistant = streamed_call(task, {'url': url})
        try:
            accumulator.inject_into_cache(task)
            row, _, _ = dispatch_stream(task, accumulator, assistant)
        finally:
            accumulator.close(wait=True)
    assert requests == ['/source.json']
    assert row['status'] == 'error' and not task.get('_activeToolContexts')


def test_large_partial_prefetch_keeps_every_source(fetch_environment, observations):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    task = recovery_task(_handle_fetch_url, 'read_only')
    with unavailable_source() as (url, requests):
        args = {'urls': [{'url': url}] + [{'url': url + '?ok'}] * 12}
        accumulator, assistant = streamed_call(task, args)
        try:
            assert accumulator.inject_into_cache(task) == 1
            row, content, settled = dispatch_stream(task, accumulator, assistant)
        finally:
            accumulator.close(wait=True)
    assert len(requests) == len(row['results']) == 13
    assert content.count('recovered-source') == 12
    assert settled[0].execution_status == 'ok' and row['_cacheableResult'] is False


def test_correlation_id_repair_preserves_the_same_prefetched_occurrence(fetch_environment, observations):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    task = recovery_task(_handle_fetch_url, 'read_only')
    task['toolRounds'] = [{'toolCallId': 'streamed-fetch', 'toolName': 'fetch_url',
                          'roundNum': 1, 'llmRound': 0, 'status': 'done'}]
    with unavailable_source() as (url, requests):
        accumulator, assistant = streamed_call(task, {'url': url}, number=1)
        try:
            accumulator.inject_into_cache(task)
            row, content, _ = dispatch_stream(task, accumulator, assistant, number=1)
        finally:
            accumulator.close(wait=True)
    assert row['toolCallId'] != 'streamed-fetch'
    assert requests == ['/source.json']
    assert tool_result_error(content).code == 'tool_http_unavailable'


@pytest.mark.parametrize('duplicate_id', [False, True])
def test_equal_arguments_do_not_replay_failure_into_another_occurrence(duplicate_id, fetch_environment, observations):
    from copy import deepcopy
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    task = recovery_task(_handle_fetch_url, 'read_only')
    with unavailable_source() as (url, requests):
        accumulator, assistant = streamed_call(task, {'url': url})
        try:
            accumulator.inject_into_cache(task)
            second = deepcopy(assistant['tool_calls'][0])
            if not duplicate_id:
                second['id'] = 'another-occurrence'
            assistant['tool_calls'].append(second)
            _, _, settled = dispatch_stream(task, accumulator, assistant)
        finally:
            accumulator.close(wait=True)
    assert requests == ['/source.json', '/source.json']
    assert len({row['toolCallId'] for row in task['toolRounds']}) == 2
    assert [value.execution_status for value in settled] == ['error', 'error']


def test_real_root_loop_continues_after_failed_prefetch(tmp_path, monkeypatch, chat_sidecar, fetch_environment):
    from importlib import import_module
    from tests.llm import test_llm_replay_scenarios as replay
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from lib.llm_dispatch import reset_dispatcher

    calls = []
    with unavailable_source(recover=True) as (url, requests):
        def stream(body, *, on_tool_call_ready=None, on_content=None, **_kwargs):
            index = len(calls)
            calls.append(body)
            usage = {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}
            if index:
                content = [m['content'] for m in body['messages'] if m.get('role') == 'tool'][-1]
                if index == 1:
                    assert tool_result_error(content).code == 'tool_http_unavailable'
                    assert requests == ['/source.json'] * 3
                elif index == 2:
                    assert 'recovered-source' in content
                    assert requests == ['/source.json'] * 4
                    message = {'role': 'assistant', 'content': 'Read recovered successfully.'}
                    if on_content:
                        on_content(message['content'])
                    return ProviderStreamResult(message=message, compatibility_finish_reason='stop',
                        provider_finish_reason='stop', saw_done=True, saw_finish_reason=True,
                        usage=usage, state=ProviderStreamState.PROVIDER_FINISHED)
                else:
                    pytest.fail('The bounded recovery scenario made an unexpected provider request')
            call = {'id': f'live-fetch-{index}', 'type': 'function',
                    'function': {'name': 'fetch_url', 'arguments': json.dumps({'url': url})}}
            assert on_tool_call_ready is not None
            on_tool_call_ready(call)
            return ProviderStreamResult(message={'role': 'assistant', 'content': '', 'tool_calls': [call]},
                compatibility_finish_reason='tool_calls', provider_finish_reason='tool_calls',
                saw_done=True, saw_finish_reason=True, usage=usage, state=ProviderStreamState.PROVIDER_FINISHED)

        def forbidden(*_args, **_kwargs):
            pytest.fail('Unexpected provider transport in the bounded recovery scenario')

        def install(*_args, **_kwargs):
            monkeypatch.setattr('lib.llm.stream_chat', stream)
            monkeypatch.setattr('lib.llm.astream.async_stream_chat', forbidden)
            monkeypatch.setattr(import_module('lib.llm.chat'), 'chat', forbidden)
            monkeypatch.setattr('lib.llm.chat', forbidden)
            return 'controlled-provider-transport'

        original_create = replay._make_task
        def create(*args, **kwargs):
            task = original_create(*args, **kwargs)
            task['config']['webSearchEnabled'] = True
            return task
        monkeypatch.setattr(replay, '_make_task', create)
        monkeypatch.setattr(replay, '_install_transport', install)
        try:
            run = replay._run_loop_scenario('streamed-fetch-recovery', tmp_path=tmp_path,
                monkeypatch=monkeypatch, user_prompt='Read the source and recover from a temporary outage.',
                record_fixture=False)
            task = run['task']
            assert len(calls) == 3 and requests == ['/source.json'] * 4
            assert task['finishReason'] == 'stop' and not task.get('error')
            assert [row['status'] for row in task['toolRounds']] == ['error', 'done']
        finally:
            reset_dispatcher()
