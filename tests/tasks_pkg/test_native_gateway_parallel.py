"""Qualified native tool callbacks overlap through the canonical pipeline."""
import asyncio
import json
import threading

import pytest

from lib.agent_core.native_process import ProgressLimits, ProgressRequest, RoundRequest, RoundSettlement, ToolRequest
from lib.observability import InstrumentedThreadPoolExecutor
from lib.tasks_pkg.executor._finalize import _finalize_tool_round
from lib.tasks_pkg.native_gateway import NativeGateway
from tests.tasks_pkg.test_native_gateway import principal, task
from types import SimpleNamespace

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('count', [2, 8])
def test_independent_reads_overlap_and_keep_every_settled_result(mode, count):
    owner = task()
    owner['config']['tools']['resultEnvelope'] = mode
    definitions = [{'type': 'function', 'function': {'name': 'fetch_url',
        'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'}}, 'required': ['url']}}}]
    owner['_executable_tool_catalog'] = definitions
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, peak = 0, 0
    completed = []
    def handler(task, tc, name, call_id, args, rn, row, *_rest):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
            if active == count:
                entered.set()
        try:
            assert release.wait(5), 'test did not release its owned read'
            result = f'observation:{args["url"]}'
            _finalize_tool_round(task, rn, row, [{'content': result, 'badge': 'done'}])
            completed.append(call_id)
            return call_id, result, False
        finally:
            with lock:
                active -= 1
    owner['_tool_env'] = SimpleNamespace(resolve=lambda name: handler if name == 'fetch_url' else None)
    async def run(executor):
        gateway = NativeGateway(principal=principal(), task=owner, executor=executor,
            configuration=owner['config'], tool_list=definitions, permissions={'fetch_url': 'read'},
            project_path=None, project_enabled=False, model='fixture', round_source=None,
            decision_policy=None, pending_calls=16, pending_bytes=65536)
        progress = gateway.enable_progress(ProgressLimits(2, 2), 8192)
        calls = [{'id': f'read-{index}', 'type': 'function', 'function': {
            'name': 'fetch_url', 'arguments': json.dumps({'url': f'https://example.test/{index}'})}}
            for index in range(count)]
        gateway.prepare_response(RoundSettlement(RoundRequest(principal(), owner['id'], 'round', 0, 0),
            {'kind': 'returned', 'model_calls': calls}))
        work = [asyncio.create_task(gateway.execute(ToolRequest(principal(), owner['id'], f'request-{index}',
            'fetch_url', {'url': f'https://example.test/{index}'}, model_round=0))) for index in range(count)]
        try:
            overlapped = await asyncio.to_thread(entered.wait, 1)
        finally:
            release.set()
            results = await asyncio.gather(*work)
            observation = await progress.observe(ProgressRequest(principal(), owner['id'], 0, count, threading.Event()))
            gateway.close()
        assert overlapped and peak == count
        assert all(f'observation:https://example.test/{index}' in result for index, result in enumerate(results))
        assert observation['resultEvidenceComplete']
        assert len(observation['evidenceIds']) == count
        assert not observation['nonretryableFailureSignatures']
        assert progress.gateway is None and progress.batch is None
    # The worker hint is idle reuse; it cannot cap accepted independent reads.
    with InstrumentedThreadPoolExecutor(max_workers=1, thread_name_prefix='gateway-parallel-test',
            metric_pool='tool') as executor:
        asyncio.run(run(executor))
    assert active == 0 and len(completed) == len(set(completed)) == count
    assert len(owner['toolRounds']) == count and all(row['status'] == 'done' for row in owner['toolRounds'])
    assert not owner.get('_activeToolContexts')
    assert '_pending_tool_result_envelopes' not in owner


