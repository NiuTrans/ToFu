"""Executable contract for the published, storage-free agent runtime."""

from __future__ import annotations

import json
import threading
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.support.standalone_model_routing import (
    standalone_model_routing_envelope,
)


ROOT = Path(__file__).resolve().parents[2]


def _access(**kwargs):
    from tofu_agent import ModelRoutingConfig

    return ModelRoutingConfig.from_mapping(
        standalone_model_routing_envelope(**kwargs))


def _patch_route_slot(monkeypatch, *, disposed=None):
    import lib.model_routing.dispatch_adapter as adapter

    handle = SimpleNamespace(slot=SimpleNamespace())
    monkeypatch.setattr(
        adapter, 'mint_ephemeral_slot', lambda **_kwargs: handle)
    monkeypatch.setattr(
        adapter,
        'dispose_ephemeral_slot',
        lambda value: (
            disposed.append(value) if disposed is not None else None) or True,
    )
    return handle


def _exercise_public_runtime_cancellation(monkeypatch, action):
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.manager import cancellation
    from lib.tasks_pkg.manager import runtime as task_runtime
    from lib.tasks_pkg.tool_runtime import context_for_task, unregister_context
    from lib.tools import tool_env
    from tofu_agent import AgentExecution, AgentRequest, AgentRuntime

    registry = TaskRuntime('fixture-public-cancel')
    monkeypatch.setattr(task_runtime, 'chat_task_runtime', registry)
    monkeypatch.setattr(cancellation, 'chat_task_runtime', registry)
    monkeypatch.setattr(tool_env, '_CLIENT_ABORT_POLL_INTERVAL', 0.01)
    principal = PrincipalContext.user(subject_id='test:cancel', owner_user_id=73)
    runtime = AgentRuntime(principal=principal, max_inflight=1)
    task = registry.create(principal=principal)
    task.update(status='running', config={}, convId='', _transientRuntime=True)
    execution = AgentExecution(runtime, task, AgentRequest(
        messages=[{'role': 'user', 'content': 'cancel this operation'}]),
        model='fixture', public_provider_id='fixture')
    runtime._executions[task['id']] = execution
    runtime._active.add(task['id'])
    scoped = context_for_task(task, round_num=1, tool_call_id='cancel-scope',
                              tool_name='custom__run_command', round_entry={})
    resources_cancelled = []
    scoped.register_cancel_callback(lambda: resources_cancelled.append(True))
    registered = threading.Event()
    result = []

    def worker():
        try:
            result.append(tool_env.request_client_tool_result(
                'cancel-client', task=task, timeout=10, on_registered=registered.set))
        finally:
            task['status'] = 'aborted'
            execution._terminal.set()

    thread = threading.Thread(target=worker, daemon=True,
                              name=f'public-cancel-probe-{task["id"][:8]}')
    thread.start()
    try:
        assert registered.wait(1)
        if action == 'abort':
            assert execution.abort()
        else:
            runtime.close(timeout_s=0.5)
        thread.join(0.5)
        assert not thread.is_alive(), 'public Stop must release a client waiter, not just set an unread event'
        assert task['abort_event'].is_set() and task['aborted'] is True
        assert resources_cancelled == [True]
        assert result[0][1] and 'aborted' in result[0][0].lower()
        assert not execution.abort(), 'terminal outcome cannot be cancelled again'
    finally:
        task['aborted'] = True
        thread.join(1)
        unregister_context(scoped)
        registry.discard(task['id'])
        runtime.close(abort=False, timeout_s=0)


@pytest.mark.unit
@pytest.mark.parametrize('action', ['abort', 'close'])
def test_public_runtime_cancellation_stops_client_wait_and_scoped_resources(monkeypatch, action):
    _exercise_public_runtime_cancellation(monkeypatch, action)


@pytest.mark.unit
def test_repeated_runtime_close_does_not_accumulate_waiting_workers(monkeypatch):
    from lib.tools import tool_env
    baseline_threads = set(threading.enumerate())
    baseline_waiters = set(tool_env._client_results)
    # More lifecycles than asyncio's usual 32-worker default pool: historically
    # each failed trial leaked a live waiter and eventually starved the next.
    for _ in range(40):
        _exercise_public_runtime_cancellation(monkeypatch, 'close')
    assert set(tool_env._client_results) == baseline_waiters
    assert not [thread for thread in threading.enumerate()
                if thread not in baseline_threads and thread.is_alive()
                and thread.name.startswith('public-cancel-probe-')]


