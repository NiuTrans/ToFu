"""Ordinary native Flow persists exact private fees with its child completion."""
from copy import deepcopy
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


@pytest.mark.parametrize('parallel', [False, True])
def test_native_flow_keeps_frozen_fees_outside_public_completion(
        application_storage, tmp_path, monkeypatch, caplog, parallel):
    from lib.orchestration.invocation_recovery import (
        FIELD, RegisteredInvocationCompletion, read_registered_actor_fees,
    )
    from lib.orchestration.engine import FlowExecutor
    from lib.orchestration.execution_journal import ExecutionJournal
    # The ordinary non-Goal HTTP Flow fixture does not request continuity.
    # Compose its public journal port so the real native children use the same
    # registered completion boundary as a recoverable Goal/Flow run.
    initialize, run = FlowExecutor.__init__, FlowExecutor.run
    journals = {}
    def initialize_with_journal(engine, definition, *args, **kwargs):
        events = []
        journal = ExecutionJournal(lambda event: events.append(deepcopy(event)),
            worker_context_policy=kwargs.get('worker_context_policy', 'isolated'))
        kwargs['execution_journal'] = journal
        initialize(engine, definition, *args, **kwargs)
        journals[id(engine)] = (definition, journal, events)
    def run_with_journal(engine, *, initial_context):
        definition, journal, _ = journals[id(engine)]
        journal.begin(definition, initial_context)
        return run(engine, initial_context=initial_context)
    monkeypatch.setattr(FlowExecutor, '__init__', initialize_with_journal)
    monkeypatch.setattr(FlowExecutor, 'run', run_with_journal)
    observed, cuts = [], []
    settle = RegisteredInvocationCompletion.settle

    def completed(writer, receipt):
        child = writer._child
        original_rows = deepcopy(child['apiRounds'])
        settle(writer, receipt)
        record = application_storage.query('record.get', dict(
            namespace='task_results', key=child['id'], user_id=2))
        admission = record['value'][FIELD]['admission']
        scope = dict(owner_user_id=2, conversation_id=child['convId'], parent_task_id=child['_parentTaskId'])
        proof = read_registered_actor_fees(application_storage, admission, **scope)
        settle(writer, receipt)
        assert read_registered_actor_fees(application_storage, admission, **scope) == proof
        observed.append((original_rows, deepcopy(receipt), admission, scope, proof))
        from lib.agent_core.account_frontier import capture_frontier
        budget = child['_executionBudget']
        with budget._lock:
            cuts.append(capture_frontier(budget, version=1, reason='bound'))

    monkeypatch.setattr(RegisteredInvocationCompletion, 'settle', completed)
    native_case(application_storage, tmp_path, monkeypatch, caplog,
        scenario='text', prebind_failure=False, resume_operation=None,
        native_flow=True, flow_parallel=parallel, actor_tool_round=True)
    assert len(observed) == 2
    from lib.billing.native_aggregate_fees import project
    cut = max(cuts, key=lambda row: row['snapshot']['tokens'])
    aggregate = project(cut, [row[-1]['evidence'] for row in observed], owner_user_id=2,
        root_task_id=cut['snapshot']['root_task_id'], wallet_user_id='source-wallet',
        reserved_micro=100000, model='fixture')
    assert aggregate['roundCount'] == 4
    assert aggregate['componentsMicro']['totalMicro'] == sum(
        row['quote']['credits']['total'] for entry in observed for row in entry[-1]['evidence']['rounds'])
    for rows, receipt, admission, scope, saved in observed:
        cold = read_registered_actor_fees(application_storage, admission, **scope)
        assert cold['evidence'] == saved['evidence']
        proof = saved['evidence']
        assert proof['tokens'] == 10
        assert len(proof['rounds']) == len(rows) == 2
        for stored, original in zip(proof['rounds'], rows):
            assert stored['producer_task_id'] == receipt['invocation_id'] == original['taskId']
            assert stored['payer_task_id'] == proof['root_task_id'] == receipt['execution_account']['root_task_id']
            assert stored['quote']['price'] == original['nativePrice']
            assert stored['quote']['credits'] == original['nativeCredits']
        assert all(key not in json.dumps(receipt) for key in ('native_fees', 'nativeCredits', 'nativePrice'))
