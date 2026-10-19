"""Pending input inspection uses the same real native graph owner when selected."""
import pytest

from lib.identity import PrincipalContext
from lib.orchestration.engine import FlowExecutor
from tests.orchestration import test_orchestration_recovery_frontier as oracle
from tests.orchestration.test_native_flow_engine import native_library as native_library

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('case', ['linear', 'parallel', 'conversation', 'isolated', 'fanout', 'nested_parallel', 'loop'])
def test_pending_inspection_keeps_native_cursor_and_joined_graph_lifetimes(case, native_library, monkeypatch):
    original = FlowExecutor.__init__
    principal = PrincipalContext.user(subject_id='pending-frontier-oracle', owner_user_id=7)
    def initialize(self, definition, **options):
        options['native_flow_library'] = native_library
        options['parent_task'] = dict(id='pending-frontier-oracle', _userId=7,
            _principalContext=principal.to_payload())
        return original(self, definition, **options)
    monkeypatch.setattr(FlowExecutor, '__init__', initialize)
    if case in ('linear', 'parallel', 'conversation'):
        oracle.test_actual_interpreter_recovers_every_exact_pending_input_and_worker_snapshot(
            parallel=case != 'linear', conversation=case == 'conversation')
    elif case == 'loop':
        oracle.test_loop_inspection_replays_prior_verdict_before_matching_the_later_iteration()
    else:
        oracle.test_nested_and_dynamic_frontiers_use_original_scopes_without_waiting_for_all_workers(case)
    assert not native_library._graphs
