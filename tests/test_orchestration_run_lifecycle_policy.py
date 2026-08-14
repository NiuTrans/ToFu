"""Pure durable-run lifecycle classification contract tests."""

from __future__ import annotations

import pytest

from lib.orchestration.run_lifecycle_policy import (
    RunLifecycle,
    abort_precondition,
    abort_runtime_conflict,
    classify_abort_transition,
    classify_delete_commit,
    classify_transition,
    delete_precondition,
)
from lib.orchestration_mutation import (
    MUTATION_ACTION_ABORT_RUN,
    MUTATION_ACTION_DELETE_RUN,
    MUTATION_ACTION_TRANSITION_RUN,
    MUTATION_ACTIVE,
    MUTATION_CONFLICT,
    MUTATION_NOT_FOUND,
    MUTATION_PERSISTENCE_FAILED,
    MUTATION_TERMINAL,
    RunMutationResult,
)


pytestmark = pytest.mark.unit


def test_lifecycle_normalizes_missing_status_and_legacy_terminal_rows():
    assert RunLifecycle.from_run({}) == RunLifecycle('pending', False)
    assert RunLifecycle.from_run({'status': 'done'}) == \
        RunLifecycle('done', True)
    assert RunLifecycle.from_run({
        'status': 'running', 'terminal': True,
    }) == RunLifecycle('running', True)


def test_transition_policy_classifies_commit_absence_and_active_failure():
    committed = classify_transition(
        'run-1', 'running', committed=True)
    missing = classify_transition(
        'run-1', 'running', committed=False, current=None)
    failed = classify_transition(
        'run-1', 'paused', committed=False,
        current={'status': 'running', 'terminal': False},
    )

    assert committed.ok is True
    assert committed.action == MUTATION_ACTION_TRANSITION_RUN
    assert committed.run_status == 'running'
    assert missing.reason == MUTATION_NOT_FOUND
    assert failed.reason == MUTATION_PERSISTENCE_FAILED
    assert failed.run_status == 'running'


def test_transition_policy_accepts_exact_terminal_retry_and_detects_race():
    current = {
        'status': 'done', 'terminal': True,
        'final': 'answer', 'error': None,
    }
    retry = classify_transition(
        'run-1', 'done', committed=False, current=current, final='answer')
    payload_mismatch = classify_transition(
        'run-1', 'done', committed=False, current=current, final='other')
    terminal_race = classify_transition(
        'run-1', 'aborted', committed=False, current=current)

    assert retry.ok is True
    assert retry.run_status == 'done'
    assert payload_mismatch.reason == MUTATION_PERSISTENCE_FAILED
    assert terminal_race.reason == MUTATION_CONFLICT


def test_abort_policy_owns_preconditions_runtime_failure_and_transition_scope():
    missing = abort_precondition('run-1', None)
    terminal = abort_precondition(
        'run-1', {'status': 'done', 'terminal': True})
    active = {'status': 'running', 'terminal': False}

    assert missing is not None and missing.reason == MUTATION_NOT_FOUND
    assert terminal is not None and terminal.reason == MUTATION_TERMINAL
    assert abort_precondition('run-1', active) is None
    conflict = abort_runtime_conflict('run-1', active)
    assert conflict.reason == MUTATION_CONFLICT
    assert conflict.action == MUTATION_ACTION_ABORT_RUN

    missing_transition = RunMutationResult(
        False, MUTATION_NOT_FOUND,
        action=MUTATION_ACTION_TRANSITION_RUN,
        target_id='run-1',
    )
    failed = classify_abort_transition(
        'run-1', active, missing_transition)
    assert failed.reason == MUTATION_PERSISTENCE_FAILED
    assert failed.action == MUTATION_ACTION_ABORT_RUN
    assert failed.run_status == 'running'


def test_delete_policy_owns_active_fence_and_commit_classification():
    active = {'status': 'running', 'terminal': False}
    terminal = {'status': 'error', 'terminal': True}

    missing = delete_precondition('run-1', None)
    blocked = delete_precondition('run-1', active)
    assert missing is not None and missing.reason == MUTATION_NOT_FOUND
    assert blocked is not None and blocked.reason == MUTATION_ACTIVE
    assert blocked.action == MUTATION_ACTION_DELETE_RUN
    assert delete_precondition('run-1', terminal) is None

    deleted = classify_delete_commit('run-1', terminal, deleted=True)
    failed = classify_delete_commit('run-1', terminal, deleted=False)
    assert deleted.ok is True and deleted.run_status == 'error'
    assert failed.reason == MUTATION_PERSISTENCE_FAILED
