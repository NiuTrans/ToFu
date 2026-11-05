"""Structured, templated delegation: one shared prompt, schema-validated results.

Executable specification for ``lib/swarm/spawn_contracts.py`` and its wiring:
``spawn_agents`` admission, the worker's Result contract section, the bounded
corrective round, result canonicalization and parent delivery of violations.
"""
import json
import threading
import uuid

import pytest

from lib.swarm.spawn_contracts import (
    SpawnContractError, canonical_json, expand_agent_templates,
    output_contract_text, parse_structured_answer, validate_output_schema,
)
from lib.swarm.types import SubAgentResult, SubTaskSpec
from tests.goals import test_goal_invocation_integration as goal_fixture

runtime = goal_fixture.runtime
pytestmark = pytest.mark.unit
_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/a.py', 'lib/b.py'}

SCHEMA = {
    'type': 'object',
    'properties': {
        'verdict': {'type': 'string', 'enum': ['pass', 'fail']},
        'evidence': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': ['verdict', 'evidence'],
    'additionalProperties': False,
}
TEMPLATES = {'audit': {
    'objective': 'Audit one module for unsafe file writes.',
    'context': 'Repository root: /repo',
    'role': 'researcher',
    'tools': ['read_files'],
    'output_schema': SCHEMA,
}}
VALID = '{"verdict": "pass", "evidence": ["lib/a.py:3"]}'


# ── Template expansion ─────────────────────────────────────────────────

def test_same_shape_workers_share_the_template_prefix_and_differ_only_in_params():
    first, second = expand_agent_templates(TEMPLATES, [
        {'id': 'a', 'template': 'audit', 'params': {'module': 'lib/a.py', 'depth': 2}},
        {'id': 'b', 'template': 'audit', 'params': {'depth': 2, 'module': 'lib/b.py'}},
    ])
    prefix = 'Audit one module for unsafe file writes.\n\n## Parameters\n'
    assert first['objective'] == prefix + '- depth: 2\n- module: lib/a.py'
    assert second['objective'] == prefix + '- depth: 2\n- module: lib/b.py'
    for agent in (first, second):
        assert agent['context'] == 'Repository root: /repo'
        assert (agent['role'], agent['tools'], agent['output_schema']) == (
            'researcher', ['read_files'], SCHEMA)
        assert 'template' not in agent and 'params' not in agent
    assert (first['id'], second['id']) == ('a', 'b')
    assert first['output_schema'] is not TEMPLATES['audit']['output_schema']


def test_non_string_params_render_as_sorted_json_and_empty_params_add_no_block():
    agent, plain, empty = expand_agent_templates(TEMPLATES, [
        {'template': 'audit', 'params': {'scope': {'b': 1, 'a': '中'}, 'flags': [True, None]}},
        {'template': 'audit'},
        {'template': 'audit', 'params': {}},
    ])
    assert agent['objective'].endswith(
        '## Parameters\n- flags: [true, null]\n- scope: {"a": "中", "b": 1}')
    assert plain['objective'] == empty['objective'] == TEMPLATES['audit']['objective']


def test_agent_fields_override_the_template_and_agent_context_follows_it():
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}
    (agent,) = expand_agent_templates(TEMPLATES, [{
        'template': 'audit', 'params': {'module': 'x'}, 'role': 'coder',
        'tools': ['run_command'], 'output_schema': schema, 'context': 'Focus on writes.',
        'depends_on': ['other'], 'required_for_completion': False,
    }])
    assert (agent['role'], agent['tools'], agent['output_schema']) == ('coder', ['run_command'], schema)
    assert agent['context'] == 'Repository root: /repo\n\nFocus on writes.'
    assert agent['depends_on'] == ['other'] and agent['required_for_completion'] is False


def test_objectiveless_template_is_a_shared_preset_for_distinct_objectives():
    preset = {'audit': {'role': 'reviewer', 'context': 'Repository root: /repo',
                        'tools': ['read_files']}}
    first, second = expand_agent_templates(preset, [
        {'id': 'a', 'template': 'audit', 'objective': 'Audit storage writes.'},
        {'id': 'b', 'template': 'audit', 'objective': 'Audit swarm teardown.',
         'context': 'Start at lib/swarm.'},
    ])
    assert first == {'id': 'a', 'objective': 'Audit storage writes.',
                     'context': 'Repository root: /repo', 'role': 'reviewer',
                     'tools': ['read_files']}
    assert second['objective'] == 'Audit swarm teardown.'
    assert second['context'] == 'Repository root: /repo\n\nStart at lib/swarm.'


