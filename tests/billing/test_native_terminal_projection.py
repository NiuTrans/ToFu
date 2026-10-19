"""An uncertain native bill must not prevent a failed Turn from being recorded."""
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.manager._persist import build_result_meta
from tests.billing.test_native_task_cost import task as quoted_task

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('status,reason', [
    ('error','error'), ('aborted','aborted'), ('interrupted','interrupted'), ('done','aborted')])
def test_unknown_native_usage_records_failed_outcome_without_a_settlement(status, reason):
    task = quoted_task()
    task.update(_nativePricingRequired=True, _billingUserId='wallet',
        _billing_reservation_micro=100, status=status, finish_reason=reason,
        _executionActualsComplete=True,
        _executionBudget=SimpleNamespace(consumption_snapshot=lambda: (10200,0,False)))
    metadata = build_result_meta(task)
    assert metadata['nativePricingRequired'] is True
    assert metadata['usageComplete'] is False
    assert 'nativeBillingSettlement' not in metadata
    assert '_nativeBillingSettlement' not in task


def test_unknown_native_usage_cannot_publish_a_successful_invoice():
    task = quoted_task()
    task.update(_nativePricingRequired=True, _billingUserId='wallet',
        status='done', finish_reason='stop',
        _executionBudget=SimpleNamespace(consumption_snapshot=lambda: (10200,0,False)))
    with pytest.raises(ValueError, match='unknown financial usage'):
        build_result_meta(task)


def test_unknown_failed_projection_cannot_replace_an_already_frozen_invoice():
    task = quoted_task()
    task.update(_nativePricingRequired=True, _billingUserId='wallet',
        status='error', _nativeBillingSettlement={},
        _executionBudget=SimpleNamespace(consumption_snapshot=lambda: (10200,0,False)))
    with pytest.raises(ValueError, match='frozen settlement changed'):
        build_result_meta(task)


@pytest.mark.parametrize('damage', ['missing_account','missing_quotes'])
def test_failed_projection_does_not_hide_invalid_known_financial_evidence(damage):
    task = quoted_task()
    task.update(_nativePricingRequired=True, _billingUserId='wallet', status='error')
    if damage == 'missing_account':
        task.pop('_executionBudget')
    else:
        task['apiRounds'] = []
    with pytest.raises(ValueError, match='requires'):
        build_result_meta(task)
