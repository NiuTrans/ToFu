"""Chat controls reuse programme durability and genuine human authority."""
from copy import deepcopy
import pytest

from tests.evolution.test_evolution_programs import runtime as _runtime, OWNER
from tests.evolution.test_evolution_runs import runtime as _run_runtime

runtime = _runtime
run_runtime = _run_runtime
pytestmark = pytest.mark.unit


@pytest.fixture
def chat(monkeypatch):
    from lib.identity import PrincipalContext
    from lib.evolution import chat_programs
    import lib.turn_lifecycle as turns
    task = {'id': 'chat-task', 'convId': 'conversation-a', '_userId': OWNER,
        '_turnId': 'assistant-turn', '_attemptId': 'attempt', '_attended': True,
        '_principalContext': PrincipalContext.user(subject_id='human', owner_user_id=OWNER).to_payload(),
        'status': 'running', 'config': {}}
    human = {'conversationId': 'conversation-a', 'turnId': 'human-turn', 'actor': 'human',
             'projection': {'role': 'user', 'content': 'Proceed with the agreed exploration'}}
    assistant = {'conversationId': 'conversation-a', 'turnId': 'assistant-turn',
                 'actor': 'assistant', 'parentTurnId': 'human-turn', 'currentAttemptId': 'attempt'}
    attempt = {'attemptId': 'attempt', 'taskId': 'chat-task', 'turnId': 'assistant-turn',
               'conversationId': 'conversation-a', 'status': 'running'}
    def get_turn(cid, tid, *, user_id):
        if cid != task['convId'] or user_id != OWNER:
            return None
        return deepcopy(human if tid == 'human-turn' else assistant if tid == 'assistant-turn' else None)
    monkeypatch.setattr(turns, 'get_turn', get_turn)
    monkeypatch.setattr(turns, 'get_attempt', lambda aid, **kw: deepcopy(attempt) if aid == 'attempt' else None)
    monkeypatch.setattr('lib.evolution.runs.get_run_snapshot', lambda rid, **kw: {'run_id': rid, 'status': 'executing'})
    return chat_programs, task, human, attempt


def create(chat, **extra):
    module, task, _, _ = chat
    return module.execute(task, 'manage_exploration', {'action': 'create', 'idempotency_key': 'request',
        'request': {'objective': 'Synthetic mechanism and robustness exploration'}, **extra})


def test_chat_creation_and_retry_use_the_same_notebook_and_one_initial_activation(runtime, chat):
    programs, _, launched = runtime
    saved = create(chat)
    assert saved['status'] == 'ready' and not launched
    assert saved['detail_path'].endswith(saved['program_id'])
    assert programs.get_program(saved['program_id'], user_id=OWNER)['chat_source'] == {
        'conversation_id': 'conversation-a', 'turn_id': 'human-turn'}
    started = create(chat, start=True)
    assert started['execution_started'] and len(launched) == 1
    assert create(chat, start=True)['program_id'] == saved['program_id']
    assert len(launched) == 1
    module, task, _, _ = chat
    observed = module.execute(task, 'get_exploration', {'program_id': saved['program_id']})
    assert observed['current_experiment']['status'] == 'executing' and len(launched) == 1


def test_control_version_cannot_replay_resume_over_a_later_pause(runtime, chat):
    module, task, _, _ = chat
    created = create(chat)
    command = {'action': 'command', 'program_id': created['program_id'],
               'expected_version': created['state_version'], 'command': {'action': 'pause'}}
    paused = module.execute(task, 'manage_exploration', command)
    assert paused['status'] == 'paused'
    with pytest.raises(RuntimeError, match='changed'):
        module.execute(task, 'manage_exploration', command)
    assert runtime[0].get_program(created['program_id'], user_id=OWNER)['status'] == 'paused'
    assert not runtime[2]


