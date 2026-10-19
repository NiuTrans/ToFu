"""Task admission, context and final routing agree on the local tool interface."""
from copy import deepcopy
import json

import pytest

from lib.llm._sse_core import prepare_request
from lib.llm.tool_wire import project_tool_wire
from lib.tasks_pkg.context_composer import ComposeRequest
from lib.tasks_pkg.context_composer._providers import _programmatic_guidance
from lib.tasks_pkg.orchestrator._tool_assembly_prep import attach_gateway_contract_documents
from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
from lib.tools.gateway import tool_schema_fingerprint

pytestmark = pytest.mark.unit


def tool(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {
        'type': 'object', 'properties': {}, 'additionalProperties': False}}}


def request(task, *, round_num=1):
    messages = [{'role': 'user', 'content': 'filter and aggregate all rows'}]
    catalog = [tool('custom__read_rows')]
    body = {'model': 'unverified-model', 'messages': messages, 'tools': catalog}
    attach_tool_request_metadata(task, body, messages, catalog,
                                 model=body['model'], round_num=round_num, tid='test')
    return body


def names(body):
    return {(t.get('function') or t).get('name') for t in body['tools']}


@pytest.fixture(autouse=True)
def no_environment_override(monkeypatch):
    monkeypatch.delenv('TOFU_PTC_TIER', raising=False)


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
@pytest.mark.parametrize('mode', [None, 'auto', 'on', 'off'])
def test_interfaces_agree_across_admission_context_and_wire(protocol, mode):
    settings = {'toolSearch': 'off'}
    if mode is not None:
        settings['programmaticCalling'] = mode
    task = {'config': {'tools': settings}, '_toolContractDocumentsByName': {}}
    assert attach_gateway_contract_documents(task, 'off')
    body = request(task)
    guide = _programmatic_guidance(ComposeRequest(
        user_id=7, task=task, has_real_tools=True,
        tool_names=frozenset({'custom__read_rows'})))
    plan = prepare_request(body, api_key='test-key',
                           base_url='https://gateway.example/v1', api_protocol=protocol)
    exposed = names(plan.body)
    assert 'custom__read_rows' in exposed
    assert ('execute_program' in exposed) == (mode == 'on')
    assert ('execute_program' in task['_tool_gateway_names']) == (mode == 'on')
    assert bool(guide) == (mode == 'on')
    if mode in (None, 'auto'):
        assert 'execute_tools' in exposed
        wire = json.dumps(plan.body['tools'])
        assert all(token not in wire for token in ('JavaScript', 'tools.inputs', 'tools.result'))
        batch = next(t for t in plan.body['tools']
                     if (t.get('function') or t).get('name') == 'execute_tools')
        fn = batch.get('function') or batch
        params = fn.get('parameters') or fn.get('input_schema')
        assert set(params['properties']) == {'calls', 'execution'}
    if mode == 'off':
        assert exposed == {'custom__read_rows'}


def test_stale_replay_latch_cannot_enable_programs_or_override_off():
    task = {'config': {'tools': {'programmaticCalling': 'on'}}}
    request(task)
    old = deepcopy(task['_toolOrchestrationPromptLatch'])
    old.pop('programmaticRequestedMode')  # A replay produced before this contract.
    for mode in ('auto', 'off'):
        current = {'config': {'tools': {'programmaticCalling': mode},
                              '_modelReplayToolLatch': old}}
        body = request(current)
        project_tool_wire(body, api_protocol='openai')
        assert 'execute_program' not in names(body)
        assert body['_programmatic_tool_calling'] == mode
        assert current['_toolOrchestrationPromptLatch']['programmaticRequestedMode'] == mode


def test_unchanged_policy_keeps_the_same_wire_after_failed_program_telemetry():
    task = {'config': {'tools': {'programmaticCalling': 'auto'}}}
    first = request(task)
    project_tool_wire(first, api_protocol='openai')
    task['_toolScriptBatchFallback'] = True
    later = request(task, round_num=9)
    project_tool_wire(later, api_protocol='openai')
    assert tool_schema_fingerprint(first['tools']) == tool_schema_fingerprint(later['tools'])
    assert later['_tool_orchestration_decision_sink']['programmaticTier'] == 'batch'


@pytest.mark.parametrize('feature', ['supported', 'unknown', 'unsupported'])
@pytest.mark.parametrize('force_local', [False, True])
def test_selected_route_support_and_fallback_do_not_promote_local_javascript(feature, force_local):
    body = {
        'model': 'route-alias', 'messages': [], 'tools': [tool('read_files')],
        '_programmatic_tool_calling': 'auto',
        '_programmatic_eligible_tools': ['read_files'],
        '_programmatic_tier': 'program',  # stale or untrusted capability hint
        '_force_local_programmatic': force_local,
    }
    project_tool_wire(body, api_protocol='responses', inference_profile={
        'features': {'responses_programmatic': feature}})
    native = feature == 'supported' and not force_local
    assert body['_resolved_programmatic_backend'] == ('native_openai' if native else 'local')
    assert 'execute_program' not in names(body)
    if not native:
        assert 'execute_tools' in names(body)
        assert body['_programmatic_tier'] == 'batch'


def test_batch_override_also_prevents_hosted_program_execution(monkeypatch):
    monkeypatch.setenv('TOFU_PTC_TIER', 'batch')
    body = {'model': 'route-alias', 'messages': [], 'tools': [tool('read_files')],
            '_programmatic_tool_calling': 'on', '_programmatic_eligible_tools': ['read_files']}
    project_tool_wire(body, api_protocol='responses', inference_profile={
        'features': {'responses_programmatic': 'supported'}})
    assert body['_resolved_programmatic_backend'] == 'local'
    assert names(body) == {'read_files', 'execute_tools'}


def test_auto_code_api_request_preserves_direct_tools_and_json_discovery():
    task = {'config': {'tools': {'programmaticExposure': 'code_api',
                                 'toolSearch': 'local', 'schemaBudgetTokens': 1}}}
    body = request(task)
    body['_required_direct_tool_names'] = ['custom__read_rows']
    project_tool_wire(body, api_protocol='openai')
    assert names(body) == {'custom__read_rows', 'search_tools', 'execute_tools'}
    search = next(t['function'] for t in body['tools'] if t['function']['name'] == 'search_tools')
    assert 'execute_tools' in search['description']
    assert body['_executable_tool_catalog'] == [tool('custom__read_rows')]
    sink = body['_tool_orchestration_decision_sink']
    assert sink['programmaticExposure'] == 'additive'
    assert sink['programmaticCodeApi']['reason'] == 'code_api_requires_program_tier'
