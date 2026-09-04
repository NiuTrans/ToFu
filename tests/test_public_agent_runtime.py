"""Executable contract for the published, storage-free agent runtime."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.support.standalone_model_routing import (
    standalone_model_routing_envelope,
)


ROOT = Path(__file__).resolve().parents[1]


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

    def fake_spawn(task):
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

    def fake_spawn(task):
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

    def fake_spawn(task):
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
