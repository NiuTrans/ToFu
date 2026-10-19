"""Real v4 native Flow leaves persist actor frontiers within one priced tree."""
import pytest

from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 2


def test_registered_file_flow_without_checkpoints(
        application_storage, tmp_path, monkeypatch, caplog):
    native_case(application_storage, tmp_path, monkeypatch, caplog,
        scenario='text', prebind_failure=False, resume_operation=None,
        native_flow=True, actor_tool_round=True)
    files = list(tmp_path.glob('actor-*.txt'))
    assert len(files) == 2 and all(path.read_text() == 'once\n' for path in files)


@pytest.mark.parametrize('parallel', [False, True])
def test_registered_actor_checkpoints_preserve_local_usage_and_original_fees(
        application_storage, tmp_path, monkeypatch, caplog, parallel):
    tool_round = True
    snapshots = []
    def observe(task, operation, state):
        account = task['_executionBudget']
        saved = state['shared_model_account']
        assert operation == 'complete' or tool_round
        assert saved['actor_task_id'] == state['task_id'] == task['id']
        assert saved['root_task_id'] == account.root_id != task['id']
        assert saved['tokens'] == 5 * state['next_round']
        assert saved['tool_calls'] == (1 if tool_round and operation == 'complete' else 0)
        assert saved['complete'] and task['id'] in account.continuity_snapshot()['active_child_task_ids']
        assert state['host_fee_projection']['wallet_user_id'] == ''
        assert 'wallet_settlement' not in state['host_fee_projection']
        assert state['host_fee_projection']['rounds'][0]['task_id'] == task['id']
        assert all(row['billable_task_id'] == account.root_id
                   for row in state['host_fee_projection']['rounds'])
        snapshots.append(state)
    if tool_round:
        from lib.tasks_pkg.native_gateway import NativeGateway
        execute = NativeGateway._execute
        def after_committed_frontier(gateway, request, approved):
            if request.name == 'write_file':
                assert any(state['task_id'] == gateway.task['id'] and state.get('tools_pending')
                           for state in snapshots), 'tool entered before its durable paid actor frontier'
            return execute(gateway, request, approved)
        monkeypatch.setattr(NativeGateway, '_execute', after_committed_frontier)
    native_case(application_storage, tmp_path, monkeypatch, caplog,
        scenario='text',prebind_failure=False,resume_operation=None,
        native_flow=True,flow_parallel=parallel,actor_checkpoint=True,actor_observer=observe,
        actor_tool_round=tool_round)
    assert len(snapshots) == 4
    assert len({state['task_id'] for state in snapshots}) == 2
    assert len({state['shared_model_account']['root_task_id'] for state in snapshots}) == 1
    if tool_round:
        from lib.tasks_pkg.handlers.tool_effect_inspection import inspect_for_task
        pending = [state for state in snapshots if state.get('tools_pending')]
        assert len(pending) == 2
        for state in pending:
            metadata = state['tool_recovery']; reference = metadata['calls'][0]['inspection']
            assert reference['invocationId'] == metadata['producer_task_id'] == state['task_id']
            result = inspect_for_task({'_userId': 2, 'convId': reference['conversationId']}, {
                'invocation_id': reference['invocationId'], 'operation_id': reference['operationId']})
            assert result['outcome'] == 'file_published'
        files = list(tmp_path.glob('actor-*.txt'))
        assert len(files) == 2 and all(path.read_text() == 'once\n' for path in files)
