"""Terminal orchestration semantics stay identical across every adapter."""

import threading

import pytest

from lib.agent_verdict import INCOMPLETE_STOP_REASONS
from lib.error_envelope import is_envelope
from lib.orchestration.outcome_contract import (
    OUTCOME_ERROR_DISPLAY_CHARS,
    OUTCOME_FINAL_DISPLAY_CHARS,
    outcome_contract,
    outcome_payload_schema,
)
from lib.orchestration.outcome_domain import (
    OUTCOME_FORMAT,
    classify_terminal_outcome,
    outcome_from_result,
)
from lib.orchestration.outcome_ledger import OrchestrationOutcomeLedger
from lib.orchestration.outcome_projection import (
    aborted_result,
    failure_result,
    outcome_from_run_header,
    project_run_header_outcome,
)


pytestmark = pytest.mark.unit


def test_outcome_internal_consumers_depend_on_focused_owners():
    from pathlib import Path

    domain_consumers = [
        'lib/orchestration/outcome_contract.py',
        'lib/orchestration/outcome_ledger.py',
        'lib/orchestration/runtime_outcome.py',
        'lib/orchestration_execution_runtime.py',
        'lib/orchestration_subflow_runtime.py',
    ]
    projection_consumers = [
        'lib/orchestration/run_service.py',
        'lib/orchestration/runtime_outcome.py',
    ]

    for filename in domain_consumers + projection_consumers:
        assert 'orchestration.outcome_result' not in Path(filename).read_text()
    assert 'orchestration.outcome_domain' in Path(
        'lib/orchestration/outcome_ledger.py').read_text()
    assert 'orchestration.outcome_projection' in Path(
        'lib/orchestration/run_service.py').read_text()


@pytest.mark.parametrize(
    ('status', 'values', 'category', 'lifecycle', 'chat', 'finish', 'reason'),
    [
        ('completed', {}, 'success', 'done', 'done', 'stop', 'completed'),
        ('completed', {'reported_ok': False,
                       'reported_stop_reason': 'max_iterations'},
         'incomplete', 'error', 'done', 'incomplete', 'max_iterations'),
        ('failed', {'failure_kind': 'structural', 'error': 'bad edge'},
         'failure', 'error', 'error', 'error', 'structural'),
        ('aborted', {}, 'aborted', 'aborted', 'aborted', 'aborted', 'aborted'),
    ],
)
def test_terminal_classification_projects_each_surface_once(
        status, values, category, lifecycle, chat, finish, reason):
    outcome = classify_terminal_outcome(status, **values)

    assert outcome.category == category
    assert outcome.lifecycle_status == lifecycle
    assert outcome.chat_status == chat
    assert outcome.finish_reason == finish
    assert outcome.stop_reason == reason
    assert outcome.as_dict()['format'] == OUTCOME_FORMAT


def test_node_failure_wins_over_an_incomplete_loop_exit():
    outcome = classify_terminal_outcome(
        'completed',
        loop_exits=[{'node_id': 'loop', 'reason': 'max_iterations'}],
        node_failures=[{'node_id': 'worker', 'error': 'runner crashed'}],
    )

    assert outcome.category == 'failure'
    assert outcome.stop_reason == 'node_failed'
    assert outcome.error == 'runner crashed'


def test_outcome_contract_owns_cross_surface_display_limits():
    contract = outcome_contract()
    assert contract['displayLimits'] == {
        'final': OUTCOME_FINAL_DISPLAY_CHARS,
        'error': OUTCOME_ERROR_DISPLAY_CHARS,
    }
    assert contract['incompleteStopReasons'] == sorted(
        INCOMPLETE_STOP_REASONS)


def test_outcome_ledger_owns_snapshots_and_keeps_legacy_clear_view():
    ledger = OrchestrationOutcomeLedger(lock=threading.Lock())
    ledger.record_loop_exit(
        node_id='loop', reason='no_progress', iterations=3)
    ledger.record_artifact({'node_id': 'artifact', 'path': 'report.md'})

    snapshot = ledger.loop_exits_snapshot()
    snapshot[0]['reason'] = 'completed'
    assert ledger.classify('completed').stop_reason == 'no_progress'

    ledger.loop_exits_live.clear()
    assert ledger.classify('completed').category == 'success'
    assert ledger.artifacts_snapshot() == [
        {'node_id': 'artifact', 'path': 'report.md'},
    ]


def test_result_normalizer_accepts_legacy_and_canonical_shapes():
    legacy = outcome_from_result({
        'ok': False,
        'status': 'completed',
        'stop_reason': 'stuck',
    })
    canonical = outcome_from_result({
        'ok': True,
        'status': 'completed',
        'outcome': classify_terminal_outcome(
            'completed', reported_ok=False,
            reported_stop_reason='replan_exhausted').as_dict(),
    })

    assert legacy.category == 'incomplete'
    assert canonical.category == 'incomplete'
    assert canonical.stop_reason == 'replan_exhausted'