def test_legacy_activation_is_unknown_and_creation_replay_cannot_resume_a_pause(runtime, chat):
    programs, _, launched = runtime
    module, task, _, _ = chat
    started = create(chat, start=True)
    paused = module.execute(task, 'manage_exploration', {'action': 'command',
        'program_id': started['program_id'], 'expected_version': started['state_version'], 'command': {'action': 'pause'}})
    assert paused['status'] == 'paused' and len(launched) == 1
    assert create(chat, start=True)['status'] == 'paused' and len(launched) == 1
    legacy = programs.get_program(started['program_id'], user_id=OWNER)
    legacy.pop('started_at')
    programs.records.put_record(programs.PROGRAMS,
        programs.records.entity_key(legacy['program_id'], OWNER, kind='program'), legacy, kind='program')
    observed = module.execute(task, 'get_exploration', {'program_id': legacy['program_id']})
    assert observed['execution_started'] is None
    assert observed['current_experiment']['run_id'] == started['current_experiment']['run_id']
    assert len(launched) == 1


def test_superseded_live_attempt_cannot_grant_exploration(runtime, chat, monkeypatch):
    import lib.turn_lifecycle as turns
    original = turns.get_turn
    def changed_turn(*args, **kwargs):
        turn = original(*args, **kwargs)
        if turn and turn['actor'] == 'assistant':
            turn['currentAttemptId'] = 'later-attempt'
        return turn
    monkeypatch.setattr(turns, 'get_turn', changed_turn)
    with pytest.raises(PermissionError, match='stale'):
        create(chat, start=True)
    assert not runtime[2] and runtime[0].list_programs(user_id=OWNER) == []


@pytest.mark.parametrize('marker,value', [('_initiator', 'timer'), ('_timer', True),
    ('_peerMessage', True), ('_isVirtualUser', True), ('origin', {'initiator': 'peer'})])
def test_synthetic_chat_cannot_inherit_human_execution_rights(runtime, chat, marker, value):
    chat[2]['projection'][marker] = value
    with pytest.raises((PermissionError, ValueError)):
        create(chat, start=True)
    assert not runtime[2] and runtime[0].list_programs(user_id=OWNER) == []


@pytest.mark.parametrize('change', ['foreign_owner', 'stale_attempt', 'cancelled', 'child', 'headless', 'restricted'])
def test_no_creation_from_foreign_stale_or_restricted_carriers(runtime, chat, change):
    _, task, _, attempt = chat
    if change == 'foreign_owner':
        task['_userId'] += 1
    elif change == 'stale_attempt':
        attempt['taskId'] = 'foreign-task'
    elif change == 'cancelled':
        task['aborted'] = True
    elif change == 'child':
        task['_parentTaskId'] = 'parent'
    elif change == 'headless':
        task['config']['evolution'] = {'stage': 'executing'}
    else:
        task['config']['planMode'] = True
    with pytest.raises((PermissionError, ValueError)):
        create(chat, start=True)
    assert not runtime[2] and runtime[0].list_programs(user_id=OWNER) == []


def test_chat_project_skills_model_and_explicit_budget_reach_existing_policy(runtime, chat, tmp_path, monkeypatch):
    from lib.skills.activation import bind_skill_selection
    from tests.skills.test_skill_activation import _package
    _package(tmp_path, '.tofu/skills', 'synthetic-check', 'Inspect the real evidence.')
    model = {'creator_id': 'test', 'model_id': 'chosen'}
    task = chat[1]
    task['config'].update(projectEnabled=True, projectPath=str(tmp_path), modelRef=model,
                          executionLimits={'max_tokens': None, 'max_tool_calls': None, 'deadline': None}, plugins=[])
    task['_selectedSkillsSnapshot'] = {'receipts': [{'skill_id': 'synthetic-check', 'status': 'loaded', 'trigger': 'explicit'}]}
    saved = create(chat, request={'objective': 'Synthetic work', 'budget': {'max_tokens': 100, 'max_tool_calls': 5}})
    options = runtime[0].get_program(saved['program_id'], user_id=OWNER)['execution_options']
    assert options['project_path'] == str(tmp_path) and options['model'] == model
    assert options['budget'] == {'max_tokens': 100, 'max_tool_calls': 5}
    assert options['executor_plugins'] == []
    assert options['skill_bindings'] == bind_skill_selection(['synthetic-check'], project_path=str(tmp_path), owner_user_id=OWNER)


