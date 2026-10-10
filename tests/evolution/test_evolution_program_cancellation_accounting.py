"""Cancelled exploration accounts consumed stages while draining terminal receipts.

Real run/task births and immutable outcomes; no provider or executor dispatch.
"""
from copy import deepcopy

import pytest

from tests.evolution.test_evolution_runs import runtime as runtime, finish, OWNER
from tests.evolution.test_evolution_programs import step

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_external_reindex(monkeypatch):
    monkeypatch.setattr('lib.evolution.maintenance.reindex_maintenance', lambda **kw: [])


def reviewing_program(runtime):
    from lib.evolution import programs
    p = programs.create_program({'objective': 'Explore a reproducible alternative.',
        'acceptance': ['Retain the evidence and its measured consumption.'],
        'steps': [step(), step('final', 'Check the original mission')]}, user_id=OWNER, idempotency_key='cancel-accounting')
    p = programs.command_program(p['program_id'], {'action': 'start'}, user_id=OWNER)
    run = finish(runtime, runtime[2][-1], 'Original executor result retained for independent review.')
    assert run['status'] == 'reviewing' and run['actuals']['tokens'] == 15
    return programs, p, runtime[2][-1]


def test_cancelled_program_accounts_execution_and_waits_for_review_terminal(runtime):
    programs, p, reviewer = reviewing_program(runtime)
    cancelled = programs.command_program(p['program_id'], {'action': 'cancel'}, user_id=OWNER)
    assert cancelled['status'] == 'cancelled'
    assert cancelled['actuals']['tokens'] == 15
    assert cancelled['actuals_complete'] is False
    assert cancelled['current'] and cancelled['settled_through'] == 0
    programs.sweep_programs(user_id=OWNER)
    registry = programs.records.get_record(programs.REGISTRY,
        programs.records.entity_key('programs', OWNER, kind='program registry'))
    assert p['program_id'] in registry['program_ids']
    assert len(runtime[2]) == 2
    finish(runtime, reviewer, '', status='aborted')
    settled = programs.advance_program(p['program_id'], user_id=OWNER)
    assert settled['status'] == 'cancelled' and settled['current'] is None
    assert settled['actuals']['tokens'] == 30
    assert settled.get('actuals_complete', True)
    assert settled['settled_through'] == 1
    receipt = programs.experiment_page(p['program_id'], user_id=OWNER)['experiments'][0]
    assert receipt['outcome'] == 'cancelled' and receipt['actuals']['tokens'] == 30
    assert receipt['review'] is None
    assert settled['queue'] == p['queue'] and settled['acceptance'] == p['acceptance']
    assert programs.command_program(p['program_id'], {'action': 'cancel'}, user_id=OWNER) == settled
    programs.sweep_programs(user_id=OWNER)
    registry = programs.records.get_record(programs.REGISTRY,
        programs.records.entity_key('programs', OWNER, kind='program registry'))
    assert p['program_id'] not in registry['program_ids']
    assert len(runtime[2]) == 2


def test_unknown_cancelled_review_usage_keeps_lower_bound_in_archive(runtime):
    programs, p, reviewer = reviewing_program(runtime)
    programs.command_program(p['program_id'], {'action': 'cancel'}, user_id=OWNER)
    reviewer.update(status='aborted', finishReason='aborted', content='',
                    finished_at=reviewer['created_at'] + 1,
                    usage={'input_tokens': 10, 'output_tokens': 0},
                    _executionActualsComplete=False)
    runtime[0].advance_run(p['current']['run_id'], user_id=OWNER, terminal_task=reviewer)
    final = programs.advance_program(p['program_id'], user_id=OWNER)
    assert final['current'] is None and final['actuals']['tokens'] == 25
    assert final['actuals_complete'] is False
    receipt = programs.experiment_page(p['program_id'], user_id=OWNER)['experiments'][0]
    assert receipt['actuals_complete'] is False and receipt['actuals']['tokens'] == 25
    assert len(runtime[2]) == 2


def test_proved_dead_cancelled_review_preserves_prior_cost_without_replay(runtime, monkeypatch):
    programs, p, reviewer = reviewing_program(runtime)
    programs.command_program(p['program_id'], {'action': 'cancel'}, user_id=OWNER)
    monkeypatch.setattr(programs.runs, 'default_task_loader', lambda *a, **kw: None)
    monkeypatch.setattr('lib.evolution.review_recovery.owner_state', lambda owner: 'dead')
    final = programs.advance_program(p['program_id'], user_id=OWNER)
    assert final['status'] == 'cancelled' and final['current'] is None
    assert final['actuals']['tokens'] == 15 and final['actuals_complete'] is False
    run = programs.runs.get_run(p['current']['run_id'], user_id=OWNER)
    assert run['stage_losses'][-1]['task_id'] == reviewer['id']
    assert len(runtime[2]) == 2


@pytest.mark.parametrize('loss', ['before_commit', 'after_commit'])
def test_cancelled_receipt_and_header_ack_loss_do_not_double_count(runtime, monkeypatch, loss):
    from lib.storage.errors import StorageError
    programs, p, reviewer = reviewing_program(runtime)
    programs.command_program(p['program_id'], {'action': 'cancel'}, user_id=OWNER)
    finish(runtime, reviewer, '', status='aborted')
    original = programs._save
    def fail(value, version, user_id):
        if value['current'] is None:
            if loss == 'after_commit':
                original(value, version, user_id)
            raise StorageError('database_timeout', 'controlled header acknowledgement loss')
        return original(value, version, user_id)
    monkeypatch.setattr(programs, '_save', fail)
    with pytest.raises(StorageError):
        programs.advance_program(p['program_id'], user_id=OWNER)
    # A receipt committed before the header CAS remains hidden from paging.
    receipt = deepcopy(programs.records.get_record(programs.EXPERIMENTS,
        programs._experiment_key(p['program_id'], 1, OWNER)))
    assert receipt is not None
    monkeypatch.setattr(programs, '_save', original)
    final = programs.advance_program(p['program_id'], user_id=OWNER)
    assert final['actuals']['tokens'] == 30 and final['settled_through'] == 1
    assert final['current'] is None and final['status'] == 'cancelled'
    assert programs.experiment_page(p['program_id'], user_id=OWNER)['experiments'] == [receipt]
    assert len(runtime[2]) == 2


def test_cancelled_current_does_not_erase_prior_incomplete_cost(runtime):
    programs, p, reviewer = reviewing_program(runtime)
    header, version = programs._read(p['program_id'], OWNER)
    header['actuals_complete'] = False
    programs._save(header, version, OWNER)
    programs.command_program(p['program_id'], {'action': 'cancel'}, user_id=OWNER)
    finish(runtime, reviewer, '', status='aborted')
    final = programs.advance_program(p['program_id'], user_id=OWNER)
    assert final['current'] is None and final['actuals']['tokens'] == 30
    assert final['actuals_complete'] is False
