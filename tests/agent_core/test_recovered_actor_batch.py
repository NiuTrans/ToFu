"""Whole actor accounting waits for one durable sink, without issuing leases."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading

import pytest

from lib.agent_core.account_frontier import validate_frontier, validate_recovery_transition
from lib.agent_core.execution_budget import ModelBudgetExceeded, close_execution_budget
from tests.agent_core.test_recovered_actor_prefix import child, root, source, spend

pytestmark = pytest.mark.unit


def prepared(sink=None, maximum=1024 * 1024, *, spent_before_binding=False):
    saved = source()
    task, account = root('target', saved['snapshot']['limits'])
    account.restore_pending_frontier(task, saved, owner=7)
    if spent_before_binding:
        spend(task, 1)
    ordinary, committed = [], []
    account.bind_account_frontier_observer(lambda cut: ordinary.append(deepcopy(cut)),
        actor_rebind_observer=sink or (lambda cuts: committed.append(deepcopy(cuts))),
        actor_rebind_bytes=maximum)
    a, b = child(task, 'target-a'), child(task, 'target-b')
    return task, account, [(a, 'source-a'), (b, 'source-b')], ordinary, committed


def test_whole_batch_preserves_every_paid_counter_and_uses_one_ack():
    task, account, bindings, ordinary, committed = prepared()
    before = deepcopy(ordinary[-1])
    account.rebind_all_actor_prefixes(bindings, owner=7)
    assert account.consumption() == (36, 5)
    assert len(committed) == 1 and len(committed[0]) == 2 and ordinary[-1] == before
    for cut in committed[0]:
        validate_frontier(cut)
        validate_recovery_transition(before, cut)
        assert cut['version'] == before['version'] + 1
        assert cut['snapshot'] == before['snapshot']
        before = cut
    assert before['recovery']['pending_actors'] == {}
    for task, source_id in bindings:
        assert account.recovered_actor_prefix_tokens(task, owner=7) == source()['actors'][source_id]['tokens']
    spend(bindings[0][0], 3, 1)
    account.rebind_all_actor_prefixes(list(reversed(bindings)), owner=7)
    assert account.consumption() == (39, 6) and len(committed) == 1


@pytest.mark.parametrize('damage', ['missing', 'duplicate_source', 'duplicate_target', 'clone', 'foreign', 'spent', 'extra_child', 'malformed', 'partial_single'])
def test_invalid_group_refuses_before_moving_any_actor(damage):
    task, account, bindings, ordinary, committed = prepared(spent_before_binding=damage == 'spent')
    if damage == 'missing': bindings.pop()
    elif damage == 'duplicate_source': bindings[1] = (bindings[1][0], 'source-a')
    elif damage == 'duplicate_target': bindings[1] = (bindings[0][0], 'source-b')
    elif damage == 'clone': bindings[1] = (dict(bindings[1][0]), 'source-b')
    elif damage == 'foreign': bindings[1][0]['_userId'] = 8
    elif damage == 'extra_child': child(task, 'unmatched')
    elif damage == 'malformed': bindings[1] = (bindings[1][0],)
    before = (account.consumption(), deepcopy(account._pending_actor_prefixes), deepcopy(ordinary))
    with pytest.raises(ValueError):
        if damage == 'partial_single':
            account.rebind_actor_prefix(bindings[0][0], source_task_id='source-a', owner=7)
        else:
            account.rebind_all_actor_prefixes(bindings, owner=7)
    assert (account.consumption(), account._pending_actor_prefixes, ordinary) == before
    assert account._rebound_actor_sources == {} and not committed


def test_pending_atomic_group_fences_root_and_children_until_acknowledgement():
    task, account, bindings, ordinary, committed = prepared()
    for actor in [task, *(row[0] for row in bindings)]:
        with pytest.raises(ModelBudgetExceeded, match='execution_account_frontier_unavailable'):
            account.reserve_model(actor, input_tokens=0, max_output_tokens=1)
        assert account.reserve_tool(actor) == 'execution_account_frontier_unavailable'
    assert account.consumption() == (36, 5) and not committed
    account.rebind_all_actor_prefixes(bindings, owner=7)
    assert account.reason() is None
    spend(task, 1, 1)
    assert account.consumption() == (37, 6)


@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_failed_or_unknown_whole_ack_keeps_debt_and_fences_every_recipient(failure):
    delivered = []
    def sink(cuts):
        delivered.append(deepcopy(cuts))
        raise failure('lost durable response')
    _, account, bindings, _, _ = prepared(sink)
    with pytest.raises(failure): account.rebind_all_actor_prefixes(bindings, owner=7)
    assert len(delivered) == 1 and account.consumption() == (36, 5)
    assert delivered[0][-1]['recovery']['pending_actors'] == {}
    with pytest.raises(ValueError): account.rebind_all_actor_prefixes(bindings, owner=7)
    for task, _ in bindings:
        with pytest.raises(ModelBudgetExceeded): account.reserve_model(task, input_tokens=0, max_output_tokens=1)
        assert account.reserve_tool(task) == 'execution_account_frontier_unavailable'
        with pytest.raises(ValueError): account.recovered_actor_prefix_tokens(task, owner=7)
        close_execution_budget(task)
    assert account.consumption() == (36, 5) and not account.actuals_complete()


def test_byte_allowance_refuses_before_any_durable_batch_and_keeps_global_debt():
    _, account, bindings, ordinary, committed = prepared(maximum=10)
    before = deepcopy(ordinary)
    with pytest.raises(ValueError, match='byte allowance'): account.rebind_all_actor_prefixes(bindings, owner=7)
    assert not committed and ordinary == before and account.consumption() == (36, 5)
    assert account.reason() == 'execution_account_frontier_unavailable'


def test_batch_without_prebound_atomic_observer_is_never_published_as_separate_events():
    task, account = root('target')
    account.restore_pending_frontier(task, source(), owner=7)
    cuts = []
    account.bind_account_frontier_observer(cuts.append)
    bindings = [(child(task, 'target-a'), 'source-a'), (child(task, 'target-b'), 'source-b')]
    before = deepcopy(cuts)
    with pytest.raises(ValueError): account.rebind_all_actor_prefixes(bindings, owner=7)
    assert cuts == before and account._rebound_actor_sources == {}


def test_durability_callback_cannot_reenter_admission_or_observe_unacknowledged_fees():
    state = {}
    def sink(cuts):
        account, task = state['account'], state['task']
        assert account.reason() == 'execution_account_frontier_unavailable'
        with pytest.raises(ModelBudgetExceeded): account.reserve_model(task, input_tokens=0, max_output_tokens=1)
        assert account.reserve_tool(task) == 'execution_account_frontier_unavailable'
        with pytest.raises(ValueError): account.recovered_actor_prefix_tokens(task, owner=7)
        assert len(cuts) == 2
    _, account, bindings, _, _ = prepared(sink)
    state.update(account=account, task=bindings[0][0])
    account.rebind_all_actor_prefixes(bindings, owner=7)
    assert account.reason() is None and account.recovered_actor_prefix_tokens(bindings[0][0], owner=7) == 11


def test_concurrent_complete_contenders_publish_once():
    _, account, bindings, _, committed = prepared()
    start = threading.Barrier(2)
    def transfer():
        start.wait()
        account.rebind_all_actor_prefixes(bindings, owner=7)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(transfer) for _ in range(2)]
        for future in futures: future.result()
    assert len(committed) == 1 and account.consumption() == (36, 5)


@pytest.mark.parametrize('options', [dict(actor_rebind_bytes=1), dict(actor_rebind_observer=lambda cuts: None),
    dict(actor_rebind_observer=False, actor_rebind_bytes=1), dict(actor_rebind_observer=lambda cuts: None, actor_rebind_bytes=True),
    dict(actor_rebind_observer=lambda cuts: None, actor_rebind_bytes=0)])
def test_atomic_observer_requires_its_explicit_positive_bound(options):
    _, account = root('target')
    with pytest.raises(ValueError): account.bind_account_frontier_observer(lambda cut: None, **options)
    assert account._account_frontier_observer is None