def test_agent_objective_follows_the_template_objective_and_precedes_params():
    (agent,) = expand_agent_templates(TEMPLATES, [{
        'template': 'audit', 'objective': 'Report only confirmed writes.',
        'params': {'module': 'lib/a.py'},
    }])
    assert agent['objective'] == (
        'Audit one module for unsafe file writes.\n\nReport only confirmed writes.'
        '\n\n## Parameters\n- module: lib/a.py')
    blank_shared = {'audit': {'objective': '  ', 'role': 'reviewer'}}
    (own,) = expand_agent_templates(blank_shared, [{'template': 'audit', 'objective': 'Own task.'}])
    assert own['objective'] == 'Own task.'


def test_untemplated_agents_pass_through_and_inputs_are_not_mutated():
    agents = [{'objective': 'Plain worker', 'role': 'general'}, 'not-an-object']
    original = json.loads(json.dumps(agents))
    assert expand_agent_templates(None, agents) == original
    templated = [{'template': 'audit', 'params': {'module': 'x'}}]
    expand_agent_templates(TEMPLATES, templated)
    assert templated == [{'template': 'audit', 'params': {'module': 'x'}}]


@pytest.mark.parametrize('templates, agents', [
    (['audit'], [{'objective': 'x'}]),
    ({'audit': 'objective'}, [{'objective': 'x'}]),
    ({'audit': {'objective': 'x', 'contxt': 'typo'}}, [{'objective': 'x'}]),
    ({'audit': {'context': 'no objective'}}, [{'template': 'audit'}]),
    ({'audit': {'objective': '   '}}, [{'template': 'audit', 'objective': ''}]),
    ({'audit': {'objective': 3}}, [{'objective': 'x'}]),
    (TEMPLATES, [{'template': 'audit', 'objective': ['not', 'text']}]),
    ({'audit': {'objective': 'x', 'tools': 'read_files'}}, [{'objective': 'x'}]),
    ({'audit': {'objective': 'x', 'output_schema': 'object'}}, [{'objective': 'x'}]),
    (TEMPLATES, [{'objective': 'x', 'params': {'module': 'a'}}]),
    (TEMPLATES, [{'template': 'missing'}]),
    (None, [{'template': 'audit'}]),
    (TEMPLATES, [{'template': 'audit', 'params': ['module', 'a']}]),
    (TEMPLATES, [{'template': 'audit', 'context': ['not', 'text']}]),
])
def test_every_template_rule_rejects_with_an_actionable_correction(templates, agents):
    with pytest.raises(SpawnContractError) as caught:
        expand_agent_templates(templates, agents)
    assert caught.value.code == 'invalid_spawn_contract'
    assert caught.value.message and caught.value.next_action


# ── Result schema admission ────────────────────────────────────────────

def test_local_object_schema_is_admitted():
    local = {'type': 'object', '$defs': {'path': {'type': 'string'}},
             'properties': {'file': {'$ref': '#/$defs/path'}}}
    assert validate_output_schema(SCHEMA) is SCHEMA
    assert validate_output_schema(local) is local


@pytest.mark.parametrize('schema, fragment', [
    ([], 'top-level type'),
    ({'type': 'array', 'items': {}}, 'top-level type'),
    ({'properties': {}}, 'top-level type'),
    ({'type': 'object', 'properties': {'a': {'$ref': 'https://example.com/a.json'}}}, 'local'),
    ({'type': 'object', 'properties': {'a': {'$dynamicRef': '#meta'}}}, 'local'),
    ({'type': 'object', 'properties': {'a': {'$id': 'urn:a', 'type': 'string'}}}, '$id'),
    ({'type': 'object', '$anchor': 'root'}, '$id'),
    ({'type': 'object', 'properties': {'a': {'type': 'strin'}}}, 'not a valid JSON Schema'),
    ({'type': 'object', 'required': 'verdict'}, 'not a valid JSON Schema'),
    ({'type': 'object', 'description': 'x' * 16384}, 'exceeds 16384'),
])
def test_unsafe_malformed_or_oversized_schemas_are_rejected(schema, fragment):
    with pytest.raises(SpawnContractError) as caught:
        validate_output_schema(schema)
    assert fragment in caught.value.message
    assert caught.value.code == 'invalid_spawn_contract' and caught.value.next_action


def test_contract_text_embeds_the_compact_schema():
    text = output_contract_text(SCHEMA)
    assert text.startswith('Finish with exactly one JSON object and nothing else')
    assert canonical_json(SCHEMA) in text
    assert 'store_artifact' in text


