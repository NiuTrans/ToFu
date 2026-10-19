"""A billed native Flow must protect its root hold before any child fees exist."""
import json
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 2


def test_native_flow_root_hold_survives_missing_terminal_projection(
        application_storage, tmp_path, monkeypatch, caplog):
    observed = []
    def observe(child, operation, state):
        if observed:
            return
        from lib.billing.native_settlement import recover
        from lib.billing import get_balance
        account = child['_executionBudget']; root = account._active[account.root_id]
        record = application_storage.query('task_results.replay_get', dict(
            key=root['id'], user_id=2, include_metadata=True))
        before = get_balance(root['_billingUserId'])
        try:
            recovered = recover(wallet_user_id=root['_billingUserId'], task_id=root['id'],
                held_micro=root['_billing_reservation_micro'])
        except ValueError:
            recovered = 'protected'
        observed.append(dict(in_memory_required=root.get('_nativePricingRequired'),
            durable_required=(record or {}).get('metadata', {}).get('nativePricingRequired'),
            recovery=recovered, balance=before, after=get_balance(root['_billingUserId'])))
    native_case(application_storage, tmp_path, monkeypatch, caplog,
        scenario='text', prebind_failure=False, resume_operation=None,
        native_flow=True, actor_checkpoint=True, actor_observer=observe, actor_tool_round=True)
    assert observed and observed[0]['recovery'] == 'protected', json.dumps(observed)
    assert observed[0]['balance'] == observed[0]['after']
