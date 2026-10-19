"""Settled child native quotes enter the existing root wallet exactly once."""
import copy
import pytest
from lib.agent_core.execution_budget import ExecutionBudget
from lib.billing.cost import native_task_cost,transfer_native_child_fees
from tests.billing.test_native_task_cost import task as quote_task

pytestmark=pytest.mark.unit


def family():
    parent=quote_task();parent.update(_userId=7,status='running',config={'model':'alpha'},
        usage={'prompt_tokens':10000,'completion_tokens':200},_nativePricingRequired=True)
    parent['apiRounds'][0]['taskId']=parent['id']
    account=ExecutionBudget(parent);parent['_executionBudget']=account
    child=quote_task();child.update(id='paid-child',_parentTaskId=parent['id'],_userId=7,status='running',
        config={'model':'alpha'},usage={'prompt_tokens':10000,'completion_tokens':200},_executionBudget=account,
        _nativePricingRequired=True,_executionActualsComplete=True)
    child['apiRounds'][0]['taskId']=child['id']
    account.attach(child,parent_task_id=parent['id']);child['status']='done'
    return parent,child,account


def test_child_fee_transfer_preserves_original_quotes_and_wallet_coverage_once():
    parent,child,account=family();original=copy.deepcopy(child['apiRounds'])
    transfer_native_child_fees(parent,child);transfer_native_child_fees(parent,child)
    assert len(parent['apiRounds'])==2 and child['apiRounds']==original
    row=parent['apiRounds'][1]
    assert row['taskId']==child['id'] and row['billableTaskId']==parent['id'] and row['sourceRound']==1
    assert row['nativeCredits']==original[0]['nativeCredits'] and row['nativePrice']==original[0]['nativePrice']
    account.close(child)
    cost=native_task_cost(parent,model='alpha')
    assert cost.micro==2*original[0]['nativeCredits']['total']
    assert account.consumption()[0]==20400


@pytest.mark.parametrize('fault',['owner','parent','carrier','unknown','pending'])
def test_foreign_or_unsettled_child_cannot_transfer_quotes(fault):
    parent,child,account=family();before=copy.deepcopy(parent['apiRounds'])
    if fault=='owner':child['_userId']=8
    elif fault=='parent':child['_parentTaskId']='other'
    elif fault=='carrier':child=copy.copy(child)
    elif fault=='unknown':child['_executionActualsComplete']=False
    else:child['_terminalPersistencePending']=True
    with pytest.raises(ValueError):transfer_native_child_fees(parent,child)
    assert parent['apiRounds']==before and '_nativeChildFeeGrants' not in parent


def test_mutated_or_duplicate_transferred_quote_is_not_billable():
    parent,child,account=family();transfer_native_child_fees(parent,child);account.close(child)
    parent['apiRounds'].append(dict(parent['apiRounds'][1],round=3))
    with pytest.raises(ValueError,match='another executor'):native_task_cost(parent,model='alpha')
    parent['apiRounds'].pop();parent['apiRounds'][1]['provider_id']='changed'
    with pytest.raises(ValueError,match='another executor'):native_task_cost(parent,model='alpha')


def test_fee_retry_after_detachment_keeps_exact_candidate_and_never_appends():
    parent, child, account = family()
    transfer_native_child_fees(parent, child)
    account.close(child)
    before = copy.deepcopy(parent['apiRounds'])
    transfer_native_child_fees(parent, child)
    assert parent['apiRounds'] == before
    child['apiRounds'][0]['nativePrice']['input'] += 1
    with pytest.raises(ValueError, match='candidate'):
        transfer_native_child_fees(parent, child)
    assert parent['apiRounds'] == before


def test_detached_child_without_fee_grant_cannot_create_new_billable_rows():
    parent, child, account = family()
    account.close(child)
    with pytest.raises(ValueError, match='settled participant'):
        transfer_native_child_fees(parent, child)
    assert len(parent['apiRounds']) == 1


def test_participant_completion_does_not_certify_running_tree_or_unknown_actor():
    parent, child, account = family()
    assert account.participant_actuals_complete(child)
    assert not account.actuals_complete()
    assert not account.participant_actuals_complete(copy.copy(child))
    child['_executionActualsComplete'] = False
    assert not account.participant_actuals_complete(child)


def test_nested_child_quotes_transfer_directly_to_tree_root_once():
    parent, child, account = family()
    child['status'] = 'running'
    grandchild = quote_task()
    grandchild.update(id='grandchild', _parentTaskId=child['id'], _userId=7,
        status='running', config={'model': 'alpha'}, _executionBudget=account,
        usage={'prompt_tokens': 10000, 'completion_tokens': 200},
        _nativePricingRequired=True, _executionActualsComplete=True)
    grandchild['apiRounds'][0]['taskId'] = grandchild['id']
    account.attach(grandchild, parent_task_id=child['id'])
    grandchild['status'] = 'done'
    transfer_native_child_fees(parent, grandchild)
    account.close(grandchild)
    assert len(child['apiRounds']) == 1
    child['status'] = 'done'
    transfer_native_child_fees(parent, child)
    account.close(child)
    transfer_native_child_fees(parent, grandchild)
    assert [row['taskId'] for row in parent['apiRounds']] == [parent['id'], 'grandchild', child['id']]
    assert native_task_cost(parent, model='alpha').micro == 3 * child['apiRounds'][0]['nativeCredits']['total']
    assert account.consumption_snapshot()[0] == 30600
    assert not account._attachment_parents


def test_mutable_parent_hint_cannot_replace_registered_attachment_edge():
    parent, child, account = family()
    child['_parentTaskId'] = child['id']
    with pytest.raises(ValueError, match='settled participant'):
        transfer_native_child_fees(parent, child)
    assert len(parent['apiRounds']) == 1


@pytest.mark.parametrize('fault', ['receiver_clone', 'receiver_owner', 'foreign_account', 'closed_parent'])
def test_fee_receiver_admission_requires_live_owned_root(fault):
    root, child, account = family()
    child['status'] = 'running'
    receiver = root
    if fault == 'receiver_clone':
        receiver = copy.copy(root)
    elif fault == 'receiver_owner':
        receiver = dict(root, _userId=8)
    elif fault == 'foreign_account':
        child['_executionBudget'] = object()
    else:
        account.close(child)
    with pytest.raises(ValueError, match='owned execution root'):
        account.validate_child_invoice_receiver(receiver, child)
