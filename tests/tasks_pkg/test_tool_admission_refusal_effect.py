"""A pre-execution command refusal never enters the tool effect.

`CommandAdmissionRefusal` proves the handler never ran the command: nothing
was spawned, opened or mutated. The occurrence must settle `not_started` and
surface the refusal itself, not a false `tool_effect_unknown` that claims the
operation "may have taken effect".
"""
import json

import pytest

from lib.tools.result_envelope import tool_result_error
from tests.tasks_pkg.test_tool_outcome_finality import dispatch
from tests.tasks_pkg.test_tool_timeout_recovery import recovery_task

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('policy', ['read_only', 'idempotent', 'reconcile'])
def test_command_admission_refusal_settles_not_started_without_unknown_effect(policy):
    from lib.project_mod.run_command import CommandAdmissionRefusal

    def handler(task, tc, name, call_id, *args):
        raise CommandAdmissionRefusal(
            'Error [workspace-target-unresolved]: Unresolved Git shell output '
            'target; use a literal output filename. Command was not started.')

    task = recovery_task(handler, policy)
    row, messages, settled = dispatch(task)
    error = tool_result_error(messages[0]['content'])
    assert error is not None
    # The refusal is a known no-effect outcome, never an unknown effect.
    assert error.code == 'tool_execution_failed'
    assert error.retryable is False
    assert 'may have taken effect' not in error.message
    assert 'was not started' in error.message
    effect = row['executionEffect']
    assert effect['state'] == 'not_started'
    assert effect['operationId'] not in error.message
    if policy == 'reconcile':
        assert 'Do not repeat' not in error.next_action


def test_plain_value_error_still_reports_an_entered_unknown_effect():
    """An untyped ValueError keeps the existing unknown-effect recovery."""

    def handler(task, tc, name, call_id, *args):
        raise ValueError('runtime environment incomplete')

    task = recovery_task(handler, 'reconcile')
    row, messages, _ = dispatch(task)
    error = tool_result_error(messages[0]['content'])
    assert error is not None
    assert error.code == 'tool_effect_unknown'
    assert row['executionEffect']['state'] == 'unknown'
