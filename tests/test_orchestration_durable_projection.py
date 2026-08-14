"""Fail-closed durable worker/start projection boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.orchestration.durable_projection import (
    DurableProjectionError,
    DurableRunProjection,
)
from lib.orchestration_mutation import (
    MUTATION_CONFLICT,
    MUTATION_PERSISTENCE_FAILED,
)


pytestmark = pytest.mark.unit


class _Runs:
    def __init__(self):
        self.projected = []
        self.transitions = []
        self.project_result = True
        self.transition_result = SimpleNamespace(
            ok=True, reason='accepted', run_status='running')
        self.project_error = None
        self.transition_error = None

    def project_event(self, run_id, seq, event, status=''):
        if self.project_error is not None:
            raise self.project_error
        self.projected.append((run_id, seq, event, status))
        return self.project_result

    def transition_status(self, run_id, status, **values):
        if self.transition_error is not None:
            raise self.transition_error
        self.transitions.append((run_id, status, values))
        return self.transition_result


def test_event_and_nonterminal_status_projection_fail_closed():
    runs = _Runs()
    projection = DurableRunProjection(runs, 'run-1')
    runs.project_result = False

    with pytest.raises(DurableProjectionError, match='run-1/4'):
        projection.project_event(
            4, {'type': 'step_complete'}, 'running')

    runs.project_result = True
    projection.project_event(5, {'type': 'step_trace'})
    assert runs.projected[-1] == (
        'run-1', 5, {'type': 'step_trace'}, '',
    )


def test_event_projection_wraps_port_exceptions():
    runs = _Runs()
    runs.project_error = OSError('database offline')

    with pytest.raises(DurableProjectionError, match='run-1/4'):
        DurableRunProjection(runs, 'run-1').project_event(
            4, {'type': 'flow_start'}, 'running')


def test_terminal_projection_classifies_accept_abort_race_and_failure():
    runs = _Runs()
    projection = DurableRunProjection(runs, 'run-1')

    accepted = projection.finalize('done', final='answer')
    assert accepted.accepted and not accepted.abort_won

    runs.transition_result = SimpleNamespace(
        ok=False, reason=MUTATION_CONFLICT, run_status='aborted')
    aborted = projection.finalize('done', final='late')
    assert aborted.abort_won and aborted.error is None

    runs.transition_result = SimpleNamespace(
        ok=False,
        reason=MUTATION_PERSISTENCE_FAILED,
        run_status='running',
    )
    failed = projection.finalize('done')
    assert not failed.accepted and not failed.abort_won
    assert isinstance(failed.error, DurableProjectionError)


def test_terminal_projection_normalizes_error_before_any_run_port():
    runs = _Runs()
    projection = DurableRunProjection(runs, 'run-1')

    assert projection.finalize(
        'error', error={'kind': 'future_kind', 'message': 'failed'},
    ).accepted

    error = runs.transitions[0][2]['error']
    assert error['kind'] == 'generic'
    assert error['message'] == 'failed'
    assert error['context'] == 'durable finalization'
    assert error['source'] == 'orchestration:durable-projection'


def test_best_effort_error_projection_reports_rejection_and_exception():
    runs = _Runs()
    projection = DurableRunProjection(runs, 'run-1')
    runs.transition_result = SimpleNamespace(
        ok=False,
        reason=MUTATION_PERSISTENCE_FAILED,
        run_status='running',
    )

    assert projection.record_error({'kind': 'projection'}) is False

    runs.transition_error = OSError('database offline')
    assert projection.record_error({'kind': 'start'}) is False


def test_best_effort_error_projection_closes_the_envelope_kind_boundary():
    runs = _Runs()
    projection = DurableRunProjection(runs, 'run-1')

    assert projection.record_error({
        'kind': 'runtime_start',
        'message': 'worker failed',
    }) is True

    error = runs.transitions[0][2]['error']
    assert error['kind'] == 'generic'
    assert error['message'] == 'worker failed'
    assert error['context'] == 'projection failure'
    assert error['source'] == 'orchestration:durable-projection'