def test_parallel_qualification_reuses_write_and_ordered_state_policy(monkeypatch):
    from lib.tasks_pkg.tool_dispatch.api import parallel_tool_names
    owner = task()
    owner['_tool_env'] = SimpleNamespace(write_names={'custom_write'})
    names = {'fetch_url', 'todo_write', 'write_file', 'custom_write'}
    assert parallel_tool_names(owner, names) == {'fetch_url'}


def test_identical_read_occurrences_bind_original_identity_in_reverse_arrival_order():
    owner = task()
    definitions = [{'type': 'function', 'function': {'name': 'fetch_url', 'parameters': {'type': 'object'}}}]
    with InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='tool') as executor:
        gateway = NativeGateway(principal=principal(), task=owner, executor=executor,
            configuration=owner['config'], tool_list=definitions, permissions={'fetch_url': 'read'},
            project_path=None, project_enabled=False, model='fixture', round_source=None,
            decision_policy=None, pending_calls=16, pending_bytes=65536)
        calls = [{'id': f'original-{i}', 'type': 'function', 'function': {'name': 'fetch_url', 'arguments': '{}'}} for i in range(2)]
        gateway.prepare_response(RoundSettlement(RoundRequest(principal(), owner['id'], 'round', 0, 0),
            {'kind': 'returned', 'model_calls': calls}))
        def request(identity):
            return ToolRequest(principal(), owner['id'], 'request', 'fetch_url', {}, model_round=0,
                model_call_id=identity)
        with pytest.raises(ValueError, match='occurrence'):
            gateway._parse_occurrence(request('forged'), 0)
        for identity in ['original-1', 'original-0']:
            parsed, assistant = gateway._parse_occurrence(request(identity), 0)
            assert parsed[0][2] == assistant['tool_calls'][0]['id'] == identity
        gateway.close()


def test_live_write_reclassification_refuses_previously_qualified_callback_before_effect():
    owner = task()
    environment = SimpleNamespace(write_names=set(), idempotent_names=set())
    owner['_tool_env'] = environment
    definitions = [{'type': 'function', 'function': {'name': 'custom_read', 'parameters': {'type': 'object'}}}]
    with InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='tool') as executor:
        gateway = NativeGateway(principal=principal(), task=owner, executor=executor,
            configuration=owner['config'], tool_list=definitions, permissions={'custom_read': 'read'},
            project_path=None, project_enabled=False, model='fixture', round_source=None,
            decision_policy=None, pending_calls=16, pending_bytes=65536)
        environment.write_names.add('custom_read')
        with pytest.raises(ValueError, match='classification changed'):
            asyncio.run(gateway.execute(ToolRequest(principal(), owner['id'], 'request', 'custom_read', {}, model_round=0)))
        gateway.close()
    assert not owner['toolRounds']


def test_approval_request_uses_its_exact_call_id_with_prepared_siblings():
    from lib.agent_core.native_process import ApprovalRequest
    from tests.tasks_pkg.test_native_gateway import catalog
    owner = task()
    arguments = {'path': 'file.txt', 'content': 'approved body'}
    with InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='tool') as executor:
        gateway = NativeGateway(principal=principal(), task=owner, executor=executor,
            configuration=owner['config'], tool_list=catalog(), permissions={'write_file': 'write'},
            project_path=None, project_enabled=True, model='fixture', round_source=None,
            decision_policy=None, pending_calls=16, pending_bytes=65536)
        calls = [{'id': f'original-{i}', 'type': 'function', 'function': {
            'name': 'write_file', 'arguments': json.dumps(arguments)}} for i in range(2)]
        gateway.prepare_response(RoundSettlement(RoundRequest(principal(), owner['id'], 'round', 0, 0),
            {'kind': 'returned', 'model_calls': calls}))
        for identity in ['original-1', 'original-0']:
            request = ApprovalRequest(principal(), owner['id'], 'approval', identity, 'write_file', 'epoch',
                json.dumps(arguments).encode(), b'null', asyncio.Event(), 1)
            parsed, assistant = gateway._parse_occurrence(request, 0)
            assert parsed[0][2] == assistant['tool_calls'][0]['id'] == identity
        gateway.close()


