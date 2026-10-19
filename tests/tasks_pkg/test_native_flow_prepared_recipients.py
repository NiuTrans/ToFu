"""Real application ROOT/native file rounds consume prepared Flow recipients.

The wrapper supplies the explicit preparation policy at the existing Flow entry.
The upstream provider remains the established loopback fixture; routing, ROOT
preparation, checkpoint factories, registered completion and file writes are real.
This covers recipient integration, not an already interrupted source checkpoint.
"""
from copy import deepcopy

import pytest

from lib.orchestration.agent_runner import OrchestrationAgentRunnerConfig
from lib.orchestration.continuation import FlowSuspended
from lib.orchestration.engine import FlowExecutor
from lib.orchestration.execution_journal import ExecutionJournal
from lib.orchestration.prepared_recipients import prepare_flow_recipients
from lib.orchestration.recovery_frontier import PendingInvocationInput
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 2


@pytest.mark.parametrize('parallel', [False, True])
def test_actual_native_file_flow_consumes_prepared_registered_recipients(
        application_storage, tmp_path, monkeypatch, caplog, parallel):
    from lib.orchestration import runtime_service
    execute = runtime_service.execute_flow
    prepared_ids, checkpoint_ids, receipts = [], [], []

    def prepared_flow(definition, *, initial_context='', on_event=None, abort_check=None, executor_options=None):
        options = dict(executor_options or {})
        config = OrchestrationAgentRunnerConfig(**{key: options[key]
            for key in OrchestrationAgentRunnerConfig.__dataclass_fields__ if key in options})
        inputs = {}
        def observe(*, key, kind, inputs: list, iteration, input_hash, worker_context):
            assert kind == 'agent'
            frontiers[key] = PendingInvocationInput(key, input_hash, deepcopy(inputs[0]),
                inputs[1], iteration, deepcopy(worker_context))
        frontiers = inputs
        def refuse(*args, **kwargs):
            pytest.fail('read-only input discovery entered an effect')
        scout = ExecutionJournal(refuse, worker_context_policy=config.worker_context_policy,
            unsettled_observer=observe)
        with pytest.raises(FlowSuspended):
            FlowExecutor(definition, **dict(options, agent_runner=refuse, execution_journal=scout),
                on_event=lambda event: None, abort_check=abort_check).run(initial_context=initial_context)
        assert len(inputs) == (2 if parallel else 1)
        journal = options.get('execution_journal')
        if journal is None:
            journal = ExecutionJournal(lambda event: receipts.append(deepcopy(event)),
                worker_context_policy=config.worker_context_policy)
            journal.begin(definition, initial_context)
        with prepare_flow_recipients(inputs, config=config, emit=on_event or (lambda event: None),
                                     abort_check=abort_check or (lambda: False)) as recipients:
            prepared_ids.extend(task['id'] for task in recipients.children.values())
            recipients.bind(journal)
            result = execute(definition, initial_context=initial_context, on_event=on_event,
                abort_check=abort_check, executor_options=dict(options, execution_journal=journal))
            journal.verify_consumed()
            return result

    monkeypatch.setattr(runtime_service, 'execute_flow', prepared_flow)
    def checkpoint(task, operation, state):
        if operation == 'pending':
            checkpoint_ids.append(task['id'])
    native_case(application_storage, tmp_path, monkeypatch, caplog,
        scenario='text', prebind_failure=False, resume_operation=None,
        native_flow=True, flow_parallel=parallel, actor_checkpoint=True,
        actor_tool_round=True, actor_observer=checkpoint)
    assert set(prepared_ids) <= set(checkpoint_ids)
    assert len(checkpoint_ids) == len(set(checkpoint_ids)) == 2
    if receipts:
        completed = [row for row in receipts if row['type'] == 'execution_effect_completed']
        assert len(completed) == 2
        assert {row['invocation_id'] for row in completed} == set(checkpoint_ids)
    files = list(tmp_path.glob('actor-*.txt'))
    assert len(files) == 2 and all(path.read_text() == 'once\n' for path in files)
