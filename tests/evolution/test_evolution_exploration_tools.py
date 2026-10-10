"""Experimental frontier authority, falsifiable proposals and durable recall."""
import json

import pytest

from tests.evolution.test_evolution_programs import runtime as runtime, create, launch, finish, command, step, OWNER

pytestmark = pytest.mark.unit


def candidate():
    return {**step(title='Prototype local early-warning memory'), 'hypothesis': {
        'problem': 'Repeated handoffs lose the cause of prior failed experiments.',
        'audience': 'A researcher resuming interrupted exploration.',
        'baseline': 'Manual reading of the entire journal.',
        'scenario': 'Warn about a previously falsified mechanism before another costly run.',
        'mechanism': 'Retrieve counterexamples and compare their constraints to the new proposal.',
        'expected_value': 'Avoid repeated invalid experiments while retaining novel alternatives.',
        'falsifier': 'No reduction in repeated invalid proposals relative to journal-only baseline.',
        'assumptions': ['Counterexamples transfer between sufficiently similar constraints.'],
        'evidence_refs': ['experiment:1:result; inspected failed handoff reproduction'],
    }}


def queued_candidate():
    from lib.evolution.exploration_tools import HYPOTHESIS_CHECKS
    proposed = candidate()
    proposed['checks'] += HYPOTHESIS_CHECKS
    return proposed


def bound_task(runtime, program, *, tid='experiment-task'):
    programs = runtime[0]
    run_id = program['current']['run_id']
    run = programs.runs.get_run(run_id, user_id=OWNER)
    run['task_id'] = tid
    programs.records.put_record(programs.records.NAMESPACE_RUNS,
        programs.records.entity_key(run_id, OWNER, kind='run'), run, kind='run')
    return {'id': tid, '_userId': OWNER,
            'config': {'evolution': {'goal_id': 'goal', 'run_id': run_id, 'stage': 'executing'}}}


def call(task, program, action, **args):
    from lib.evolution.exploration_tools import execute
    return execute(task, {'program_id': program['program_id'], 'action': action, **args},
                   call_id='stable-call')


def test_proposal_extends_authorized_frontier_once_without_claiming_value(runtime):
    programs = runtime[0]
    program = launch(runtime, create(runtime))
    task = bound_task(runtime, program)
    first = call(task, program, 'propose', step=candidate())
    assert first == call(task, program, 'propose', step=candidate())
    updated = programs.get_program(program['program_id'], user_id=OWNER)
    assert updated['objective'] == program['objective'] and updated['acceptance'] == program['acceptance']
    assert updated['current'] == program['current'] and updated['settled_through'] == 0
    assert updated['queue'][-2] == queued_candidate() and updated['queue'][-1]['kind'] == 'final'
    assert 'unverified' in first['evidence_status']
    assert len(runtime[2]) == 1
    changed = candidate(); changed['hypothesis']['mechanism'] = 'Another mechanism'
    with pytest.raises(programs.EvolutionStateError, match='another expansion'):
        call(task, program, 'propose', step=changed)
    settled = finish(runtime, program)
    intent = command(runtime, settled, 'advance')
    seed = json.loads(intent['current']['context_seed'])
    assert seed['current_hypothesis'] == candidate()['hypothesis']
    assert intent['current']['step']['hypothesis'] == candidate()['hypothesis']