@pytest.mark.parametrize('qualification', [None, [], ['todo_write'], ['fetch_url', 'fetch_url']])
def test_application_derives_qualification_and_binds_original_response_declarations(monkeypatch, qualification):
    from pathlib import Path
    from lib.agent_core.abort_signal import AbortSignal
    from lib.tasks_pkg.native_execution import run_native_task
    from lib.tasks_pkg.native_gateway import compile_native_tool_catalog
    owner = task()
    owner['abort_event'] = AbortSignal()
    definitions = [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}
        for name in ['fetch_url', 'todo_write']]
    client = SimpleNamespace(principal=principal(), budget=SimpleNamespace(frame_bytes=65536))
    observed = []
    async def native(client, route, configuration, **ports):
        observed.append(configuration)
        assert configuration['parallel_tool_names'] == (['fetch_url'] if qualification is None else qualification)
        if configuration['parallel_tool_names']:
            assert configuration['tool_sibling_control'] is True
            assert ports['round_policy'].response_policy.__self__ is gateway
    monkeypatch.setattr('lib.tasks_pkg.native_execution.run_native_route', native)
    with InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='tool') as executor:
        gateway = NativeGateway(principal=principal(), task=owner, executor=executor,
            configuration=owner['config'], tool_list=definitions, permissions={'fetch_url': 'read', 'todo_write': 'read'},
            project_path=None, project_enabled=False, model='fixture', round_source=None,
            decision_policy=None, pending_calls=16, pending_bytes=65536)
        settings = dict(owner_user_id=1, task_id=owner['id'], tools=compile_native_tool_catalog(owner, definitions),
            terminal_persistence=dict(operation_id='commit', attempts=1, ack_ms=1000, frame_bytes=65536))
        if qualification is not None: settings['parallel_tool_names'] = qualification
        async def run():
            await run_native_task(client, object(), settings, task=owner, executor=executor,
                callbacks={}, working_directory=Path('/tmp'), gateway=gateway,
                round_policy=SimpleNamespace(prepare=lambda request: []))
        if qualification and qualification != ['fetch_url']:
            with pytest.raises(ValueError, match='parallel tools'): asyncio.run(run())
            assert not observed
        else:
            asyncio.run(run())
            assert len(observed) == 1
        gateway.close()


def test_parallel_image_fallback_cannot_overdraw_shared_turn_budget(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    url = 'data:image/png;base64,AAAA'
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    class DelayedCounter:
        def __int__(self):
            entered.set()
            assert release.wait(3)
            return 0
        def __bool__(self): return True
    owner = {'_userId': 1, '_mcpMediaLock': threading.Lock(), '_inlineModelImageBytes': DelayedCounter()}
    monkeypatch.setattr(pipeline, '_INLINE_MODEL_IMAGES_PER_TURN_MAX_BYTES', len(url))
    monkeypatch.setattr(pipeline, '_screenshot_display_content', lambda *args: ('screen', False))
    monkeypatch.setattr(pipeline, '_append_screenshot_message', lambda target, *args:
        target.append({'content': [{'type': 'image_url', 'image_url': {'url': url}}]}))
    monkeypatch.setattr('lib.tasks_pkg.mcp_result_media.capture_mcp_result_images', lambda *args, **kwargs: 0)
    rows = [{}, {}]
    def project(i):
        if i: second.set()
        pipeline._prepare_screenshot_replay(owner, rows[i], {}, 'fixture', 'read', str(i))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(project, 0)
        try:
            assert entered.wait(2)
            other = pool.submit(project, 1)
            assert second.wait(2)
        finally:
            release.set()
        first.result(); other.result()
    retained = sum(block['type'] == 'image_url' for row in rows for block in row['toolModelContent'])
    assert retained == 1 and owner['_inlineModelImageBytes'] == len(url)
