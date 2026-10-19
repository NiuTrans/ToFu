"""One real native task preserves review, reconciles a write and adopts new scope.

This is a short deterministic rehearsal, not a real-model or two-hour result.
The provider is scripted; ROOT, gateway, checkpoint, process death, continuation,
file inspection, requirement admission and owner review are production owners.
"""
import pytest

from tests.tasks_pkg.test_native_file_root_recovery import (
    test_actual_root_restores_paid_file_batch_and_spent_tools as run_scenario,
)

pytestmark = pytest.mark.api


@pytest.mark.parametrize('fresh_executor', [False, True])
def test_restart_and_requirement_change_preserve_verified_output(tmp_path, fresh_executor):
    run_scenario(tmp_path, wallet=True, changed_file=False,
        fresh_executor=fresh_executor, delivery_review=True)


@pytest.mark.parametrize('fresh_executor', [False, True])
def test_changed_requirement_revises_a_previously_accepted_report_only(tmp_path, fresh_executor):
    run_scenario(tmp_path, wallet=True, changed_file=False,
        fresh_executor=fresh_executor, delivery_review=True, revise_accepted_output=True)