@pytest.mark.parametrize('field,value', [('max_tokens', 100), ('max_tokens', 0),
    ('max_tool_calls', 3), ('deadline', 4102444800)])
def test_finite_chat_allowance_cannot_reset_as_an_independent_program_budget(runtime, chat, field, value):
    chat[1]['config']['executionLimits'] = {'max_tokens': None, 'max_tool_calls': None, 'deadline': None, field: value}
    with pytest.raises(PermissionError, match='allowance'):
        create(chat, start=True)
    assert not runtime[2] and runtime[0].list_programs(user_id=OWNER) == []


def test_chat_can_find_existing_plans_across_conversations_without_starting_work(runtime, chat):
    module, task, _, _ = chat
    saved = create(chat)
    another = runtime[0].create_program({'objective': 'Another owned plan'}, user_id=OWNER, idempotency_key='another-conversation')
    runtime[0].create_program({'objective': 'Foreign plan'}, user_id=OWNER+1, idempotency_key='foreign-owner')
    before = runtime[1].writes
    first = module.execute(task, 'get_exploration', {'view': 'catalog', 'limit': 1})
    second = module.execute(task, 'get_exploration', {'view': 'catalog', 'limit': 1, 'cursor': first['next_cursor']})
    assert {row['program_id'] for page in (first, second) for row in page['programs']} == {saved['program_id'], another['program_id']}
    assert second['next_cursor'] is None and runtime[1].writes == before and not runtime[2]


def test_tool_schemas_are_resolved_and_runtime_rejects_mixed_actions(runtime, chat):
    import json
    from lib.tools.exploration import build_exploration_tools
    tools = build_exploration_tools()
    assert {t['function']['name'] for t in tools} == {'get_exploration', 'manage_exploration'}
    assert '$ref' not in json.dumps(tools)
    with pytest.raises(ValueError):
        chat[0].execute(chat[1], 'manage_exploration', {'action': 'create', 'idempotency_key': 'mixed',
            'request': {'objective': 'Synthetic work'}, 'program_id': 'program-' + 'a' * 32})
    assert not runtime[2]


def test_full_chat_tool_schema_is_discoverable_through_the_shared_gateway():
    from lib.tools.exploration import build_exploration_tools
    from lib.tools.gateway import search_executable_catalog
    result = search_executable_catalog(build_exploration_tools(), 'manage_exploration')
    assert result['status'] == 'ok', result
    from jsonschema import Draft202012Validator
    from lib.evolution.contracts import validate_program
    validator = Draft202012Validator(result['items'][0]['arguments_schema'])
    step = {'kind': 'final', 'title': 'Audit the original requirement',
            'instruction': 'Read the retained original evidence', 'checks': ['Observed checks are required']}
    for args in ({'action': 'create', 'idempotency_key': 'create', 'request': {'objective': 'Synthetic plan', 'steps': [step]}},
                 {'action': 'append', 'program_id': 'program-'+'a'*32, 'idempotency_key': 'append', 'steps': [step]}):
        validate_program(args, 'chat_write')
        validator.validate(args)