def test_failure_and_abort_result_helpers_keep_one_versioned_shape():
    failed = failure_result(RuntimeError('boom'), 'exception')
    aborted = aborted_result(failed)

    assert failed['outcome']['format'] == OUTCOME_FORMAT
    assert failed['outcome']['category'] == 'failure'
    assert outcome_from_result(failed).runtime_error == 'RuntimeError: boom'
    assert aborted['outcome']['category'] == 'aborted'
    assert aborted['status'] == 'aborted'
    assert 'error' not in aborted


def test_outcome_payload_schema_is_owned_with_the_published_contract():
    contract = outcome_contract()
    schema = outcome_payload_schema()

    assert schema['required'] == [
        'format', 'category', 'engine_status', 'lifecycle_status',
        'chat_status', 'ok', 'stop_reason', 'finish_reason', 'error',
    ]
    assert schema['properties']['category']['enum'] == contract['categories']
    assert schema['properties']['lifecycle_status']['enum'] == \
        contract['lifecycleStatuses']


def test_incomplete_durable_error_preserves_machine_outcome_and_message():
    outcome = classify_terminal_outcome(
        'completed', reported_ok=False,
        reported_stop_reason='max_iterations')

    error = outcome.error_envelope
    assert is_envelope(error)
    assert error['kind'] == 'generic'
    assert error['severity'] == 'warning'
    assert error['retryable'] is False
    assert error['detail'] == 'max_iterations'
    assert error['outcome'] == outcome.as_dict()


def test_failure_error_envelope_is_shared_and_uses_registered_kind():
    outcome = classify_terminal_outcome(
        'failed', failure_kind='exception', error='worker exploded')

    assert is_envelope(outcome.error_envelope)
    assert outcome.error_envelope['kind'] == 'generic'
    assert outcome.error_envelope['severity'] == 'error'
    assert outcome.error_envelope['detail'] == 'worker exploded'


def test_durable_header_projection_handles_canonical_and_legacy_rows():
    incomplete = classify_terminal_outcome(
        'completed', reported_ok=False,
        reported_stop_reason='max_iterations')
    canonical = project_run_header_outcome({
        'id': 'canonical', 'status': 'error', 'terminal': True,
        'error': incomplete.error_envelope,
    })
    legacy = outcome_from_run_header({
        'id': 'legacy', 'status': 'error', 'terminal': True,
        'error': 'no_progress',
    })
    done = project_run_header_outcome({
        'id': 'done', 'status': 'done', 'terminal': True,
        'error': None,
    })

    assert canonical['outcome'] == incomplete.as_dict()
    assert legacy is not None and legacy.category == 'incomplete'
    assert done['outcome']['category'] == 'success'


def test_durable_header_projection_repairs_legacy_layout_without_mutation():
    stored_definition = {
        'schema': 'tofu.orchestration/v1',
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {'id': 'worker', 'type': 'role', 'role': 'worker',
             'pos': {'x': 10 ** 1000, 'y': 30}},
            {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'start', 'to': 'worker'},
            {'from': 'worker', 'to': 'stop'},
        ],
    }

    projected = project_run_header_outcome({
        'id': 'legacy', 'status': 'running', 'terminal': False,
        'definition': stored_definition,
    })

    positions = [node['pos'] for node in projected['definition']['nodes']]
    assert [(pos['x'], pos['y']) for pos in positions] == [
        (40, 30), (40, 180), (40, 330),
    ]
    assert 'pos' not in stored_definition['nodes'][0]
    assert stored_definition['nodes'][1]['pos']['x'] == 10 ** 1000
    assert projected['definition'] is not stored_definition


def test_durable_header_projection_preserves_complete_user_layout():
    stored_definition = {
        'nodes': [
            {'id': 'start', 'pos': {'x': 101.5, 'y': -20}},
            {'id': 'worker', 'pos': {'x': 402, 'y': 87}},
        ],
        'edges': [{'from': 'start', 'to': 'worker'}],
    }

    projected = project_run_header_outcome({
        'id': 'custom', 'status': 'running', 'terminal': False,
        'definition': stored_definition,
    })

    assert projected['definition'] == stored_definition
    assert projected['definition'] is not stored_definition
    assert projected['definition']['nodes'] is not stored_definition['nodes']


def test_legacy_header_terminal_detection_uses_run_status_boundary(monkeypatch):
    import lib.orchestration.run_status as run_status

    monkeypatch.setattr(
        run_status, 'is_terminal_run_status',
        lambda status: status == 'archived',
    )

    projected = outcome_from_run_header({
        'id': 'future', 'status': 'archived', 'error': 'legacy failure',
    })

    assert projected is not None
    assert projected.category == 'failure'
