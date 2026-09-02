"""Executable contract for the published, storage-free agent runtime."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


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
def test_provider_config_accepts_three_fields_and_redacts_secret():
    from tofu_agent import ProviderConfig

    provider = ProviderConfig.from_mapping({
        'endpoint': 'https://models.example/v1/',
        'api_key': 'sk-never-print-this',
        'model': 'model-a',
    })

    assert provider.base_url == 'https://models.example/v1'
    assert provider.model == 'model-a'
    assert 'sk-never-print-this' not in repr(provider)
    assert 'sk-never-print-this' not in str(provider.public_dict())
    assert provider.public_dict()['has_api_key'] is True

    header_secret = ProviderConfig.from_mapping({
        'endpoint': 'https://models.example/v1',
        'api_key': 'sk-hidden',
        'model': 'model-a',
        'extra_headers': {'X-Gateway-Secret': 'also-hidden'},
    })
    assert 'also-hidden' not in repr(header_secret)


@pytest.mark.unit
@pytest.mark.parametrize('field,value', [
    ('provider', 'not-an-object'),
    ('config', []),
    ('capabilities', []),
    ('tools', {}),
])
def test_public_request_rejects_invalid_shapes(field, value):
    from tofu_agent import AgentConfigurationError, AgentRequest

    arguments = {
        'messages': [{'role': 'user', 'content': 'hello'}],
    }
    request_field = 'custom_tools' if field == 'tools' else field
    arguments[request_field] = value
    with pytest.raises(AgentConfigurationError):
        AgentRequest(**arguments)


@pytest.mark.unit
def test_provider_env_precedence_and_openai_default():
    from tofu_agent import ProviderConfig

    provider = ProviderConfig.from_env({
        'TOFU_AGENT_PROVIDER_API_KEY': 'new-key',
        'TOFU_AGENT_PROVIDER_MODEL': 'new-model',
        'LLM_BASE_URL': 'https://legacy.example/v1',
        'LLM_API_KEY': 'legacy-key',
        'LLM_MODEL': 'legacy-model',
    })
    assert provider is not None
    assert provider.base_url == 'https://legacy.example/v1'
    assert provider.api_key == 'new-key'
    assert provider.model == 'new-model'

    openai = ProviderConfig.from_env({
        'TOFU_AGENT_PROVIDER_API_KEY': 'key',
        'TOFU_AGENT_PROVIDER_MODEL': 'gpt-example',
    })
    assert openai is not None
    assert openai.base_url == 'https://api.openai.com/v1'


@pytest.mark.unit
def test_runtime_submits_explicit_principal_as_transient(monkeypatch):
    """The public composition must never accidentally enter durable paths."""
    from lib.agent_core.admission import fire_terminal_callbacks
    import lib.tasks_pkg.manager as task_manager
    import lib.tasks_pkg.spawn as task_spawn
    from tofu_agent import AgentRuntime, ProviderConfig

    captured: dict = {}

    def fake_create(conversation_id, messages, config, **kwargs):
        captured.update({
            'conversation_id': conversation_id,
            'messages': messages,
            'config': config,
            'kwargs': kwargs,
        })
        return {
            'id': 'task-public-1',
            '_userId': kwargs['principal'].owner_user_id,
            'status': 'queued',
            'events': [],
            'toolRounds': [],
        }

    def fake_spawn(task):
        task.update({
            'status': 'done',
            'content': 'ready',
            'finishReason': 'stop',
            'usage': {'total_tokens': 3},
        })
        fire_terminal_callbacks(task['id'])

    monkeypatch.setattr(task_manager, 'create_task', fake_create)
    monkeypatch.setattr(task_spawn, 'spawn_task', fake_spawn)
    monkeypatch.setattr(
        ProviderConfig, 'from_env', classmethod(lambda cls, **_kw: None))

    runtime = AgentRuntime.local(default_model='model-a')
    try:
        result = runtime.run([{'role': 'user', 'content': 'hello'}])
    finally:
        runtime.close()

    assert result.ok is True
    assert result.content == 'ready'
    assert captured['kwargs']['transient'] is True
    assert captured['kwargs']['supersede'] is False
    assert captured['config']['_storageFreeRuntime'] is True
    assert captured['config']['memoryEnabled'] is False
    assert captured['config']['schedulerEnabled'] is False
    principal = captured['kwargs']['principal']
    assert principal.subject_id == 'local:developer'
    assert principal.owner_user_id is not None
    assert runtime.in_flight == 0


@pytest.mark.unit
def test_runtime_disposes_one_shot_provider(monkeypatch):
    from lib.agent_core.admission import fire_terminal_callbacks
    from lib.llm_dispatch import ephemeral
    import lib.tasks_pkg.manager as task_manager
    import lib.tasks_pkg.spawn as task_spawn
    from tofu_agent import AgentRuntime, ProviderConfig

    disposed: list[object] = []
    handle = SimpleNamespace(slot=SimpleNamespace(provider_id='ephemeral-1'))
    monkeypatch.setattr(ephemeral, 'mint_ephemeral_slot', lambda **_kw: handle)
    monkeypatch.setattr(
        ephemeral, 'dispose_ephemeral_slot', lambda value: disposed.append(value))

    def fake_create(_conversation_id, _messages, _config, **kwargs):
        return {
            'id': 'task-provider-1',
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
    provider = ProviderConfig(
        base_url='https://models.example/v1', api_key='secret', model='m')
    runtime = AgentRuntime.local(provider=provider)
    try:
        assert runtime.run([{'role': 'user', 'content': 'hi'}]).ok
    finally:
        runtime.close()
    assert disposed == [handle]


@pytest.mark.unit
def test_storage_free_runtime_omits_durable_tool_families():
    from lib.tools.registry._build import (
        _build_conv_ref,
        _build_knowledge,
        _build_memory,
        _build_project_brain,
        _build_project_brain_write,
        _build_scheduler,
        _build_search_settings,
    )
    from lib.tools.registry._spec import ToolContext

    context = ToolContext(
        cfg={
            '_storageFreeRuntime': True,
            'memoryEnabled': True,
        },
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
        _build_project_brain,
        _build_project_brain_write,
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
        monkeypatch):
    from lib.agent_core.admission import fire_terminal_callbacks
    import lib.tasks_pkg.manager as task_manager
    import lib.tasks_pkg.spawn as task_spawn
    from tofu_agent import AgentRuntime, ProviderConfig

    schema = {
        'type': 'function',
        'function': {
            'name': 'custom__run_command',
            'description': 'Run one command in the caller environment.',
            'parameters': {
                'type': 'object',
                'properties': {'command': {'type': 'string'}},
                'required': ['command'],
            },
        },
        'execution': {'mode': 'client'},
    }
    captured: dict = {}

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
    monkeypatch.setattr(
        ProviderConfig, 'from_env', classmethod(lambda cls, **_kw: None))

    runtime = AgentRuntime.local(default_model='model-a')
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
    assert 'execution' not in captured['config']['_explicitToolSchemas'][0]


@pytest.mark.unit
def test_execution_resolves_custom_call_with_its_owner(monkeypatch):
    from lib.identity import PrincipalContext
    from tofu_agent import AgentExecution, AgentRequest, AgentRuntime
    import lib.tools.tool_env as tool_env

    principal = PrincipalContext.user(
        subject_id='evaluation:owner', owner_user_id=73)
    runtime = AgentRuntime(
        principal=principal, default_model='model-a', max_inflight=1)
    task = {
        'id': 'task-client-call', '_userId': 73, 'status': 'running',
        'events': [], 'toolRounds': [],
    }
    request = AgentRequest(
        messages=[{'role': 'user', 'content': 'hello'}],
        model='model-a', request_id='run-client-call')
    execution = AgentExecution(
        runtime, task, request, model='model-a', public_provider_id='')
    runtime._executions[task['id']] = execution
    captured: dict = {}

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
        default_model='model-a',
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
            '_wire_private': 'must-not-be-projected-either',
            '_dispatch': {
                'key': 'private-key-name',
                'key_tail': 'cret',
                'provider_id': 'inline',
                'latency_ms': 12,
                'ttft_ms': 3,
                'queue_wait_ms': 1.5,
                'queue_wait_measurement': 'dispatcher_backpressure_only',
            },
        }}],
        '_contextTelemetryRounds': [{'round': 1, 'toolSchemaTokens': 42}],
        '_contextCompactionEvents': [],
        '_toolExposureTelemetry': {'exposedTools': 1},
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
        model='model-a', request_id='run-evidence')
    execution = AgentExecution(
        runtime, task, request, model='model-a', public_provider_id='inline')
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
    assert evidence['output']['charCount'] == len('verified answer')
    dispatch = evidence['apiRounds'][0]['usage']['_dispatch']
    assert dispatch == {
        'provider_id': 'inline', 'latency_ms': 12, 'ttft_ms': 3,
        'queue_wait_ms': 1.5,
        'queue_wait_measurement': 'dispatcher_backpressure_only',
    }
    rendered = repr(evidence)
    assert 'must-not-be-projected' not in rendered
    assert 'must not enter evaluation evidence' not in rendered