def test_chat_restrictions_survive_executor_launch_without_changing_reviewer(run_runtime, chat):
    runs, _, tasks = run_runtime
    from lib.evolution import programs
    task = chat[1]
    task['config'].update(searchMode='off', fetchEnabled=False, codeExecEnabled=False,
                          toolSpecDenylist=['desktop'])
    saved = create(chat)
    policy = programs.get_program(saved['program_id'], user_id=OWNER)['execution_options']
    run = runs.submit_run({'objective': saved['objective'], **policy}, user_id=OWNER,
                          idempotency_key='bounded-chat-executor')
    cfg = tasks[0]['config']
    assert cfg['searchMode'] == 'off' and not cfg['fetchEnabled'] and not cfg['codeExecEnabled']
    assert cfg['toolSpecDenylist'] == ['desktop']
    reviewer = runs._task_config(run, 'reviewing')
    assert reviewer['toolSpecAllowlist'] == [] and not reviewer.get('toolSpecDenylist')


def test_guidance_is_bound_to_the_observed_experiment_and_preserves_delivery_truth(runtime, chat, monkeypatch):
    module, task, _, _ = chat
    saved = create(chat, start=True)
    calls = []
    monkeypatch.setattr('lib.evolution.coordination.publish', lambda rid, payload, **kw:
        calls.append((rid, payload, kw)) or {'seq': 3})
    args = {'action': 'guide', 'program_id': saved['program_id'], 'expected_run_id': 'stale',
            'idempotency_key': 'guidance', 'content': 'Preserve the failed baseline result'}
    with pytest.raises(RuntimeError, match='changed'):
        module.execute(task, 'manage_exploration', args)
    assert not calls
    args['expected_run_id'] = saved['current_experiment']['run_id']
    result = module.execute(task, 'manage_exploration', args)
    assert calls[0][0] == args['expected_run_id'] and calls[0][1]['kind'] == 'guidance'
    assert result['guidance']['status'] == 'recorded'
    assert 'Pending' in result['guidance']['acceptance']


def test_chat_tool_visibility_and_shared_dispatch_are_registered():
    from tests.tools.test_tool_registry import _ctx
    from lib.tools.registry import all_specs
    spec = next(spec for spec in all_specs() if spec.key == 'chat_exploration')
    assert spec.write_tools == {'manage_exploration'} and spec.cacheable_tools == set()
    assert spec.build(_ctx()) == []
    assert len(spec.build(_ctx(conv_id='owned', owner_user_id=OWNER))) == 2
    assert spec.build(_ctx(conv_id='owned', owner_user_id=OWNER, cfg={'evolution': {'run_id': 'headless'}})) == []
    assert spec.handler is not None


def test_registered_handler_preserves_result_and_host_error_verdict(runtime, chat, monkeypatch):
    import json
    from lib.tools.registry import all_specs
    from lib.tools.result_value import capture_result
    from lib.tools.result_envelope import tool_result_error
    from lib.tasks_pkg.handlers import exploration
    monkeypatch.setattr(exploration, 'simple_call', lambda task, name, args, *rest, executor, **kw: executor(name, args))
    handler = next(spec.handler for spec in all_specs() if spec.key == 'chat_exploration')
    def invoke(args):
        return handler(chat[1], {}, 'manage_exploration', 'call', args, 1, {}, {}, None, False)
    args = {'action': 'create', 'idempotency_key': 'registered', 'request': {'objective': 'Synthetic dispatch'}}
    created = json.loads(invoke(args))
    assert created['status'] == 'ready' and not runtime[2]
    chat[1]['aborted'] = True
    refused = invoke(args)
    assert capture_result(refused).execution_status == 'error'
    assert tool_result_error(refused).retryable is False
    assert 'human Chat' in tool_result_error(refused).message
    assert len(runtime[0].list_programs(user_id=OWNER)) == 1


def test_lazy_handler_load_works_with_frozen_shared_registry():
    import importlib
    from lib.tasks_pkg.executor import tool_registry
    from lib.tasks_pkg.handlers import exploration
    snapshot = tool_registry.snapshot()
    try:
        tool_registry.freeze()
        importlib.reload(exploration)
        assert callable(tool_registry.lookup('manage_exploration'))
        assert tool_registry.frozen
    finally:
        tool_registry.restore(snapshot)
