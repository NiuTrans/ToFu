"""Ordinary child executions retain root runtime semantics without root rights."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from lib.tasks_pkg import invocation
from lib.tasks_pkg.child_invocation import create_child_invocation, run_child_invocation

pytestmark = pytest.mark.unit


def _child(sink=lambda event: None, abort=lambda: False, *, persistent=False):
    return create_child_invocation(
        {'id': 'child-fixture-parent', 'convId': 'child-fixture-conv', '_userId': 71,
         '_transientRuntime': not persistent, 'config': {'memoryEnabled': False, 'searchMode': 'off'}},
        model='fixture-model', tools=[], objective='fixture objective',
        event_sink=sink, abort_check=abort)


def _discard(child):
    from lib.tasks_pkg.manager import discard_task
    discard_task(child['id'])


def test_archived_policy_matches_real_conversation_child_inputs():
    from lib.tasks_pkg.child_invocation import snapshot_conversation_child_policy
    tools = [{'type': 'function', 'function': {'name': 'inspect'}}]
    parent = {'id': 'policy-parent', 'convId': 'policy-conv', '_userId': 71,
              '_pinned_provider_id': 'original-provider',
              '_transientRuntime': True,
              'config': {'temperature': 0.3, 'toolPermissions': {'shell': 'deny'},
                         '_executableToolCatalog': tools,
                         '_toolScriptSafeByName': {'inspect': False}}}
    archived = snapshot_conversation_child_policy(
        parent, model='fixture-model', tools=tools, project_path='/project',
        thinking_enabled=False)
    parent['_goalInvocationPolicy'] = archived
    parent['config']['toolPermissions']['shell'] = 'allow'
    parent['config']['temperature'] = 0.9
    parent['config']['_executableToolCatalog'] = []
    parent['_pinned_provider_id'] = 'different-provider'
    child = create_child_invocation(
        parent, model='fixture-model', tools=tools, objective='policy check',
        project_path='/project', thinking_enabled=False,
        conversation_messages=[{'role': 'user', 'content': 'policy check'}],
        event_sink=lambda event: None, abort_check=lambda: False)
    try:
        assert all(child['config'][key] == value
                   for key, value in archived['config'].items())
        assert child['_invocationConversationToolPolicy'] == archived['conversation_tool_policy']
        assert child['_invocationToolContracts'] == archived['tool_contracts']
        assert child['_invocationToolScriptSafeByName'] == archived['tool_script_safe']
        assert child['_pinned_provider_id'] == 'original-provider'
    finally:
        _discard(child)
    from lib.model_routing import ModelRoutingError
    with pytest.raises(ModelRoutingError, match='conflicts with the archived Goal'):
        create_child_invocation(
            parent, model='different-model', tools=tools, objective='policy check',
            project_path='/project', event_sink=lambda event: None,
            abort_check=lambda: False)


def test_native_child_owns_route_after_parent_settles(monkeypatch):
    from lib.agent_core.execution_session import (
        ExecutionPhase, execution_session_for_task,
    )
    from lib.llm_dispatch.provider_pin import get_pinned_provider
    from lib.model_routing import ModelRoutingError
    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.orchestrator._provider_binding import bind_provider_and_affinity
    from lib.llm_dispatch.provider_pin import set_pinned_provider
    import lib.model_routing as routing

    groups = []
    disposed = []

    def mint(*args, **kwargs):
        group = SimpleNamespace(
            pin_id=f'isolated-pin-{len(groups)}',
            candidates=[SimpleNamespace(offering={})],
        )
        groups.append(group)
        return group

    monkeypatch.setattr(routing, 'mint_routed_slot_group', mint)
    monkeypatch.setattr(routing, 'dispose_routed_slot_group', disposed.append)
    ref = {'creator_id': 'creator', 'model_id': 'fixture-model'}
    from lib.identity import PrincipalContext
    principal = PrincipalContext.user(subject_id='test:owner', owner_user_id=71)
    parent = create_task('route-parent', [{'role': 'user', 'content': 'parent'}],
                         {'modelRef': ref, 'model': 'fixture-model'},
                         principal=principal, transient=True)
    try:
        from lib.tasks_pkg.child_invocation import snapshot_conversation_child_policy
        parent['_goalInvocationPolicy'] = snapshot_conversation_child_policy(
            parent, model='fixture-model', tools=[])
        child = create_child_invocation(
            parent, model='fixture-model', tools=[], objective='child',
            event_sink=lambda event: None, abort_check=lambda: False)
        try:
            assert child['_pinned_provider_id'] == groups[1].pin_id
            assert child['_pinned_provider_id'] != parent['_pinned_provider_id']
            execution_session_for_task(parent).settle(ExecutionPhase.COMPLETED)
            assert disposed == [groups[0]]
            bind_provider_and_affinity(child, child['id'][:8])
            assert get_pinned_provider() == groups[1].pin_id
            with pytest.raises(ModelRoutingError) as error:
                create_child_invocation(parent, model='other-model', tools=[],
                                        objective='override', event_sink=lambda event: None,
                                        abort_check=lambda: False)
            assert error.value.kind == 'model_route_unavailable'
            assert len(groups) == 2
        finally:
            set_pinned_provider('')
            _discard(child)
        assert disposed == groups
    finally:
        _discard(parent)


@pytest.mark.parametrize('failure', [False, True])
def test_nested_execution_restores_parent_context_even_on_failure(monkeypatch, failure):
    from lib.tasks_pkg.orchestrator import _run
    from lib.llm_dispatch.provider_pin import get_pinned_provider, provider_pin, set_pinned_provider
    from lib.llm_dispatch.conv_affinity import conv_affinity, get_conv_affinity, set_conv_affinity
    from lib.log import bind_log_context, req_id, set_req_id
    child = _child()
    caller = threading.get_ident()
    def execute(task, *, policy):
        assert threading.get_ident() == caller
        set_pinned_provider('child-pin')
        set_conv_affinity('child-affinity')
        set_req_id('child-request')
        _run.finalize_task_lane(task, tid=task['id'][:8])
        if failure:
            raise RuntimeError('fixture failure')
    monkeypatch.setattr(_run, '_execute_task_invocation', execute)
    previous_request = req_id()
    try:
        set_req_id('parent-request')
        with provider_pin('parent-pin'), conv_affinity('parent-affinity'), bind_log_context(task_id='parent'):
            if failure:
                with pytest.raises(RuntimeError, match='fixture failure'):
                    invocation.execute_invocation(child, policy=child['_invocationPolicy'])
            else:
                invocation.execute_invocation(child, policy=child['_invocationPolicy'])
            assert get_pinned_provider() == 'parent-pin'
            assert get_conv_affinity() == 'parent-affinity'
            assert req_id() == 'parent-request'
    finally:
        set_req_id(previous_request)
        _discard(child)


def test_child_does_not_inherit_strict_billing_stop_admission(monkeypatch):
    """A child invocation is user-requested work, not optional background work.

    If the parent's turn entered strict_billing_stop_admission() for its own
    cheap side calls (title-gen, translate, optimizer, …), copy_context()
    would propagate that ContextVar into the child and the child's model
    dispatch would reject every slot with a recorded billing stop — the swarm
    "No slot available" failure (conv mu8e3bdwdqovh6). The child must reset
    it for its own execution while the parent's scope stays intact.
    """
    from lib.tasks_pkg.orchestrator import _run
    from lib.key_stats import (
        is_strict_billing_stop_admission, strict_billing_stop_admission)
    child = _child()
    seen = {}

    def execute(task, *, policy):
        seen['child_strict'] = is_strict_billing_stop_admission()
        _run.finalize_task_lane(task, tid=task['id'][:8])

    monkeypatch.setattr(_run, '_execute_task_invocation', execute)
    try:
        with strict_billing_stop_admission():
            invocation.execute_invocation(
                child, policy=child['_invocationPolicy'])
            # Parent's optional-work scope is untouched by the child's reset.
            assert is_strict_billing_stop_admission() is True
        assert seen['child_strict'] is False
    finally:
        _discard(child)


@pytest.mark.parametrize('entry', ['owner', 'compatibility', 'root'])
@pytest.mark.parametrize('corrupt', ['same_id', 'parent', 'turn', 'attempt', 'live_turn'])
def test_every_public_entry_rejects_parent_lifecycle_carriers(monkeypatch, entry, corrupt):
    from lib.tasks_pkg.orchestrator import _run
    child = _child()
    allocated_id = child['id']
    if corrupt == 'same_id':
        child['id'] = child['_parentTaskId']
    elif corrupt == 'parent':
        child['_parentTaskId'] = 'foreign-parent'
    else:
        child[{'turn': '_turnId', 'attempt': '_attemptId', 'live_turn': '_flowLiveTurn'}[corrupt]] = 'parent-authority'
    before = dict(child)
    monkeypatch.setattr(_run, '_execute_task_invocation',
                        lambda *a, **kw: pytest.fail('invalid child executed'))
    try:
        with pytest.raises(ValueError):
            if entry == 'root':
                _run.run_task(child)
            else:
                owner = invocation.execute_invocation if entry == 'owner' else _run.execute_task_invocation
                owner(child, policy=child['_invocationPolicy'])
        assert child == before
    finally:
        child['id'] = allocated_id
        _discard(child)


@pytest.mark.parametrize('failure', [None, RuntimeError, KeyboardInterrupt])
def test_child_routing_and_diagnostic_scope_is_local_and_released(monkeypatch, failure):
    from lib.llm_dispatch import conv_affinity as affinity
    from lib.tasks_pkg.orchestrator import _run
    from lib.tasks_pkg.orchestrator._provider_binding import bind_provider_and_affinity
    child = _child()
    scope = invocation.invocation_affinity_id(child)
    parent_scope = child['convId']
    with affinity.isolated_conv_affinity(parent_scope):
        affinity.record_conv_key(parent_scope, 'parent-key')
        affinity.record_pick_decision(preferred_key='parent-key', chosen_key='parent-key', fell_back=False)
        parent_pick = dict(affinity.get_pick_decision())
        def execute(task, *, policy):
            bind_provider_and_affinity(task, task['id'][:8])
            assert affinity.get_conv_affinity() == scope != parent_scope
            assert affinity.get_pick_decision() is None
            affinity.record_conv_key(scope, 'child-key', route_key='fixture-model')
            affinity.record_pick_decision(preferred_key=None, chosen_key='child-key', fell_back=True)
            _run.finalize_task_lane(task, tid=task['id'][:8])
            if failure:
                raise failure('fixture')
        monkeypatch.setattr(_run, '_execute_task_invocation', execute)
        try:
            if failure:
                with pytest.raises(failure):
                    _run.execute_task_invocation(child, policy=child['_invocationPolicy'])
            else:
                _run.execute_task_invocation(child, policy=child['_invocationPolicy'])
            assert affinity.get_conv_affinity() == parent_scope
            assert affinity.get_preferred_key(parent_scope) == 'parent-key'
            assert affinity.get_pick_decision() == parent_pick
            assert affinity.get_preferred_key(scope, route_key='fixture-model') is None
        finally:
            _discard(child)


def test_inherited_stop_reaches_blocked_tool_and_monitor_is_released(monkeypatch):
    import lib.tasks_pkg.child_invocation as owner
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    parent_stop = threading.Event()
    child = _child(abort=parent_stop.is_set)
    threads_before = {t.ident for t in threading.enumerate()}
    def execute(task, *, policy):
        parent_stop.set()
        assert task['abort_event'].wait(0.7)
        assert task['aborted']
        task.update(status='done', finishReason='aborted')
    monkeypatch.setattr(owner, 'execute_invocation', execute)
    run_child_invocation(child)
    assert chat_task_runtime.get(child['id']) is None
    assert not [t for t in threading.enumerate()
                if t.ident not in threads_before and t.name.startswith('child-abort-')]


def test_sink_failure_cannot_become_leaf_success(monkeypatch):
    import lib.tasks_pkg.child_invocation as owner
    from lib.tasks_pkg.manager import append_event
    child = _child(sink=lambda event: (_ for _ in ()).throw(RuntimeError('persist failed')))
    def execute(task, *, policy):
        # Some pre-existing optional observers catch callback errors. The
        # invocation terminal boundary must still reject successful delivery.
        try:
            append_event(task, {'type': 'delta', 'content': 'visible'})
        except RuntimeError:
            pass
        task.update(status='done', finishReason='stop')
    monkeypatch.setattr(owner, 'execute_invocation', execute)
    with pytest.raises(RuntimeError, match='evidence delivery failed'):
        run_child_invocation(child)
    assert child['aborted'] and child['abort_event'].is_set()


def test_child_evidence_failure_never_cancels_parent_or_sibling():
    parent_stop = threading.Event()
    child = _child(sink=lambda event: (_ for _ in ()).throw(RuntimeError('sink failed')),
                   abort=parent_stop.is_set)
    sibling = _child(abort=parent_stop.is_set)
    try:
        with pytest.raises(RuntimeError, match='sink failed'):
            invocation.deliver_invocation_event(child, {'type': 'delta'})
        assert child['aborted'] and child['abort_event'].is_set()
        assert not parent_stop.is_set()
        assert not invocation.invocation_abort_requested(sibling)
        assert not sibling['abort_event'].is_set()
    finally:
        _discard(child)
        _discard(sibling)


def test_inspector_persists_before_required_sink_and_uses_exact_owner(monkeypatch):
    from lib.tasks_pkg.manager import append_event
    import lib.tasks_pkg.event_log as log
    order = []
    child = _child(sink=lambda event: order.append(('sink', event['seq'])))
    # This spec owns evidence ordering, not durable archive registration.
    child['_invocationStorageFree'] = False
    monkeypatch.setattr(log, 'append_persistent_event',
                        lambda task, seq, event: order.append(('persist', task, seq)))
    try:
        append_event(child, {'type': 'messages_snapshot', 'kind': 'request',
                             'roundNum': 1, 'model': 'fixture-model', 'label': 'Request',
                             'messages': [{'role': 'user', 'content': 'go'}]})
        assert order == [('persist', child['_inspectorTaskId'], 0), ('sink', 0)]
    finally:
        _discard(child)


class _NeverAbort:
    def __init__(self):
        self.set_calls = 0

    def wait(self, timeout=None):
        return False

    def is_set(self):
        return False

    def set(self):
        self.set_calls += 1


def test_child_evidence_append_transient_stall_retries_before_sink(monkeypatch):
    """A deadline_elapsed from the authority must not poison the invocation.

    The append is idempotent on its natural (task, sequence) key, so the
    bounded replay runs before the ordered sink handoff — the observed
    2026-09-19 cascade killed a healthy 15-round conversation at round 16
    from a single transient storage stall.
    """
    import lib.tasks_pkg.event_log as log
    from lib.storage.errors import StorageError
    order = []
    child = _child(sink=lambda event: order.append(('sink', event['seq'])))
    child['_invocationStorageFree'] = False
    child['abort_event'] = _NeverAbort()
    attempts = []
    def flaky_append(task_id, seq, event):
        attempts.append(seq)
        if len(attempts) == 1:
            raise StorageError('database_timeout', 'deadline_elapsed', False)
        order.append(('persist', task_id, seq))
    monkeypatch.setattr(log, 'append_persistent_event', flaky_append)
    try:
        invocation.deliver_invocation_event(
            child, {'type': 'messages_snapshot', 'seq': 7})
        assert attempts == [7, 7]
        assert order == [('persist', child['_inspectorTaskId'], 7), ('sink', 7)]
        assert not child['aborted']
        assert child.get('_invocationDeliveryError') is None
    finally:
        _discard(child)


def test_child_evidence_append_transient_exhaustion_poisons_once(monkeypatch):
    import lib.tasks_pkg.event_log as log
    from lib.storage.errors import StorageError
    child = _child()
    child['_invocationStorageFree'] = False
    child['abort_event'] = _NeverAbort()
    attempts = []
    def always_down(task_id, seq, event):
        attempts.append(seq)
        raise StorageError('database_timeout', 'deadline_elapsed', False)
    monkeypatch.setattr(log, 'append_persistent_event', always_down)
    try:
        with pytest.raises(StorageError, match='deadline_elapsed'):
            invocation.deliver_invocation_event(
                child, {'type': 'messages_snapshot', 'seq': 3})
        assert attempts == [3] * 4
        assert child['aborted'] and child['abort_event'].set_calls == 1
        # The failure stays latched: a later delivery fails fast instead of
        # reordering evidence around the lost handoff.
        with pytest.raises(RuntimeError, match='previously failed'):
            invocation.deliver_invocation_event(
                child, {'type': 'messages_snapshot', 'seq': 4})
        assert attempts == [3] * 4
    finally:
        _discard(child)


def test_child_evidence_append_non_transient_failure_is_not_retried(monkeypatch):
    import lib.tasks_pkg.event_log as log
    child = _child()
    child['_invocationStorageFree'] = False
    child['abort_event'] = _NeverAbort()
    attempts = []
    def verdict(task_id, seq, event):
        attempts.append(seq)
        raise RuntimeError('attempt is no longer live')
    monkeypatch.setattr(log, 'append_persistent_event', verdict)
    try:
        with pytest.raises(RuntimeError, match='no longer live'):
            invocation.deliver_invocation_event(
                child, {'type': 'messages_snapshot', 'seq': 5})
        assert attempts == [5]
    finally:
        _discard(child)


def test_empty_child_capabilities_never_assemble_root_tools(monkeypatch):
    from lib.tasks_pkg.orchestrator import _tool_assembly_prep as assembly
    child = _child()
    monkeypatch.setattr(assembly, '_assemble_tool_list', lambda *a, **kw: pytest.fail('root inventory'))
    try:
        assert assembly.assemble_round_tools({}, child, {}) == ([], False)
        assert child['_executable_tool_catalog'] == []
        assert child['_tool_gateway_names'] == []
    finally:
        _discard(child)


def test_conversation_child_keeps_root_tool_discovery_but_not_human_approval(monkeypatch):
    from lib.tasks_pkg.orchestrator import _tool_assembly_prep as assembly
    from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names
    def tool(name):
        return {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}
    visible = [tool('read_files'), tool('spawn_agents')]
    config = {
        'compaction': {'steps': ['compact_tool_results']},
        '_executableToolCatalog': visible + [tool('fixture_hidden'), tool('ask_human')],
        '_toolSearchMode': 'on', '_toolExecutionScope': 'available',
        '_toolScriptSafeByName': {'fixture_hidden': True},
        '_executableToolNamespaceByName': {'fixture_hidden': 'fixture'},
        '_executableToolSearchTextByName': {'fixture_hidden': 'synthetic evidence'},
    }
    parent = {'id': 'tool-policy-parent', '_userId': 71, '_transientRuntime': True,
              'config': config}
    child = create_child_invocation(
        parent, model='fixture-model', tools=visible, objective='Verify',
        conversation_messages=[{'role': 'user', 'content': 'Verify'}],
        event_sink=lambda event: None, abort_check=lambda: False)
    monkeypatch.setattr(assembly, '_assemble_tool_list', lambda *a, **kw: pytest.fail('reassembled authority'))
    try:
        returned, enabled = assembly.assemble_round_tools(child['config'], child, {})
        assert enabled and returned == visible
        assert _known_tool_names(child) == {
            'read_files', 'spawn_agents', 'fixture_hidden', 'search_tools',
            'execute_tools', 'execute_program'}
        assert child['_toolSearchMode'] == 'on'
        assert child['_toolScriptSafeByName']['fixture_hidden']
        assert child['_executableToolNamespaceByName']['fixture_hidden'] == 'fixture'
        assert {'search_tools', 'execute_tools'} <= child['_toolContractDocumentsByName'].keys()
        assert child['config']['humanGuidanceEnabled'] is False
        child['config']['compaction']['steps'].clear()
        assert config['compaction']['steps'] == ['compact_tool_results']
        child['_executable_tool_catalog'].clear()
        assert len(config['_executableToolCatalog']) == 4
    finally:
        _discard(child)


def test_child_approval_is_explicitly_unavailable_never_auto_granted(monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _approval as approval
    child = _child()
    row = {'roundNum': 1, 'toolCallId': 'approval-fixture', 'toolName': 'write_file'}
    monkeypatch.setattr(approval, 'request_write_approval',
                        lambda *a, **kw: pytest.fail('invisible human gate'))
    monkeypatch.setattr(approval, 'append_event',
                        lambda *a, **kw: pytest.fail('unanswerable approval UI'))
    monkeypatch.setattr(approval, '_finalize_tool_round',
                        lambda task, rn, entry, meta: entry.update(status='rejected'))
    try:
        approved, detail = approval._handle_approval(
            child, 'write_file', {'path': 'fixture.txt'}, 1, row, None, 0, 'm')
        assert not approved and 'human approval unavailable' in detail
        assert row['status'] == 'rejected'
        assert row['rejection']['kind'] == 'approval_unavailable'
        assert not row['rejection']['retryable']
        assert not child.get('_tool_approval_receipts')
    finally:
        _discard(child)


def test_production_snapshot_short_log_id_does_not_change_evidence_owner(monkeypatch):
    from lib.tasks_pkg.orchestrator import _messages_snapshot as snapshots
    rows = []
    monkeypatch.setattr(snapshots, 'append_event', lambda task, event: rows.append(event))
    task = {'id': 'full-task-id-not-the-log-prefix', 'config': {}}
    snapshots.emit_messages_snapshot_event(
        task, [], tid=task['id'][:8], round_num=0, model='m',
        thinking_enabled=False, thinking_depth='low', preset='', temperature=1,
        max_tokens=1000, response_format=None, tools=[], prepared_messages=[])
    assert rows[0]['inspectorRef']['taskId'] == task['id']
    assert rows[0]['roundNum'] == 1


def test_actual_invocation_runs_mature_prep_loop_and_terminal_owner(monkeypatch):
    """Only external/context acquisition and provider transport are fixtures."""
    import lib.tasks_pkg.orchestrator._run as runner
    import lib.tasks_pkg.orchestrator._root_agent_loop as loop
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.manager import append_event
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.agent_core.execution_session import execution_session_for_task
    rows = []
    child = _child(sink=lambda event: rows.append(dict(event)))
    monkeypatch.setattr(runner, 'start_prefetches', lambda *a, **kw: None)
    monkeypatch.setattr(runner, 'stop_prefetches', lambda *a, **kw: None)
    monkeypatch.setattr(runner, 'maybe_run_memory_prefetch', lambda **kw: None)
    monkeypatch.setattr(runner, 'inject_context_and_emit_chips', lambda **kw: None)
    for name in ('run_turn_prelude', 'setup_project_context', 'check_autopilot_kick'):
        monkeypatch.setattr(runner, name, lambda *a, **kw: pytest.fail('parent lifecycle'))
    monkeypatch.setattr(loop, 'drain_and_inject_inbox', lambda **kw: pytest.fail('parent inbox'))
    monkeypatch.setattr(loop, 'run_round_message_hygiene', lambda *a, **kw: None)
    monkeypatch.setattr(loop, 'build_round_request', lambda task, state, messages, tools, **kw: (tools, {}))
    monkeypatch.setattr(loop, 'build_stream_accumulator', lambda *a, **kw: SimpleNamespace(
        announced_tc_map={}, close=lambda **kw: None))
    monkeypatch.setattr(loop, 'stamp_round_cache_accounting', lambda *a, **kw: None)
    monkeypatch.setattr(loop, 'settle_stream_accumulator', lambda *a, **kw: None)
    def dispatch(task, state, *a, **kw):
        state.assistant_msg = {'role': 'assistant', 'content': 'verified answer'}
        state.last_finish_reason = 'stop'
        state.last_usage = {'total_tokens': 3}
        state.last_stream_result = ProviderStreamResult.from_legacy(
            state.assistant_msg, 'stop', state.last_usage)
        task['content'] = 'verified answer'
        append_event(task, {'type': 'delta', 'content': task['content']})
        return 'proceed'
    monkeypatch.setattr(loop, 'run_llm_call_with_fallback', dispatch)
    run_child_invocation(child)
    assert child['finishReason'] == 'stop'
    assert child['streamState'] == 'provider_finished'
    assert child['content'] == 'verified answer'
    assert child['messages'][-1]['content'] == 'verified answer'
    assert [row['type'] for row in rows].count('done') == 1
    assert child['segments'][-1]['text'] == 'verified answer'
    assert chat_task_runtime.get(child['id']) is None
    assert execution_session_for_task(child).is_terminal


@pytest.mark.parametrize('options', [
    {'timeout_seconds': -1}, {'timeout_seconds': float('nan')},
    {'max_429_attempts': 0}, {'max_429_attempts': 4097},
])
def test_invalid_child_budget_rejected_before_allocation(monkeypatch, options):
    import lib.tasks_pkg.manager as manager
    monkeypatch.setattr(manager, 'create_task', lambda *a, **kw: pytest.fail('invalid policy allocated a child'))
    with pytest.raises(ValueError):
        create_child_invocation({'id': 'parent', '_userId': 71, 'config': {}},
            model='fixture-model', tools=[], objective='inspect',
            event_sink=lambda event: None, abort_check=lambda: False, **options)


def test_monitor_starts_with_child_and_propagates_parent_stop_without_capacity_wait(monkeypatch):
    import lib.tasks_pkg.child_invocation as owner
    stop, entered = threading.Event(), threading.Event()
    child = _child(abort=stop.is_set)
    def execute(task, **kwargs):
        entered.set()
        assert task['abort_event'].wait(3)
        assert task['aborted']
    monkeypatch.setattr(owner, 'execute_invocation', execute)
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(run_child_invocation(child)))
    try:
        thread.start()
        assert entered.wait(1)
        stop.set()
        thread.join(3)
        assert not thread.is_alive() and outcome == [child]
    finally:
        stop.set()
        thread.join(3)


@pytest.mark.parametrize('explicit_path', ['', '/canonical'])
def test_child_borrows_isolated_execution_roots_not_delivery_ownership(explicit_path):
    from lib.project_mod.config import clear_conv_state, get_conv_roots, set_conv_roots
    from lib.tasks_pkg.workspace_delivery import (
        execution_root_key, execution_repositories, route_project_tool_args,
        settle_task_workspaces,
    )
    parent = {
        'id': 'workspace-parent', 'convId': 'workspace-conversation', '_userId': 71,
        '_transientRuntime': True,
        'config': {'projectPath': '/private-task',
                   'projectPaths': ['/private-task', '/private-task/reference'],
                   'readOnlyPaths': ['/private-task/reference'], 'memoryEnabled': False},
        '_workspaceDelivery': {'repositories': [
            {'canonicalRoot': '/canonical', 'workspacePath': '/private-task'}]},
    }
    child = create_child_invocation(
        parent, model='fixture-model', tools=[], objective='inspect workspace',
        project_path=explicit_path, event_sink=lambda event: None, abort_check=lambda: False)
    grandchild = None
    try:
        assert child['config']['projectPath'] == '/private-task'
        assert child['config']['readOnlyPaths'] == ['/private-task/reference']
        assert '_workspaceDelivery' not in child
        assert settle_task_workspaces(child) is None
        assert execution_repositories(child) == execution_repositories(parent)
        assert execution_repositories(child) is not execution_repositories(parent)
        assert execution_root_key(child) == child['id']
        assert execution_root_key(child) != execution_root_key(parent)
        args = route_project_tool_args(child, 'read_files', {'path': 'canonical:src/a.py'})
        assert args['path'] == '/private-task/src/a.py'
        set_conv_roots(child['id'], child['config']['projectPath'],
                       extras=child['config']['projectPaths'],
                       readonly_paths=child['config']['readOnlyPaths'])
        assert {row['path'] for row in get_conv_roots(child['id']).values()} == {
            '/private-task', '/private-task/reference'}
        grandchild = create_child_invocation(
            child, model='fixture-model', tools=[], objective='nested inspection',
            event_sink=lambda event: None, abort_check=lambda: False)
        assert grandchild['config']['projectPath'] == '/private-task'
        assert execution_root_key(grandchild) == grandchild['id']
        assert execution_repositories(grandchild) == execution_repositories(parent)
        assert '_workspaceDelivery' not in grandchild
        execution_repositories(child)[0]['workspacePath'] = '/not-parent-owned'
        assert execution_repositories(parent)[0]['workspacePath'] == '/private-task'
        assert execution_repositories(grandchild)[0]['workspacePath'] == '/private-task'
    finally:
        clear_conv_state(child['id'])
        _discard(child)
        if grandchild:
            clear_conv_state(grandchild['id'])
            _discard(grandchild)


def test_archived_goal_policy_stays_frozen_when_child_borrows_isolated_roots():
    import copy
    from lib.project_mod.config import clear_conv_state
    from lib.tasks_pkg.child_invocation import snapshot_conversation_child_policy
    from lib.tasks_pkg.workspace_delivery import (
        execution_repositories, route_project_tool_args, settle_task_workspaces,
    )

    tools = [{'type': 'function', 'function': {'name': 'inspect'}}]
    parent = {
        'id': 'archived-isolated-parent', 'convId': 'archived-isolated-conversation',
        '_userId': 71, '_transientRuntime': True,
        '_pinned_provider_id': 'archived-provider',
        '_toolScriptSafeByName': {'inspect': False},
        'config': {
            'projectPath': '/canonical', 'projectPaths': ['/canonical/reference'],
            'readOnlyPaths': ['/canonical/reference'], 'memoryEnabled': False,
            'toolPermissions': {'shell': 'deny'}, 'temperature': 0.3,
            '_executableToolCatalog': tools,
        },
        '_workspaceDelivery': {'repositories': [
            {'canonicalRoot': '/canonical', 'workspacePath': '/private-task'}]},
    }
    archived = snapshot_conversation_child_policy(
        parent, model='fixture-model', tools=tools, project_path='/canonical',
        thinking_enabled=False)
    archived_before = copy.deepcopy(archived)
    parent['_goalInvocationPolicy'] = archived
    parent['config']['toolPermissions']['shell'] = 'allow'
    parent['config']['temperature'] = 0.9
    parent['config']['_executableToolCatalog'] = []
    parent['_toolScriptSafeByName'] = {'inspect': True}

    # Binding remaps the live parent after archiving its canonical policy.
    from lib.project_mod.config import build_root_policy
    parent['_workspaceRootPolicy'] = build_root_policy(
        '/canonical', ['/canonical/reference'], ['/canonical/reference'])
    parent['_workspaceRootPolicy']['primaryPath'] = '/private-task'
    for state in parent['_workspaceRootPolicy']['roots'].values():
        state['path'] = state['path'].replace('/canonical', '/private-task', 1)
    parent['config']['projectPath'] = '/private-task'
    parent['config']['projectPaths'] = ['/private-task/reference']
    parent['config']['readOnlyPaths'] = ['/private-task/reference']
    parent['_pinned_provider_id'] = 'changed-provider'
    child = create_child_invocation(
        parent, model='fixture-model', tools=tools, objective='archived isolated child',
        project_path='/canonical', thinking_enabled=False,
        conversation_messages=[{'role': 'user', 'content': 'preserve frozen authority'}],
        event_sink=lambda event: None, abort_check=lambda: False)
    try:
        assert child['config']['projectPath'] == '/private-task'
        assert child['config']['projectPaths'] == ['/private-task/reference']
        assert child['config']['readOnlyPaths'] == ['/private-task/reference']
        path_fields = {'projectPath', 'projectPaths', 'readOnlyPaths'}
        assert all(child['config'][key] == value for key, value in archived['config'].items()
                   if key not in path_fields)
        assert child['_pinned_provider_id'] == 'archived-provider'
        assert child['_invocationConversationToolPolicy'] == archived['conversation_tool_policy']
        assert child['_invocationToolContracts'] == archived['tool_contracts']
        assert child['_invocationToolScriptSafeByName'] == {'inspect': False}
        assert archived == archived_before
        assert execution_repositories(child) == execution_repositories(parent)
        assert execution_repositories(child) is not execution_repositories(parent)
        assert '_workspaceDelivery' not in child
        assert settle_task_workspaces(child) is None
        assert route_project_tool_args(child, 'read_files', {
            'path': 'canonical:reference/notes.txt'})['path'] == '/private-task/reference/notes.txt'
    finally:
        clear_conv_state(parent['id'])
        clear_conv_state(child['id'])
        _discard(child)


def test_all_child_monitors_start_despite_old_global_worker_setting(monkeypatch):
    import lib.tasks_pkg.child_invocation as owner
    monkeypatch.setenv('TOFU_SWARM_GLOBAL_WORKERS', '1')
    release = threading.Event()
    children = [_child() for _ in range(8)]
    entered = {child['id']: threading.Event() for child in children}
    errors = []
    def execute(task, **kwargs):
        entered[task['id']].set()
        assert release.wait(5)
    def run(child):
        try:
            run_child_invocation(child)
        except Exception as error:
            errors.append(error)
    monkeypatch.setattr(owner, 'execute_invocation', execute)
    threads = [threading.Thread(target=run, args=(child,)) for child in children]
    try:
        for thread in threads:
            thread.start()
        assert all(event.wait(2) for event in entered.values())
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
        for child in children:
            _discard(child)
    assert not errors
    assert not any(thread.is_alive() for thread in threads)