def test_canonical_json_is_compact_and_keeps_unicode_and_key_order():
    assert canonical_json({'b': 'é', 'a': [1, 2]}) == '{"b":"é","a":[1,2]}'


# ── Final-answer parsing ───────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    VALID,
    f'```json\n{VALID}\n```',
    f'The audit is complete.\n{VALID}\nThat is all.',
    f'Checked {{all}} modules.\n```json\n{VALID}\n```',
])
def test_raw_fenced_and_prose_wrapped_objects_parse(text):
    assert parse_structured_answer(text, SCHEMA) == (json.loads(VALID), '')


@pytest.mark.parametrize('text, expected', [
    ('{"verdict": "pass",', '/: invalid JSON'),
    ('The audit passed.', '/: invalid JSON'),
    ('[1, 2]', '/: the final answer must be a JSON object'),
    ('', '/: invalid JSON'),
    ('{"verdict": "pass", "evidence": [1]}', "/evidence/0: 1 is not of type 'string'"),
    ('{"verdict": "pass"}', "/: 'evidence' is a required property"),
])
def test_invalid_answers_report_a_json_pointer_error(text, expected):
    value, error = parse_structured_answer(text, SCHEMA)
    assert value is None
    assert error.startswith(expected)


def test_parse_error_is_bounded():
    value, error = parse_structured_answer(
        json.dumps({'verdict': 'x' * 2000, 'evidence': []}), SCHEMA)
    assert value is None and error.startswith('/verdict: ') and len(error) <= 300


# ── spawn_agents admission through the real handler ────────────────────

