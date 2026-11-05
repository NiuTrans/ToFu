"""Sibling execute_program / execute_tools calls of one response overlap.

Owner decision 2026-11-04 (docs/contracts/TOOL_EXECUTION_POLICY.md, "Sibling
gateway calls run concurrently"): a gateway call no longer holds a task-wide
lock across its program or batch. Exactly-once is scoped to the receipt key,
and a child that ``tool_dispatch/_ordering`` orders (same-target file,
desktop, memory and browser-tab mutations, ``todo_write``) waits until every
earlier-issued sibling gateway call of the response has settled.

These specs run real JavaScript programs through the real gateway handler and
dispatch pipeline and replace only the leaf executor, so a task-wide lock
anywhere on the path breaks a rendezvous instead of letting siblings meet.

An occurrence holds its place until the worker that runs it returns, so an
expired collection deadline never releases an earlier program that may still
mutate. Only occurrences the runtime will run take a place: one the native
runtime refuses without a callback, or whose callback fails first, never
holds back a later sibling.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

import pytest

from lib.tasks_pkg.handlers import tool_gateway
from lib.tools.gateway import EXECUTE_PROGRAM_NAME, EXECUTE_TOOLS_NAME

pytestmark = pytest.mark.unit

_CFG = {'autoApply': True, 'tools': {'programmaticCalling': 'on'}}
_WAIT = 10


def _schema(name, **properties):
    return {'type': 'function', 'function': {
        'name': name, 'description': name,
        'parameters': {'type': 'object', 'properties': properties}}}


_CATALOG = [
    _schema('rendezvous_read'), _schema('hold_read'), _schema('probe_read'),
    _schema('nested_gateway_probe'),
    _schema('write_file', path={'type': 'string'}, content={'type': 'string'}),
]


class _Leaves:
    """Scripted leaf tools of this test's own tasks plus one ordered enter log.

    Only tasks the test created are scripted, so a worker left behind by
    another (failed) test can never satisfy this test's events.
    """

    def __init__(self):
        self.behaviors = {}
        self.tasks = set()
        self.entered = {}
        self.log = []
        self._lock = threading.Lock()

    def event(self, name):
        with self._lock:
            return self.entered.setdefault(name, threading.Event())

    def mark(self, name):
        with self._lock:
            self.log.append(name)
        self.event(name).set()

    def count(self, name):
        with self._lock:
            return self.log.count(name)


@pytest.fixture
def leaves(monkeypatch):
    import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round

    scripted = _Leaves()
    real_execute = pipeline._execute_tool_one

    def execute(task, tc, name, call_id, arguments, rn, row, cfg, project_path,
                project_enabled, all_tools=None):
        behavior = (scripted.behaviors.get(name)
                    if task.get('id') in scripted.tasks else None)
        if behavior is None:  # gateway calls keep their real handler
            return real_execute(task, tc, name, call_id, arguments, rn, row, cfg,
                                project_path, project_enabled, all_tools=all_tools)
        scripted.mark(name)
        value = behavior(task, dict(arguments))
        _finalize_tool_round(task, rn, row, [{
            'toolName': name, 'title': name, 'snippet': 'ok', 'source': 'Test',
            'fetched': True, 'fetchedChars': 2}])
        return call_id, json.dumps(value), False

    monkeypatch.setattr(heartbeat, '_execute_tool_one', execute)
    monkeypatch.setattr(pipeline, '_execute_tool_one', execute)
    return scripted


@pytest.fixture
def make_task(leaves):
    created = []

    def make():
        task = {
            'id': f'sibling-gateway-{uuid.uuid4().hex[:12]}', '_userId': 1,
            'convId': 'cv-sibling-gateway', 'status': 'running', 'aborted': False,
            'abort_event': threading.Event(), 'model': 'test-model',
            'config': {'tools': {'resultEnvelope': 'legacy'}}, 'events': [],
            'events_lock': threading.Lock(), '_dispatch_heartbeat': 0.0,
            '_t_last_event': 0.0, '_attended': False, 'toolRounds': [],
            '_executable_tool_catalog': list(_CATALOG),
        }
        created.append(task['id'])
        leaves.tasks.add(task['id'])
        return task

    yield make
    from lib.agent_core.admission import fire_terminal_callbacks
    for task_id in created:
        fire_terminal_callbacks(task_id)


def _program_item(task, call_id, code, number):
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    arguments = {'code': code}
    _number, row, _event = _build_tool_round_entry(
        EXECUTE_PROGRAM_NAME, dict(arguments), call_id, json.dumps(arguments),
        number, False)
    row['llmRound'] = 0
    task['toolRounds'].append(row)
    tc = {'id': call_id, 'type': 'function', 'function': {
        'name': EXECUTE_PROGRAM_NAME, 'arguments': json.dumps(arguments)}}
    return tc, EXECUTE_PROGRAM_NAME, call_id, dict(arguments), row['roundNum'], row, None


class _Round:
    """One provider response of sibling programs, dispatched on a thread."""

    def __init__(self, task, programs):
        from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
        self.task = task
        self.items = [_program_item(task, call_id, code, number)
                      for number, (call_id, code) in enumerate(programs, 1)]
        self.messages = []
        self.failure = []
        self.done = threading.Event()

        def run():
            try:
                execute_tool_pipeline(
                    task, self.items, cfg=dict(_CFG), project_path=None,
                    project_enabled=False, tool_list=task['_executable_tool_catalog'],
                    messages=self.messages, all_search_results_text=[], round_num=0,
                    model='test-model')
            except BaseException as error:  # surfaced by finish()
                self.failure.append(error)
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def finish(self):
        assert self.done.wait(_WAIT * 2), 'the sibling gateway round never settled'
        assert not self.failure, self.failure
        return {row['tool_call_id']: row['content'] for row in self.messages
                if row.get('role') == 'tool'}


def _runs(task):
    return {run['gatewayCallId']: run for run in task.get('programRuns') or []}


def _row_status(task, call_id):
    return next(row.get('status') for row in task['toolRounds']
                if row.get('toolCallId') == call_id)


def _until(predicate, timeout=_WAIT):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


_LATER_WRITE = ('await tools.invoke("probe_read", {}); '
                'await tools.invoke("write_file", {path: "notes.txt", content: "b"}); '
                'return "b";')


def _native_round(leaves, calls, callbacks, *, permissions=None):
    """Settle one native response, then deliver only ``callbacks``.

    ``callbacks`` maps a model call ID to the (name, arguments) its native
    callback carries; a call left out is one the native runtime never ran. A
    round still waiting at the deadline is stopped, as the native host would.
    """
    from lib.agent_core.admission import fire_terminal_callbacks
    from lib.agent_core.native_process import (
        NativeToolOrder, RoundRequest, RoundSettlement, ToolRequest,
    )
    from lib.observability import InstrumentedThreadPoolExecutor
    from lib.tasks_pkg.native_gateway import NativeGateway
    from tests.tasks_pkg.test_native_gateway import principal, task as native_task

    owner = native_task()
    owner['id'] = f'native-sibling-{uuid.uuid4().hex[:12]}'
    leaves.tasks.add(owner['id'])
    owner['_executable_tool_catalog'] = list(_CATALOG)
    owner['_tool_gateway_names'] = [EXECUTE_PROGRAM_NAME, EXECUTE_TOOLS_NAME]
    definitions = [
        _schema(EXECUTE_PROGRAM_NAME, code={'type': 'string'}),
        _schema(EXECUTE_TOOLS_NAME, calls={'type': 'array'}, execution={'type': 'string'})]
    permissions = permissions or {EXECUTE_PROGRAM_NAME: 'read', EXECUTE_TOOLS_NAME: 'read'}

    async def run(executor):
        gateway = NativeGateway(
            principal=principal(), task=owner, executor=executor, configuration=dict(_CFG),
            tool_list=definitions, permissions=permissions,
            project_path=None, project_enabled=False, model='fixture',
            round_source=lambda: 0, decision_policy=None, pending_calls=16,
            pending_bytes=1 << 20)
        gateway.select_concurrent_dispatch()
        try:
            gateway.prepare_response(RoundSettlement(
                RoundRequest(principal(), owner['id'], 'round-0', 0, 0),
                {'kind': 'returned', 'model_calls': calls}, None, NativeToolOrder()))
            requests = [gateway.execute(ToolRequest(
                principal(), owner['id'], f'run-{call_id}', name, arguments, call_id,
                model_round=0, model_call_id=call_id))
                for call_id, (name, arguments) in callbacks.items()]
            try:
                return await asyncio.wait_for(
                    asyncio.gather(*requests, return_exceptions=True), _WAIT)
            except asyncio.TimeoutError:
                return 'stopped at the deadline'
        finally:
            gateway.close()

    try:
        with InstrumentedThreadPoolExecutor(
                max_workers=1, metric_pool='tool',
                thread_name_prefix='native-sibling-programs') as executor:
            return asyncio.run(run(executor)), owner
    finally:
        fire_terminal_callbacks(owner['id'])


def _native_call(call_id, arguments, *, name=EXECUTE_PROGRAM_NAME):
    return {'id': call_id, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments)}}


def test_sibling_programs_overlap_children_that_meet_on_a_barrier(leaves, make_task):
    """Two programs whose reads must meet: a task-wide lock breaks the barrier."""
    barrier = threading.Barrier(2)
    leaves.behaviors['rendezvous_read'] = lambda task, _args: (
        barrier.wait(_WAIT), {'met': True})[1]
    task = make_task()
    code = 'return await tools.invoke("rendezvous_read", {});'

    replies = _Round(task, [('program-a', code), ('program-b', code)]).finish()

    assert not barrier.broken, 'sibling programs still execute one at a time'
    assert leaves.count('rendezvous_read') == 2
    runs = _runs(task)
    assert [runs[call_id]['status'] for call_id in ('program-a', 'program-b')] == [
        'completed', 'completed']
    assert runs['program-a']['result'] == runs['program-b']['result'] == {'met': True}
    assert list(replies) == ['program-a', 'program-b']


def test_duplicate_receipt_key_executes_once_while_other_keys_overlap(leaves, make_task):
    """Exactly-once is scoped to the receipt key, never to the whole task."""
    release = threading.Event()
    leaves.behaviors['hold_read'] = lambda task, _args: (
        release.wait(_WAIT * 2), {'held': True})[1]
    leaves.behaviors['probe_read'] = lambda task, _args: {'probe': True}
    task = make_task()
    hold = {'code': 'return await tools.invoke("hold_read", {});'}
    probe = {'code': 'return await tools.invoke("probe_read", {});'}
    outcomes = {}

    def invoke(label, call_id, arguments):
        _tc_id, content, _aborted = tool_gateway.handle_execute_program(
            task, {}, EXECUTE_PROGRAM_NAME, call_id, arguments, 1,
            {'llmRound': 0, 'toolCallId': call_id}, dict(_CFG), None, False)
        outcomes[label] = str(content)

    first = threading.Thread(target=invoke, args=('first', 'dup-call', hold), daemon=True)
    copy = threading.Thread(target=invoke, args=('copy', 'dup-call', hold), daemon=True)
    other = threading.Thread(target=invoke, args=('other', 'other-call', probe), daemon=True)
    try:
        first.start()
        assert leaves.event('hold_read').wait(_WAIT)
        copy.start()
        other.start()
        other.join(_WAIT)
        assert not other.is_alive(), (
            'an unrelated receipt key waited for the running program')
        assert leaves.count('hold_read') == 1
    finally:
        release.set()
    first.join(_WAIT)
    copy.join(_WAIT)
    assert not first.is_alive() and not copy.is_alive()
    assert leaves.count('hold_read') == 1, 'a duplicate copy executed its program again'
    assert outcomes['copy'] == outcomes['first']
    assert [run['gatewayCallId'] for run in task['programRuns']].count('dup-call') == 1


@pytest.mark.parametrize('earlier_code, earlier_status', [
    ('await tools.invoke("hold_read", {}); return "a";', 'completed'),
    ('await tools.invoke("hold_read", {}); throw new Error("earlier program failed");',
     'error'),
])
def test_ordered_child_of_a_later_sibling_waits_for_the_earlier_sibling(
        leaves, make_task, earlier_code, earlier_status):
    """A declared-target write keeps model issue order across sibling programs.

    The later program runs at once (its read overlaps the earlier program),
    but its write_file child enters only after the earlier sibling settled,
    whether that sibling returned or failed.
    """
    release = threading.Event()
    observed = {}
    leaves.behaviors['hold_read'] = lambda task, _args: (
        release.wait(_WAIT * 2), {'held': True})[1]
    leaves.behaviors['probe_read'] = lambda task, _args: {'probe': True}

    def write(task, _args):
        observed['earlier_row'] = _row_status(task, 'program-a')
        return {'wrote': True}

    leaves.behaviors['write_file'] = write
    task = make_task()
    later = ('await tools.invoke("probe_read", {}); '
             'await tools.invoke("write_file", {path: "notes.txt", content: "b"}); '
             'return "b";')
    round_ = _Round(task, [('program-a', earlier_code), ('program-b', later)])
    try:
        assert leaves.event('hold_read').wait(_WAIT)
        assert leaves.event('probe_read').wait(_WAIT), (
            'the later program did not start beside the earlier one')
        assert not leaves.event('write_file').wait(0.3), (
            'the later write entered before the earlier sibling settled')
    finally:
        release.set()
    round_.finish()

    assert leaves.count('write_file') == 1
    assert observed['earlier_row'] in {'done', 'error'}
    runs = _runs(task)
    assert runs['program-a']['status'] == earlier_status
    assert runs['program-b']['status'] == 'completed'


def test_stop_releases_an_ordered_child_waiting_for_its_sibling(leaves, make_task):
    release = threading.Event()

    def hold(task, _args):
        while not release.wait(0.05):
            if task.get('aborted'):
                return {'stopped': True}
        return {'held': True}

    leaves.behaviors['hold_read'] = hold
    leaves.behaviors['probe_read'] = lambda task, _args: {'probe': True}
    leaves.behaviors['write_file'] = lambda task, _args: {'wrote': True}
    task = make_task()
    later = ('await tools.invoke("probe_read", {}); '
             'await tools.invoke("write_file", {path: "notes.txt", content: "b"}); '
             'return "b";')
    round_ = _Round(task, [
        ('program-a', 'await tools.invoke("hold_read", {}); return "a";'),
        ('program-b', later)])
    try:
        assert leaves.event('hold_read').wait(_WAIT)
        assert leaves.event('probe_read').wait(_WAIT)
        task['aborted'] = True
        task['abort_event'].set()
        assert round_.done.wait(_WAIT * 2), 'Stop left a sibling waiting forever'
    finally:
        release.set()
    round_.finish()
    assert leaves.count('write_file') == 0, 'the ordered write ran after Stop'


def test_child_reentering_the_gateway_from_its_worker_does_not_deadlock(leaves, make_task):
    """A child may re-enter a gateway handler on another thread mid-program."""
    task = make_task()
    leaves.behaviors['probe_read'] = lambda task_, _args: {'probe': True}

    def nested(task_, _args):
        _tc_id, content, _aborted = tool_gateway.handle_execute_tools(
            task_, {}, EXECUTE_TOOLS_NAME, 'nested-gateway-call',
            {'calls': [{'name': 'probe_read', 'arguments': {}}]}, 1,
            {'llmRound': 0, 'toolCallId': 'nested-gateway-call'}, dict(_CFG), None, False)
        return json.loads(str(content))['status']

    leaves.behaviors['nested_gateway_probe'] = nested
    finished = []

    def run():
        tool_gateway.handle_execute_program(
            task, {}, EXECUTE_PROGRAM_NAME, 'outer-program',
            {'code': 'return await tools.invoke("nested_gateway_probe", {});'}, 1,
            {'llmRound': 0, 'toolCallId': 'outer-program'}, dict(_CFG), None, False)
        finished.append(True)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(_WAIT * 2)

    assert not thread.is_alive(), 'nested gateway re-entry deadlocked'
    assert finished == [True]
    assert leaves.count('probe_read') == 1
    assert _runs(task)['outer-program']['result'] == 'ok'


def test_native_sibling_program_callbacks_overlap(leaves):
    """The native concurrent gateway reaches the same handler without a lock."""
    from lib.agent_core.admission import fire_terminal_callbacks
    from lib.agent_core.native_process import (
        NativeToolOrder, RoundRequest, RoundSettlement, ToolRequest,
    )
    from lib.observability import InstrumentedThreadPoolExecutor
    from lib.tasks_pkg.native_gateway import NativeGateway
    from tests.tasks_pkg.test_native_gateway import principal, task as native_task

    barrier = threading.Barrier(2)
    leaves.behaviors['rendezvous_read'] = lambda task_, _args: (
        barrier.wait(_WAIT), {'met': True})[1]
    owner = native_task()
    owner['id'] = f'native-sibling-{uuid.uuid4().hex[:12]}'
    leaves.tasks.add(owner['id'])
    owner['_executable_tool_catalog'] = [_schema('rendezvous_read')]
    owner['_tool_gateway_names'] = [EXECUTE_PROGRAM_NAME]
    definitions = [_schema('execute_program', code={'type': 'string'}),
                   _schema('rendezvous_read')]
    code = 'return await tools.invoke("rendezvous_read", {});'
    calls = [{'id': call_id, 'type': 'function', 'function': {
        'name': EXECUTE_PROGRAM_NAME, 'arguments': json.dumps({'code': code})}}
        for call_id in ('native-a', 'native-b')]

    async def run(executor):
        gateway = NativeGateway(
            principal=principal(), task=owner, executor=executor, configuration=dict(_CFG),
            tool_list=definitions, permissions={EXECUTE_PROGRAM_NAME: 'read'},
            project_path=None, project_enabled=False, model='fixture',
            round_source=lambda: 0, decision_policy=None, pending_calls=16,
            pending_bytes=1 << 20)
        gateway.select_concurrent_dispatch()
        try:
            gateway.prepare_response(RoundSettlement(
                RoundRequest(principal(), owner['id'], 'round-0', 0, 0),
                {'kind': 'returned', 'model_calls': calls}, None, NativeToolOrder()))
            return await asyncio.gather(*(gateway.execute(ToolRequest(
                principal(), owner['id'], f'run-{call["id"]}', EXECUTE_PROGRAM_NAME,
                {'code': code}, call['id'], model_round=0, model_call_id=call['id']))
                for call in calls))
        finally:
            gateway.close()

    try:
        with InstrumentedThreadPoolExecutor(
                max_workers=1, metric_pool='tool',
                thread_name_prefix='native-sibling-programs') as executor:
            results = asyncio.run(run(executor))
    finally:
        fire_terminal_callbacks(owner['id'])

    assert not barrier.broken, 'native sibling programs still execute one at a time'
    assert leaves.count('rendezvous_read') == 2, results
    assert [json.loads(result) for result in results] == [{'met': True}, {'met': True}]


def test_an_expired_earlier_sibling_keeps_its_place_until_its_worker_returns(
        leaves, make_task, monkeypatch):
    """A collection deadline gives up on a receipt, never on the call order.

    The earlier program is still inside an unbounded write when both programs
    outlive their collection deadline and the round settles with timeout
    receipts. Its later write to the shared file must still land before the
    later program's write, because the earlier worker has not returned.
    """
    import lib.tools.registry as registry
    monkeypatch.setenv('TOOL_PARALLEL_TIMEOUT', '1')
    monkeypatch.setattr(registry, 'declared_tool_timeout_ms', lambda *_args, **_kwargs: None)
    release = threading.Event()
    shared_writes = []
    later_wrote = threading.Event()
    leaves.behaviors['probe_read'] = lambda task, _args: {'probe': True}

    def write(task, args):
        if args['path'] == 'hold.txt':
            release.wait(_WAIT * 2)
        else:
            shared_writes.append(args['content'])
            if args['content'] == 'b':
                later_wrote.set()
        return {'wrote': True}

    leaves.behaviors['write_file'] = write
    task = make_task()
    earlier = ('await tools.invoke("write_file", {path: "hold.txt", content: "x"}); '
               'await tools.invoke("write_file", {path: "notes.txt", content: "a"}); '
               'return "a";')
    round_ = _Round(task, [('program-a', earlier), ('program-b', _LATER_WRITE)])
    try:
        assert leaves.event('probe_read').wait(_WAIT)
        assert round_.done.wait(_WAIT), 'the collection deadline did not settle the round'
        # A released later write would land here, before the earlier one.
        later_wrote.wait(1.0)
    finally:
        release.set()
    assert later_wrote.wait(_WAIT)
    assert shared_writes == ['a', 'b'], (
        'the later write overtook an expired earlier sibling that was still running')
    assert _until(lambda: [_runs(task).get(call_id, {}).get('status')
                           for call_id in ('program-a', 'program-b')]
                  == ['completed', 'completed'])


@pytest.mark.parametrize('earlier', ['awaits_a_native_approval', 'callback_failed'])
def test_a_native_sibling_that_never_runs_does_not_hold_back_a_later_write(
        leaves, earlier):
    """Only occurrences the native runtime will run take a place in the order.

    A gateway call under a write or approval permission runs only after a host
    decision, and never when it is denied; a dispatched callback can also fail
    before its pipeline. Neither may leave a later sibling's ordered child
    waiting for Stop or the native deadline.
    """
    leaves.behaviors['probe_read'] = lambda task, _args: {'probe': True}
    leaves.behaviors['write_file'] = lambda task, _args: {'wrote': True}
    later = {'calls': [
        {'name': 'probe_read', 'arguments': {}},
        {'name': 'write_file', 'arguments': {'path': 'notes.txt', 'content': 'b'}}]}
    permissions = {EXECUTE_PROGRAM_NAME: 'read', EXECUTE_TOOLS_NAME: 'read'}
    callbacks = {'native-b': (EXECUTE_TOOLS_NAME, later)}
    if earlier == 'awaits_a_native_approval':
        permissions[EXECUTE_PROGRAM_NAME] = 'approval'
    else:
        callbacks = {'native-a': (EXECUTE_PROGRAM_NAME,
                                  {'code': 'return "not the declared program";'}),
                     **callbacks}

    results, owner = _native_round(leaves, [
        _native_call('native-a', {'code': 'return "a";'}),
        _native_call('native-b', later, name=EXECUTE_TOOLS_NAME)],
        callbacks, permissions=permissions)

    assert results != 'stopped at the deadline', 'the later sibling waited for a call that never ran'
    assert not owner.get('aborted'), results
    assert leaves.count('write_file') == 1, results


def test_a_native_later_write_waits_for_the_earlier_native_sibling(leaves):
    """Both native callbacks run: the later write still follows the earlier program."""
    release = threading.Event()
    observed = {}
    leaves.behaviors['hold_read'] = lambda task, _args: (
        release.wait(_WAIT * 2), {'held': True})[1]
    leaves.behaviors['probe_read'] = lambda task, _args: {'probe': True}

    def write(task, _args):
        observed['earlier_released'] = release.is_set()
        return {'wrote': True}

    leaves.behaviors['write_file'] = write
    earlier = 'await tools.invoke("hold_read", {}); return "a";'
    timer = threading.Timer(1.0, release.set)
    timer.start()
    try:
        results, owner = _native_round(
            leaves, [_native_call('native-a', {'code': earlier}),
                     _native_call('native-b', {'code': _LATER_WRITE})],
            {'native-a': (EXECUTE_PROGRAM_NAME, {'code': earlier}),
             'native-b': (EXECUTE_PROGRAM_NAME, {'code': _LATER_WRITE})})
    finally:
        timer.cancel()
        release.set()

    assert not owner.get('aborted'), results
    assert observed == {'earlier_released': True}, 'the later write overtook the earlier sibling'
    assert [json.loads(result) for result in results] == ['a', 'b']


def test_a_nested_gateway_call_keeps_its_parents_place(leaves, make_task):
    """A write reached through a nested execute_tools still follows earlier siblings."""
    release = threading.Event()
    observed = {}
    leaves.behaviors['hold_read'] = lambda task, _args: (
        release.wait(_WAIT * 2), {'held': True})[1]
    leaves.behaviors['probe_read'] = lambda task, _args: {'probe': True}

    def write(task, _args):
        observed['earlier_row'] = _row_status(task, 'program-a')
        return {'wrote': True}

    leaves.behaviors['write_file'] = write
    task = make_task()
    task['_executable_tool_catalog'] = list(_CATALOG) + [
        _schema(EXECUTE_TOOLS_NAME, calls={'type': 'array'}, execution={'type': 'string'})]
    nested = ('await tools.invoke("probe_read", {}); '
              'await tools.invoke("execute_tools", {calls: [{name: "write_file", '
              'arguments: {path: "notes.txt", content: "b"}}]}); return "b";')
    round_ = _Round(task, [
        ('program-a', 'await tools.invoke("hold_read", {}); return "a";'),
        ('program-b', nested)])
    try:
        assert leaves.event('hold_read').wait(_WAIT)
        assert leaves.event('probe_read').wait(_WAIT)
        # A nested write that skipped the order would run here.
        leaves.event('write_file').wait(0.5)
    finally:
        release.set()
    round_.finish()

    assert leaves.count('write_file') == 1
    assert observed['earlier_row'] in {'done', 'error'}, observed
    assert _runs(task)['program-b']['status'] == 'completed'


def test_a_reused_call_id_never_takes_a_published_siblings_place(make_task):
    """An occurrence is its published call ID and row, not a bare call ID."""
    from lib.tasks_pkg.tool_dispatch._ordering import (
        gateway_sibling_adopter, gateway_sibling_turn, publish_gateway_sibling_order,
        release_gateway_siblings,
    )
    task = make_task()
    earlier = _program_item(task, 'program-a', 'return "a";', 1)
    later = _program_item(task, 'program-b', 'return "b";', 2)
    publish_gateway_sibling_order(task, 0, [earlier, later])
    impostor = earlier[:5] + (dict(earlier[5]), None)
    waits_for = gateway_sibling_turn(task, 'program-b', 0, later[5]).earlier

    assert gateway_sibling_turn(task, 'program-a', 0, impostor[5]) is None
    assert gateway_sibling_adopter(task, impostor) is None
    release_gateway_siblings(task, [impostor])
    assert not any(settled.done() for settled in waits_for)
    release_gateway_siblings(task, [earlier])
    assert all(settled.done() for settled in waits_for)
