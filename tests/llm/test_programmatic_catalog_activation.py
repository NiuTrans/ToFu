"""Local PTC exposure follows the task catalog; hosted review stays separate."""
import pytest

from lib.llm._sse_core import prepare_request
from lib.tasks_pkg.tool_orchestration_policy import resolve_tool_orchestration
from lib.tools.programmatic import local_programmatic_tool_names, resolve_programmatic_backend

pytestmark = pytest.mark.unit


def tool(name):
    return {'type': 'function', 'function': {'name': name,
            'parameters': {'type': 'object', 'properties': {},
                           'additionalProperties': False}}}


@pytest.mark.parametrize('name', ['custom__read_rows', 'mcp__docs__read', 'todo_write'])
@pytest.mark.parametrize('mode', ['on', 'auto'])
def test_task_functions_need_no_hosted_review(name, mode):
    decision = resolve_tool_orchestration(
        requested_programmatic=mode, requested_multi_agent='off',
        messages=[{'role': 'user', 'content': 'filter and aggregate all rows'}],
        tools=[tool(name)], round_num=1)
    assert decision['programmaticCalling'] == mode
    assert decision['programmaticEligibleTools'] == []
    assert decision['programmaticTier'] == ('program' if mode == 'on' else 'batch')


@pytest.mark.parametrize('catalog', [[], [tool('execute_tools'), tool('execute_program'), tool('search_tools')],
                                    [{'type': 'web_search'}], [None]])
def test_empty_or_gateway_only_catalog_stays_off(catalog):
    decision = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=[], tools=catalog, round_num=1)
    assert decision['programmaticCalling'] == 'off'
    assert local_programmatic_tool_names(catalog) == set()


@pytest.mark.parametrize('protocol,base_url,model', [
    ('openai', 'https://gateway.example/v1', 'local'),
    ('responses', 'https://api.openai.com/v1', 'gpt-5.6'),
])
def test_custom_only_catalog_projects_local_gateway_on_every_provider(protocol, base_url, model):
    catalog = [tool('custom__read_rows')]
    sink = {}
    plan = prepare_request({
        'model': model, 'messages': [{'role': 'user', 'content': 'inspect rows'}],
        'tools': catalog, '_executable_tool_catalog': catalog,
        '_tool_wire_catalog': catalog, '_tool_search_mode': 'off',
        '_programmatic_tool_calling': 'on', '_programmatic_tier': 'program',
        '_programmatic_eligible_tools': [],
        '_tool_orchestration_decision_sink': sink,
    }, api_key='test-secret', base_url=base_url, api_protocol=protocol)
    assert sink['programmaticBackend'] == 'local'
    names = [(t.get('function') or t).get('name') for t in plan.body['tools']]
    assert names == ['custom__read_rows', 'execute_program']
    assert not any(t.get('type') == 'programmatic_tool_calling' for t in plan.body['tools'])
    gateway = next(t for t in plan.body['tools'] if (t.get('function') or t).get('name') == 'execute_program')
    assert 'code' in (gateway.get('function') or gateway)['parameters']['properties']


def test_local_presence_does_not_promote_hosted_review():
    assert resolve_programmatic_backend('on', protocol='responses',
        model='gpt-5.6', base_url='https://api.openai.com/v1',
        eligible_present=False, local_present=True) == 'local'
    assert resolve_programmatic_backend('off', eligible_present=False,
        local_present=True) == 'off'
    assert resolve_programmatic_backend('on', eligible_present=False,
        local_present=False) == 'off'


@pytest.mark.parametrize('names', [['custom__read_rows'], ['read_files', 'grep_search']])
def test_catalog_size_does_not_estimate_saved_model_rounds(names):
    decision = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=[], tools=[tool(n) for n in names], round_num=1,
        policy_version='v2')
    assert decision['expectedSavings'] == {'basis': 'program_surface_unmeasured'}