@pytest.fixture
def spawn(monkeypatch):
    from lib import agent_inbox
    from lib.swarm import integration

    task_id = 'tcontract-' + uuid.uuid4().hex[:8]
    captured = {}

    class Session:
        def __init__(self):
            self.specs, self.user_id, self.is_terminated = [], 1, False
            self.task_id, self.conv_id = task_id, ''
            self._parent_task_proxy = {'config': {}}

        def run_in_background(self):
            return None

    def master(**kw):
        captured['specs'] = list(kw['specs'])
        return Session()

    monkeypatch.setattr('lib.swarm.integration._tools.MasterOrchestrator', master)
    monkeypatch.setattr('lib.swarm.integration._tools._get_session', lambda key: None)
    monkeypatch.setattr('lib.swarm.integration._tools._set_session', lambda *a, **kw: None)
    monkeypatch.setattr('lib.swarm.persistence.save_session', lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(agent_inbox, 'untombstone', lambda *a, **kw: None)

    def call(arguments):
        return json.loads(integration.execute_swarm_tool(
            'spawn_agents', arguments, task={'id': task_id, '_userId': 1, 'status': 'running'}))
    try:
        yield call, captured
    finally:
        agent_inbox.reset_for_test(task_id)


def test_templated_wave_launches_distinct_specs_with_their_schema(spawn):
    call, captured = spawn
    handle = call({'templates': TEMPLATES, 'agents': [
        {'id': 'a', 'template': 'audit', 'params': {'module': 'lib/a.py'}},
        {'id': 'b', 'template': 'audit', 'params': {'module': 'lib/b.py'}},
        {'id': 'c', 'objective': 'Summarize the audit plan'},
    ]})
    assert handle['status'] == 'async_launched'
    first, second, plain = captured['specs']
    assert first.objective.endswith('- module: lib/a.py')
    assert second.objective.endswith('- module: lib/b.py')
    assert (first.role, first.tools, first.output_schema) == ('researcher', ['read_files'], SCHEMA)
    assert first.context == 'Repository root: /repo'
    assert (plain.objective, plain.output_schema) == ('Summarize the audit plan', None)
    assert SubTaskSpec.from_dict(first.to_dict()).output_schema == SCHEMA


def test_preset_template_with_per_agent_objectives_launches(spawn):
    call, captured = spawn
    handle = call({'templates': {'audit': {'role': 'reviewer', 'context': 'Repo: /repo'}},
                   'agents': [{'id': 'storage', 'template': 'audit', 'objective': 'Audit storage.'},
                              {'id': 'swarm', 'template': 'audit', 'objective': 'Audit swarm.'}]})
    assert handle['status'] == 'async_launched'
    storage, swarm = captured['specs']
    assert (storage.objective, swarm.objective) == ('Audit storage.', 'Audit swarm.')
    assert storage.role == swarm.role == 'reviewer'
    assert storage.context == swarm.context == 'Repo: /repo'


@pytest.mark.parametrize('arguments, code', [
    ({'templates': {'audit': {'role': 'reviewer'}}, 'agents': [{'template': 'audit'}]},
     'invalid_spawn_contract'),
    ({'agents': [{'template': 'audit'}]}, 'invalid_spawn_contract'),
    ({'agents': [{'objective': 'x', 'output_schema': {'type': 'array'}}]},
     'invalid_spawn_contract'),
    ({'agents': [{'objective': 'x', 'output_schema': {
        'type': 'object', 'properties': {'a': {'$ref': 'https://example.com/s'}}}}]},
     'invalid_spawn_contract'),
    ({'agents': [{'role': 'general'}]}, 'invalid_agent'),
    ({'agents': [{'objective': '   '}]}, 'invalid_agent'),
])
def test_contract_violations_are_retryable_typed_errors_before_launch(spawn, arguments, code):
    call, captured = spawn
    result = call(arguments)
    assert result['code'] == code
    assert result['retryable'] is True and result['next_action']
    assert 'specs' not in captured


# ── Worker prompt and loop gate ────────────────────────────────────────

def test_child_messages_carry_one_result_contract_section():
    from lib.tasks_pkg.child_invocation import build_child_messages
    contract = output_contract_text(SCHEMA)
    fresh = build_child_messages(objective='Audit lib/a.py', context='Repo: /repo',
                                 output_contract=contract)
    assert fresh[-1] == {'role': 'user', 'content': (
        'Audit lib/a.py\n\n## Context\nRepo: /repo\n\n## Result contract\n' + contract)}
    rebuilt = build_child_messages(objective='Audit lib/a.py', context='Repo: /repo',
                                   output_contract=contract, checkpoint_messages=fresh)
    assert [m['content'] for m in rebuilt].count(fresh[-1]['content']) == 1
    assert sum('## Result contract' in str(m['content']) for m in rebuilt) == 1
    assert build_child_messages(objective='Plain')[-1]['content'] == 'Plain'


@pytest.mark.parametrize('rebuild', [False, True])
def test_child_invocation_sets_a_detached_output_schema(rebuild):
    from lib.tasks_pkg.child_invocation import build_child_messages, create_child_invocation
    from lib.tasks_pkg.manager import discard_task
    checkpoint = (build_child_messages(objective='Audit', output_contract=output_contract_text(SCHEMA))
                  if rebuild else None)
    schema = json.loads(json.dumps(SCHEMA))
    child = create_child_invocation(
        {'id': 'contract-parent', 'convId': 'contract-conv', '_userId': 71,
         '_transientRuntime': True, 'config': {'memoryEnabled': False, 'searchMode': 'off'}},
        model='fixture-model', tools=[], objective='Audit', output_schema=schema,
        checkpoint_messages=checkpoint, event_sink=lambda event: None, abort_check=lambda: False)
    try:
        assert child['_invocationOutputSchema'] == SCHEMA
        assert child['_invocationOutputSchema'] is not schema
        sections = [m for m in child['messages'] if '## Result contract' in str(m.get('content'))]
        assert len(sections) == 1 and sections[0]['role'] == 'user'
    finally:
        discard_task(child['id'])


def _analyse(task, content, messages):
    from lib.tasks_pkg.stream_handler._analyse import analyse_stream_result
    return analyse_stream_result(
        assistant_msg={'content': content}, last_finish_reason='stop', task=task,
        tid='contract', model='fixture-model', round_num=4,
        _premature_retry_count=0, messages=messages, usage={})


def _worker(content, **extra):
    task = {'id': 'contract-worker', 'toolRounds': [], 'events': [],
            'events_lock': threading.Lock(), 'aborted': False,
            'content': content, 'thinking': '', '_invocationOutputSchema': SCHEMA}
    task.update(extra)
    return task


def test_invalid_structured_answer_gets_one_corrective_round():
    task = _worker('The audit passed.')
    messages = [{'role': 'user', 'content': 'Audit lib/a.py'}]
    decision = _analyse(task, 'The audit passed.', messages)
    assert decision['action'] == 'continue'
    assert [m['role'] for m in messages] == ['user', 'assistant', 'user']
    assert messages[1]['content'] == 'The audit passed.'
    correction = messages[-1]
    assert correction['_contextInjectionId'] == 'output_contract' and correction['_isMeta']
    assert '/: invalid JSON' in correction['content']
    assert 'ONLY the corrected JSON object' in correction['content']
    assert task['_output_contract_retry_count'] == 1
    # The corrected object alone becomes the deliverable; the vetoed answer
    # survives in the wire row and the continuation-prose record.
    assert task['content'] == ''
    assert task['_continuation_prose'][-1]['content'] == 'The audit passed.'


@pytest.mark.parametrize('content, extra', [
    ('still prose', {'_output_contract_retry_count': 1}),
    (VALID, {}),
    ('The audit passed.', {'_invocationOutputSchema': None}),
])
def test_valid_spent_or_unscheduled_answers_take_the_normal_exit(content, extra):
    task = _worker(content, **extra)
    messages = [{'role': 'user', 'content': 'Audit lib/a.py'}]
    decision = _analyse(task, content, messages)
    assert decision['action'] == 'break'
    assert decision['loop_exit_reason'] == 'no_tool_calls_round_4'
    assert len(messages) == 1 and task['content'] == content
    assert task.get('_output_contract_retry_count', 0) == extra.get('_output_contract_retry_count', 0)


# ── Worker settlement through the real child pipeline ─────────────────

def _structured_agent(runtime, monkeypatch, answers):
    from tests.swarm.test_swarm_invocation import agent_for, reply, scripted_provider
    scripted_provider(monkeypatch, runtime, [reply(answer) for answer in answers])
    spec = SubTaskSpec(role='general', objective='Audit lib/a.py', output_schema=SCHEMA)
    return agent_for(runtime, spec=spec)


def test_corrected_worker_answer_is_delivered_as_canonical_json(runtime, monkeypatch):
    agent = _structured_agent(runtime, monkeypatch, [
        'I checked it and the verdict is pass.',
        f'```json\n{VALID}\n```',
    ])
    result = agent.run()
    assert result.status == 'completed', result.error_message
    assert result.final_answer == '{"verdict":"pass","evidence":["lib/a.py:3"]}'
    assert result.contract_error == ''
    first, second = runtime.calls
    objective = [m['content'] for m in first['messages']
                 if m.get('role') == 'user' and 'Audit lib/a.py' in str(m.get('content'))]
    assert len(objective) == 1 and objective[0].count('## Result contract') == 1
    assert second['messages'][-1]['_contextInjectionId'] == 'output_contract'


def test_uncorrected_worker_completes_with_a_contract_error_and_raw_answer(runtime, monkeypatch):
    agent = _structured_agent(runtime, monkeypatch, ['First prose answer.', 'Second prose answer.'])
    result = agent.run()
    assert result.status == 'completed', result.error_message
    assert result.final_answer == 'Second prose answer.'
    assert result.contract_error.startswith('output_schema violation: /: invalid JSON')
    assert len(runtime.calls) == 2


# ── Parent delivery ────────────────────────────────────────────────────

@pytest.fixture
def master(monkeypatch):
    from lib import agent_inbox
    from lib.swarm.master import MasterOrchestrator
    key = 'contract-' + uuid.uuid4().hex
    spec = SubTaskSpec(id='child', role='researcher', objective='Audit', output_schema=SCHEMA)
    orchestrator = MasterOrchestrator(key, key, [spec], user_id=1)
    monkeypatch.setattr(orchestrator, '_persist_agent_snapshot', lambda **kw: None)
    yield orchestrator
    agent_inbox.drain(key)


def test_completed_contract_violation_reaches_the_parent_as_an_error(master):
    from lib import agent_inbox
    spec = master.specs[0]
    violation = 'output_schema violation: /: evidence is a required property'
    result = SubAgentResult(status='completed', final_answer='{"verdict":"pass"}',
                            contract_error=violation)
    master._on_agent_complete_callback(spec, result)
    (update,) = agent_inbox.drain(master.inbox_key)
    assert '<status>completed</status>' in update['value']
    assert f'<error>{violation}</error>' in update['value']
    master._reenqueue_swarm_update(spec, result)
    (again,) = agent_inbox.drain(master.inbox_key)
    assert f'<error>{violation}</error>' in again['value']
    assert master.await_agents(ids=['child'])['completed'][0]['contract_error'] == violation
    payload = master.get_agent_result('child')
    assert (payload['status'], payload['final_answer'], payload['contract_error']) == (
        'completed', '{"verdict":"pass"}', violation)
    restored = master._result_from_dict(result.to_dict())
    assert restored.contract_error == violation


def test_valid_structured_completion_has_no_error_element(master):
    from lib import agent_inbox
    master._on_agent_complete_callback(master.specs[0], SubAgentResult(
        status='completed', final_answer='{"verdict":"pass","evidence":[]}'))
    (update,) = agent_inbox.drain(master.inbox_key)
    assert '<error>' not in update['value']
    assert 'contract_error' not in master.get_agent_result('child')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