@pytest.mark.unit
def test_client_wait_also_observes_task_runtime_abort_event(monkeypatch):
    from lib.tools import tool_env
    monkeypatch.setattr(tool_env, '_CLIENT_ABORT_POLL_INTERVAL', 0.01)
    task = {'id': 'event-only-cancel', '_userId': 73, 'abort_event': threading.Event()}
    task['abort_event'].set()
    result = tool_env.request_client_tool_result('event-only', task=task, timeout=1)
    assert result[1] and 'aborted' in result[0].lower()


@pytest.mark.unit
@pytest.mark.parametrize('isolated_runtimes', [False, True])
def test_forty_native_agents_deliver_tools_without_starving_async_io(monkeypatch, isolated_runtimes):
    """Real client waiters, event observers and result waits must coexist."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    import uuid
    from lib.agent_core.admission import fire_terminal_callbacks
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.orchestrator import api
    from lib.tools import tool_env
    from tofu_agent import AgentRuntime

    _patch_route_slot(monkeypatch)
    tasks = []
    release_workers = threading.Event()
    baseline_waiters = set(tool_env._client_results)

    def create(_conversation, _messages, config, **kwargs):
        task = {'id': uuid.uuid4().hex, '_userId': kwargs['principal'].owner_user_id,
                'status': 'pending', 'events': [], 'config': config,
                '_transientRuntime': True, 'abort_event': threading.Event()}
        tasks.append(task)
        return task

    def run(task):
        try:
            assert release_workers.wait(5)
            def publish():
                task['events'].append({'seq': 1, 'type': 'custom_tool_call',
                                       'tool_call_id': task['id']})
                task['_transientEventNotifier']()
            content, error = tool_env.request_client_tool_result(
                task['id'], task=task, timeout=3, on_registered=publish)
            task.update(status='done', finishReason='stop' if not error else 'error',
                        content=content)
        finally:
            fire_terminal_callbacks(task['id'])

    monkeypatch.setattr(manager, 'create_task', create)
    monkeypatch.setattr(api, 'run_task', run)

    async def exercise():
        # Even a small control pool must remain available while all agents
        # and both types of asynchronous observers are waiting.
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=4, thread_name_prefix='test-control'))
        runtimes = [AgentRuntime.local(model_routing=_access(),
                                      max_inflight=1 if isolated_runtimes else 40)
                    for _ in range(40 if isolated_runtimes else 1)]
        runtime = runtimes[0]
        consumers = []
        try:
            executions = [runtimes[i if isolated_runtimes else 0].start(
                [{'role': 'user', 'content': 'tool'}]) for i in range(40)]
            async def consume(execution):
                async for event in execution.events_async(timeout_s=5):
                    if event['type'] == 'custom_tool_call':
                        assert execution.resolve_custom_tool_call(
                            event['tool_call_id'], 'tool-ok')
            consumers = [asyncio.create_task(consume(e)) for e in executions]
            results = [asyncio.create_task(e.result_async(timeout_s=5)) for e in executions]
            consumers.extend(results)
            await asyncio.sleep(0.05)
            await asyncio.wait_for(asyncio.to_thread(lambda: True), timeout=0.5)
            release_workers.set()
            await asyncio.wait_for(asyncio.gather(*consumers), timeout=6)
            assert all(r.result().ok and r.result().content == 'tool-ok' for r in results)
            assert all(r.in_flight == 0 for r in runtimes)
        finally:
            release_workers.set()
            for task in tasks:
                task['abort_event'].set()
            for consumer in consumers:
                consumer.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            for runtime in runtimes:
                runtime.close(abort=False, timeout_s=1)
    asyncio.run(exercise())
    assert set(tool_env._client_results) == baseline_waiters


@pytest.mark.unit
def test_runtime_default_capacity_uses_shared_hardware_budget(monkeypatch):
    import runtime_guards
    from tofu_agent import AgentRuntime
    monkeypatch.delenv('TOFU_AGENT_MAX_INFLIGHT', raising=False)
    monkeypatch.setenv('TOFU_AGENT_WORKERS', '60')
    monkeypatch.setattr(runtime_guards, 'probe_system_resources',
                        lambda *_args, **_kwargs: pytest.fail('explicit budget must not probe'))
    with AgentRuntime.local(model_routing=_access()) as runtime:
        assert runtime.capacity == 60


@pytest.mark.unit
@pytest.mark.parametrize('asynchronous', [False, True])
def test_observer_drains_committed_events_when_worker_finishes_during_yield(asynchronous):
    import asyncio
    from tofu_agent import AgentExecution, AgentRequest, AgentRuntime
    with AgentRuntime.local(model_routing=_access(), max_inflight=1) as runtime:
        task = {'id': 'observer-finish-race', 'status': 'running',
                'events': [{'seq': 1, 'type': 'delta', 'content': 'first'}]}
        execution = AgentExecution(runtime, task,
            AgentRequest(messages=[{'role': 'user', 'content': 'test'}]),
            model='fixture', public_provider_id='fixture')
        def finish():
            task['events'].extend([{'seq': 2, 'type': 'delta', 'content': 'last'},
                                   {'seq': 3, 'type': 'done', 'finishReason': 'stop'}])
            task.update(status='done', finishReason='stop')
            execution._notify()
        async def observe():
            iterator = execution.events_async(timeout_s=1)
            assert (await anext(iterator))['content'] == 'first'
            finish()
            return [event async for event in iterator]
        if asynchronous:
            remaining = asyncio.run(observe())
        else:
            iterator = execution.events(timeout_s=1)
            assert next(iterator)['content'] == 'first'
            finish()
            remaining = list(iterator)
        assert [e.get('content') for e in remaining if e['type'] == 'delta'] == ['last']
        assert remaining[-1]['seq'] == 3
        assert not execution._async_waiters


@pytest.mark.unit
def test_pure_conversation_contract_does_not_boot_storage_repository():
    completed = subprocess.run(
        [
            sys.executable,
            '-c',
            'import sys; '
            'import lib.conversation_sync.attempt_identity; '
            'assert "lib.conversation_sync.repository" not in sys.modules; '
            'assert "lib.storage" not in sys.modules',
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.unit
def test_model_routing_config_validates_full_envelope_and_redacts_secrets():
    from tofu_agent import AgentConfigurationError, ModelRoutingConfig

    access = _access(secret='sk-never-print-this')
    assert access.model == {
        'creator_id': 'test-creator', 'model_id': 'model-a'}
    assert access.model_id == 'model-a'
    assert 'sk-never-print-this' not in repr(access)
    assert 'sk-never-print-this' not in str(access.public_dict())
    assert access.public_dict()['credential_secret_hints'] == {
        'provider-a-secret': 'configured'}

    missing = standalone_model_routing_envelope()
    missing['credential_secrets'] = {}
    with pytest.raises(AgentConfigurationError, match='missing enabled'):
        ModelRoutingConfig.from_mapping(missing)
    with pytest.raises(AgentConfigurationError, match='structured object'):
        ModelRoutingConfig.from_mapping({
            **standalone_model_routing_envelope(),
            'model': 'model-a',
        })


@pytest.mark.unit
def test_model_routing_env_uses_one_exact_json_authority():
    from tofu_agent import AgentConfigurationError, ModelRoutingConfig

    envelope = standalone_model_routing_envelope()
    access = ModelRoutingConfig.from_env({
        'TOFU_AGENT_MODEL_ROUTING': json.dumps(envelope),
        'TOFU_AGENT_PROVIDER_API_KEY': 'legacy-must-not-be-read',
    })
    assert access is not None
    assert access.model_id == 'model-a'
    assert ModelRoutingConfig.from_env({
        'TOFU_AGENT_PROVIDER_API_KEY': 'legacy-only',
    }) is None
    with pytest.raises(AgentConfigurationError, match='valid JSON'):
        ModelRoutingConfig.from_env({'TOFU_AGENT_MODEL_ROUTING': '{bad'})


@pytest.mark.unit
@pytest.mark.parametrize('field,value', [
    ('model', 'plain-model'),
    ('routing', []),
    ('model_routing', 'not-an-object'),
    ('config', []),
    ('capabilities', []),
    ('custom_tools', {}),
])
def test_public_request_rejects_removed_or_invalid_shapes(field, value):
    from tofu_agent import AgentConfigurationError, AgentRequest

    arguments = {
        'messages': [{'role': 'user', 'content': 'hello'}],
        field: value,
    }
    with pytest.raises(AgentConfigurationError):
        AgentRequest(**arguments)


@pytest.mark.unit
def test_runtime_submits_explicit_principal_as_transient(
    monkeypatch,
):
    from lib.agent_core.admission import fire_terminal_callbacks
    import lib.tasks_pkg.manager as task_manager
    import lib.tasks_pkg.spawn as task_spawn
    from tofu_agent import AgentRuntime

    captured = {}
    _patch_route_slot(monkeypatch)

    def fake_create(conversation_id, messages, config, **kwargs):
        captured.update(
            conversation_id=conversation_id,
            messages=messages,
            config=config,
            kwargs=kwargs,
        )
        return {
            'id': 'task-public-1',
            '_userId': kwargs['principal'].owner_user_id,
            'status': 'queued',
            'events': [],
            'toolRounds': [],
        }

    def fake_spawn(task, *, executor=None):
        task.update(
            status='done',
            content='ready',
            finishReason='stop',
            usage={'total_tokens': 3},
        )
        fire_terminal_callbacks(task['id'])

    monkeypatch.setattr(task_manager, 'create_task', fake_create)
    monkeypatch.setattr(task_spawn, 'spawn_task', fake_spawn)

    runtime = AgentRuntime.local(model_routing=_access())
    try:
        result = runtime.run([{'role': 'user', 'content': 'hello'}])
    finally:
        runtime.close()

    assert result.ok is True
    assert result.content == 'ready'
    assert result.model == 'model-a'
    assert result.provider_id == 'provider-a'
    assert captured['kwargs']['transient'] is True
    assert captured['kwargs']['supersede'] is False
    assert captured['config']['_storageFreeRuntime'] is True
    assert captured['config']['memoryEnabled'] is False
    assert captured['config']['schedulerEnabled'] is False
    assert captured['kwargs']['principal'].subject_id == 'local:developer'
    assert runtime.in_flight == 0


@pytest.mark.unit
def test_runtime_disposes_request_route_exactly_once(monkeypatch):
    from lib.agent_core.admission import fire_terminal_callbacks
    import lib.tasks_pkg.manager as task_manager
    import lib.tasks_pkg.spawn as task_spawn
    from tofu_agent import AgentRuntime

    disposed = []
    handle = _patch_route_slot(monkeypatch, disposed=disposed)

    def fake_create(_conversation_id, _messages, _config, **kwargs):
        return {
            'id': 'task-route-1',
            '_userId': kwargs['principal'].owner_user_id,
            'status': 'queued',
            'events': [],
            'toolRounds': [],
        }

    def fake_spawn(task, *, executor=None):
        task.update(status='done', content='ok', finishReason='stop')
        fire_terminal_callbacks(task['id'])

    monkeypatch.setattr(task_manager, 'create_task', fake_create)
    monkeypatch.setattr(task_spawn, 'spawn_task', fake_spawn)
    runtime = AgentRuntime.local(model_routing=_access())
    try:
        result = runtime.run(
            [{'role': 'user', 'content': 'hello'}],
            model={'creator_id': 'test-creator', 'model_id': 'model-a'},
            routing={'preferred_provider_id': 'provider-a'},
        )
    finally:
        runtime.close()

    assert result.ok
    assert disposed == [handle]


@pytest.mark.unit
def test_storage_free_runtime_omits_durable_tool_families():
    from lib.tools.registry._build import (
        _build_conv_ref,
        _build_knowledge,
        _build_memory,
        _build_project_integration,
        _build_scheduler,
        _build_search_settings,
    )
    from lib.tools.registry._spec import ToolContext

    context = ToolContext(
        cfg={'_storageFreeRuntime': True, 'memoryEnabled': True},
        task_id='public-tools',
        project_path='/workspace',
        project_enabled=True,
        search_mode='multi',
        search_enabled=True,
        fetch_enabled=True,
        code_exec_enabled=True,
        browser_enabled=False,
        desktop_enabled=False,
    )
    context.current_count = 3
    context.has_base_tools = True
    for builder in (
        _build_conv_ref,
        _build_knowledge,
        _build_memory,
        _build_project_integration,
        _build_scheduler,
        _build_search_settings,
    ):
        assert builder(context) == []


@pytest.mark.unit
def test_custom_tool_mode_is_strict_and_exclusive_requires_a_catalog():
    from tofu_agent import AgentConfigurationError, AgentRequest

    base = {'messages': [{'role': 'user', 'content': 'hello'}]}
    with pytest.raises(AgentConfigurationError, match='augment.*exclusive'):
        AgentRequest(**base, custom_tools_mode='unknown')
    with pytest.raises(AgentConfigurationError, match='requires at least one'):
        AgentRequest(**base, custom_tools_mode='exclusive')


@pytest.mark.unit
def test_runtime_exclusive_custom_tools_use_exact_explicit_authority(
    monkeypatch,
):
    from lib.agent_core.admission import fire_terminal_callbacks
    import lib.tasks_pkg.manager as task_manager
    import lib.tasks_pkg.spawn as task_spawn
    from tofu_agent import AgentRuntime

    _patch_route_slot(monkeypatch)
    schema = {
        'type': 'function',
        'function': {
            'name': 'custom__run_command',
            'description': 'Run one command.',
            'parameters': {'type': 'object'},
        },
        'execution': {'mode': 'client'},
    }
    captured = {}

    def fake_create(_conversation_id, _messages, config, **kwargs):
        captured['config'] = config
        return {
            'id': 'task-exclusive-tools',
            '_userId': kwargs['principal'].owner_user_id,
            'status': 'queued',
            'events': [],
            'toolRounds': [],
            'config': config,
        }

    def fake_spawn(task, *, executor=None):
        task.update(status='done', content='ok', finishReason='stop')
        fire_terminal_callbacks(task['id'])

    monkeypatch.setattr(task_manager, 'create_task', fake_create)
    monkeypatch.setattr(task_spawn, 'spawn_task', fake_spawn)
    runtime = AgentRuntime.local(model_routing=_access())
    try:
        result = runtime.run(
            [{'role': 'user', 'content': 'change the project'}],
            custom_tools=[schema],
            custom_tools_mode='exclusive',
        )
    finally:
        runtime.close()

    clean = {'type': 'function', 'function': schema['function']}
    assert result.ok
    assert captured['config']['_explicitToolSchemas'] == [clean]
    assert captured['config']['_customToolSchemas'] == [clean]
    assert captured['config']['_customToolsMode'] == 'exclusive'


@pytest.mark.unit
def test_execution_resolves_custom_call_with_its_owner(monkeypatch):
    from lib.identity import PrincipalContext
    from tofu_agent import AgentExecution, AgentRequest, AgentRuntime
    import lib.tools.tool_env as tool_env

    runtime = AgentRuntime(
        principal=PrincipalContext.user(
            subject_id='evaluation:owner', owner_user_id=73),
        model_routing=_access(),
        max_inflight=1,
    )
    task = {
        'id': 'task-client-call',
        '_userId': 73,
        'status': 'running',
        'events': [],
        'toolRounds': [],
    }
    request = AgentRequest(
        messages=[{'role': 'user', 'content': 'hello'}],
        model={'creator_id': 'test-creator', 'model_id': 'model-a'},
        request_id='run-client-call',
    )
    execution = AgentExecution(
        runtime, task, request, model='model-a',
        public_provider_id='provider-a')
    runtime._executions[task['id']] = execution
    captured = {}

    def fake_resolve(call_id, content, **kwargs):
        captured.update(call_id=call_id, content=content, **kwargs)
        return True

    monkeypatch.setattr(tool_env, 'resolve_client_tool_result', fake_resolve)
    try:
        assert execution.resolve_custom_tool_call(
            'ctool_abc', 'command output', is_error=True)
    finally:
        runtime.close(abort=False)

    assert captured == {
        'call_id': 'ctool_abc',
        'content': 'command output',
        'task_id': 'task-client-call',
        'user_id': 73,
        'is_error': True,
    }


@pytest.mark.unit
def test_execution_evidence_snapshot_is_versioned_and_credential_free():
    from lib.identity import PrincipalContext
    from tofu_agent import AgentExecution, AgentRequest, AgentRuntime

    runtime = AgentRuntime(
        principal=PrincipalContext.user(
            subject_id='evaluation:owner', owner_user_id=19),
        model_routing=_access(secret='must-not-be-projected'),
    )
    clean_schema = {
        'type': 'function',
        'function': {
            'name': 'custom__run_command',
            'parameters': {'type': 'object'},
        },
    }
    task = {
        'id': 'task-evidence',
        '_userId': 19,
        '_requestId': 'run-evidence',
        'status': 'done',
        'finishReason': 'stop',
        'content': 'verified answer',
        'thinking': 'must not enter evaluation evidence',
        'created_at': 10.25,
        'finished_at': 12.5,
        'usage': {'prompt_tokens': 11, 'completion_tokens': 3},
        'apiRounds': [{'round': 1, 'usage': {
            'prompt_tokens': 11,
            '_dispatch': {
                'key': 'private-key-name',
                'provider_id': 'provider-a',
                'latency_ms': 12,
            },
        }}],
        'config': {
            'model': 'model-a',
            '_explicitToolSchemas': [clean_schema],
            '_customToolsMode': 'exclusive',
            'providerApiKey': 'must-not-be-projected',
        },
        'toolRounds': [],
        'events': [],
    }
    request = AgentRequest(
        messages=[{'role': 'user', 'content': 'hello'}],
        model={'creator_id': 'test-creator', 'model_id': 'model-a'},
        request_id='run-evidence',
    )
    execution = AgentExecution(
        runtime, task, request, model='model-a',
        public_provider_id='provider-a')
    try:
        evidence = execution.evidence_snapshot()
    finally:
        runtime.close(abort=False)

    assert evidence['contractVersion'] == 'tofu.agent-runtime-evidence/v1'
    assert evidence['customToolsMode'] == 'exclusive'
    assert evidence['toolSchemas'] == [clean_schema]
    assert evidence['createdAtUnixMs'] == 10250
    assert evidence['finishedAtUnixMs'] == 12500
    assert evidence['output']['content'] == 'verified answer'
    rendered = repr(evidence)
    assert 'must-not-be-projected' not in rendered
    assert 'must not enter evaluation evidence' not in rendered


@pytest.mark.unit
def test_explicit_tools_compile_request_owned_contracts():
    """Caller-supplied catalogs own their ToolContractV2 documents.

    The explicit-tools branch returns before registry assembly, where
    contract documents are normally compiled. Without compiling them here
    the executor fails closed on every caller-supplied call.
    """
    from lib.tasks_pkg.model_config import _assemble_tool_list
    from lib.tools.contracts import (
        ToolContractError,
        validate_tool_arguments_from_documents,
    )

    schema = {
        'type': 'function',
        'function': {
            'name': 'custom__run_command',
            'description': 'Run one command.',
            'parameters': {
                'type': 'object',
                'properties': {'command': {'type': 'string'}},
                'required': ['command'],
                'additionalProperties': False,
            },
        },
        'execution': {'mode': 'client'},
        'write': True,
    }
    cfg = {'_explicitToolSchemas': [schema]}
    tool_list, has_real_tools = _assemble_tool_list(
        cfg, '', False, 'task-contract-pin',
        'off', False, False, False, False, False,
        image_gen_enabled=False, human_guidance_enabled=False,
        scheduler_enabled=False, messages=[], conv_id='')

    assert has_real_tools
    assert tool_list == [schema]
    documents = cfg.get('_toolContractDocumentsByName')
    assert set(documents or ()) == {'custom__run_command'}
    document = documents['custom__run_command']
    assert document['contractVersion'] == 'tofu.tool-contract/v2'
    assert document['namespace'] == 'custom'
    assert document['permission'] == 'write'
    assert document['idempotency'] == 'non_idempotent'
    validated = validate_tool_arguments_from_documents(
        documents, 'custom__run_command', {'command': 'pwd'})
    assert validated == {'command': 'pwd'}
    with pytest.raises(ToolContractError):
        validate_tool_arguments_from_documents(
            documents, 'custom__run_command', {'cmd': 'pwd'})


@pytest.mark.unit
def test_explicit_tools_drop_uncontractable_schema():
    from lib.tasks_pkg.model_config import _assemble_tool_list

    bad = {
        'type': 'function',
        'function': {
            'name': 'custom__bad name!',
            'description': 'Uncontractable name.',
            'parameters': {'type': 'object'},
        },
    }
    good = {
        'type': 'function',
        'function': {
            'name': 'custom__ok',
            'description': 'Fine.',
            'parameters': {'type': 'object'},
        },
    }
    cfg = {'_explicitToolSchemas': [bad, good]}
    tool_list, has_real_tools = _assemble_tool_list(
        cfg, '', False, 'task-contract-drop',
        'off', False, False, False, False, False,
        image_gen_enabled=False, human_guidance_enabled=False,
        scheduler_enabled=False, messages=[], conv_id='')

    assert has_real_tools
    assert tool_list == [good]
    assert set(cfg.get('_toolContractDocumentsByName') or ()) == {'custom__ok'}
    assert cfg['_executableToolCatalog'] == [good]