def test_recall_finds_old_negative_results_and_reads_every_character(runtime):
    programs = runtime[0]
    program = launch(runtime, create(runtime))
    first_result = 'Early counterexample: bandwidth bottleneck. ' + 'negative result ' * 700
    program = finish(runtime, program, result=first_result)
    # Keep the first receipt outside the ordinary six-experiment context.
    for index in range(8):
        programs.append_steps(program['program_id'], {'steps': [step(title=f'Control {index}')]},
                              user_id=OWNER, idempotency_key=f'expand-{index}')
        command(runtime, program, 'advance')
        program = command(runtime, program, 'advance')
        program = finish(runtime, program, result='Another observed control result')
    command(runtime, program, 'advance')
    program = command(runtime, program, 'advance')
    task = bound_task(runtime, program)
    assert json.loads(program['current']['context_seed'])['older_experiments_omitted'] > 0
    hits = call(task, program, 'recall', query='bandwidth bottleneck')
    assert [item['sequence'] for item in hits['experiments']] == [1]
    assert hits['next_cursor'] is None
    chunks = []; offset = 0; digest = None
    while True:
        page = call(task, program, 'read', sequence=1, offset=offset, **({'source_digest':digest} if digest else {}))
        digest=page['source_digest']
        assert len(page['content']) <= 6000
        chunks.append(page['content'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert json.loads(''.join(chunks))['result'] == first_result
    assert programs.get_program(program['program_id'], user_id=OWNER)['settled_through'] == 9


def test_recall_scan_budget_and_hit_cursor_are_explicit(runtime, monkeypatch):
    programs = runtime[0]
    program = launch(runtime, create(runtime))
    task = bound_task(runtime, program)
    # Install immutable receipts and a settled cursor as a synthetic long lifetime.
    current, version = programs._read(program['program_id'], OWNER)
    current['settled_through'] = 123
    programs._save(current, version, OWNER)
    for sequence in range(1, 124):
        receipt = {'program_id':program['program_id'],'sequence': sequence, 'step': step(), 'result': 'needle' if sequence == 111 else 'ordinary',
                   'review': None, 'error': '', 'outcome': 'completed', 'run_id': f'run-{sequence}'}
        programs.records.put_record(programs.EXPERIMENTS,
            programs._experiment_key(program['program_id'], sequence, OWNER), receipt, kind='experiment')
    reads = []; original = programs.records.get_record
    def observe(namespace, key):
        if namespace == programs.EXPERIMENTS: reads.append(key)
        return original(namespace, key)
    monkeypatch.setattr(programs.records, 'get_record', observe)
    cursor = 0; results = []
    for expected in (50, 100, 123):
        reads.clear()
        page = call(task, program, 'recall', query='needle', after=cursor)
        assert len(reads) <= 50 and page['scanned_through'] == expected
        results.extend(page['experiments']); cursor = page['next_cursor']
    assert [item['sequence'] for item in results] == [111] and cursor is None
    page = call(task, program, 'recall')
    assert len(page['experiments']) == 8 and page['next_cursor'] == 8
    next_page = call(task, program, 'recall', after=8)
    assert next_page['experiments'][0]['sequence'] == 9


@pytest.mark.parametrize('mutation', ['foreign_owner', 'stale_task', 'reviewer', 'other_program', 'cancelled'])
def test_task_and_program_fences_reject_reads_and_proposals(runtime, mutation):
    from lib.evolution.contracts import EvolutionStateError
    program = launch(runtime, create(runtime)); task = bound_task(runtime, program)
    if mutation == 'foreign_owner': task['_userId'] += 1
    if mutation == 'stale_task': task['id'] = 'stale-task'
    if mutation == 'reviewer': task['config']['evolution']['stage'] = 'reviewing'
    if mutation == 'other_program': program = create(runtime, request_id='other')
    if mutation == 'cancelled': command(runtime, program, 'cancel')
    for action, args in [('recall', {}), ('propose', {'step': candidate()})]:
        with pytest.raises(EvolutionStateError): call(task, program, action, **args)


def test_pause_allows_evidence_reads_but_fences_frontier_writes_and_races(runtime, monkeypatch):
    programs = runtime[0]
    from lib.evolution import exploration_tools
    program = launch(runtime, create(runtime)); task = bound_task(runtime, program)
    command(runtime, program, 'pause')
    assert call(task, program, 'recall')['experiments'] == []
    with pytest.raises(programs.EvolutionStateError, match='paused'):
        call(task, program, 'propose', step=candidate())
    command(runtime, program, 'resume')
    original = exploration_tools._bound_program
    def pause_after_authorization(*args):
        value = original(*args); command(runtime, program, 'pause'); return value
    monkeypatch.setattr(exploration_tools, '_bound_program', pause_after_authorization)
    with pytest.raises(programs.EvolutionStateError, match='authority changed'):
        call(task, program, 'propose', step=candidate())
    assert len(programs.get_program(program['program_id'], user_id=OWNER)['queue']) == 2


@pytest.mark.parametrize('args', [
    {'action': 'read'}, {'action': 'read', 'sequence': 1},
    {'action': 'recall', 'after': True}, {'action': 'recall', 'query': 'x' * 241},
    {'action': 'propose', 'step': step()}, {'action': 'propose', 'step': step('final')},
])
def test_malformed_model_requests_are_rejected(runtime, args):
    from lib.evolution.contracts import EvolutionContractError
    program = launch(runtime, create(runtime)); task = bound_task(runtime, program)
    with pytest.raises(EvolutionContractError): call(task, program, **args)


def test_tool_schema_survives_wire_sanitization_and_stage_exposure(runtime):
    from lib.evolution.exploration_tools import tool_definition
    from lib.tools.gateway import sanitize_wire_tools
    from tests.evolution.test_evolution_tool_registration import _ctx, _assembled_names
    for model in ('', 'kimi-k3'):
        isolated = []
        wire = sanitize_wire_tools([tool_definition()], model=model, on_tool_isolated=isolated.append)
        assert len(wire) == 1 and isolated == []
    assert 'evolution_explore' in _assembled_names(_ctx({'evolution': {
        'goal_id': 'g', 'run_id': 'r', 'stage': 'executing'}}))
    for cfg in ({}, {'evolution': {'goal_id': 'g', 'run_id': 'r', 'stage': 'reviewing'}},
                {'evolution': {'goal_id': 'g', 'run_id': 'r', 'stage': 'coordinator'}}):
        assert 'evolution_explore' not in _assembled_names(_ctx(cfg))


def test_receipt_reads_reject_foreign_digest_and_missing_sequence(runtime,monkeypatch):
    from lib.evolution import exploration_tools
    from lib.evolution.contracts import EvolutionStateError
    programs=runtime[0];program=finish(runtime,launch(runtime,create(runtime)),result='Failure evidence '+ 'x'*9000)
    command(runtime,program,'advance');program=command(runtime,program,'advance')
    task=bound_task(runtime,program)
    first=call(task,program,'read',sequence=1)
    with pytest.raises(EvolutionStateError,match='identity changed'):
        call(task,program,'read',sequence=1,offset=first['next_offset'],source_digest='f'*64)
    assert call(task,program,'read',sequence=1,offset=first['next_offset'],source_digest=first['source_digest'])['source_digest']==first['source_digest']
    original=programs.experiment_page
    def missing(*args,**kwargs):
        result=original(*args,**kwargs)
        result['experiments']=[]
        return result
    monkeypatch.setattr(programs,'experiment_page',missing)
    with pytest.raises(EvolutionStateError,match='receipt is unavailable'):
        call(task,program,'read',sequence=1)
    def different(*args,**kwargs):
        result=original(*args,**kwargs)
        result['experiments'][0]['sequence']=2
        return result
    monkeypatch.setattr(programs,'experiment_page',different)
    with pytest.raises(EvolutionStateError,match='receipt is unavailable'):
        call(task,program,'read',sequence=1)
