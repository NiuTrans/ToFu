"""Concurrent native application dispatch keeps only canonical call order.

Owner decision 2026-10-28 (docs/contracts/TOOL_EXECUTION_POLICY.md, "Native
application rounds"): every call of a native round starts at once, writes
included. The gateway binds the order the tool_dispatch ``_ordering``
authority keeps (same resolved file, todo_write, frozen run_command
directories) into the settled response; it no longer serializes writes.
"""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from lib.agent_core.native_process import (
    ApprovalRequest, NativeToolOrder, RoundRequest, RoundSettlement, ToolRequest,
)
from lib.observability import InstrumentedThreadPoolExecutor
from lib.tasks_pkg.executor._finalize import _finalize_tool_round
from lib.tasks_pkg.native_gateway import NativeGateway
from tests.tasks_pkg.test_native_gateway import principal, task

pytestmark = pytest.mark.unit

_WRITE = {'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}}}
_EDIT = {'type': 'object', 'properties': {'path': {'type': 'string'}, 'edits': {'type': 'array'}}}
_TODO = {'type': 'object', 'properties': {'todos': {'type': 'array'}}}
_COMMAND = {'type': 'object', 'properties': {'command': {'type': 'string'},
                                             'working_dir': {'type': 'string'}}}


def _definitions(**schemas):
    return [{'type': 'function', 'function': {'name': name, 'parameters': schema}}
            for name, schema in schemas.items()]


def _call(identity, name, arguments):
    return {'id': identity, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}


class _Approve:
    async def decide(self, _request):
        return 'human'


def _gateway(owner, executor, definitions, project_path, permissions):
    owner['_executable_tool_catalog'] = definitions
    gateway = NativeGateway(principal=principal(), task=owner, executor=executor,
        configuration={'autoApply': False}, tool_list=definitions, permissions=permissions,
        project_path=project_path, project_enabled=project_path is not None, model='fixture',
        round_source=lambda: 0, decision_policy=_Approve(), pending_calls=16, pending_bytes=1 << 20)
    gateway.select_concurrent_dispatch()
    return gateway


def _settle(gateway, owner, calls, order=None, round_num=0):
    order = NativeToolOrder() if order is None else order
    gateway.prepare_response(RoundSettlement(RoundRequest(principal(), owner['id'], f'round-{round_num}', round_num, 0),
        {'kind': 'returned', 'model_calls': calls}, None, order))
    return order


async def _run_round(gateway, owner, calls, round_num=0, authority='human'):
    """Approve each write in call order (as the native loop does), then start all."""
    for call in calls:
        arguments = call['function']['arguments'].encode()
        assert await gateway.decide(ApprovalRequest(principal(), owner['id'], f'approval-{round_num}-{call["id"]}',
            call['id'], call['function']['name'], 'epoch', arguments, b'null', asyncio.Event(), 5)) == authority
    return await asyncio.gather(*(gateway.execute(ToolRequest(principal(), owner['id'], f'run-{round_num}-{call["id"]}',
        call['function']['name'], json.loads(call['function']['arguments']), call['id'],
        model_round=round_num, model_call_id=call['id'])) for call in calls))


def _finish(task_, rn, row, call_id, content='done'):
    _finalize_tool_round(task_, rn, row, [{'title': 'fixture', 'content': content, 'badge': 'done'}])
    return call_id, content, False


def test_writes_to_different_files_start_together_through_the_canonical_pipeline(tmp_path):
    owner = task()
    together = threading.Barrier(2)

    def write(task_, tc, name, call_id, args, rn, row, *_rest):
        together.wait(5)  # Raises BrokenBarrierError when the writes run one at a time.
        return _finish(task_, rn, row, call_id, 'wrote ' + args['path'])

    owner['_tool_env'] = SimpleNamespace(resolve=lambda name: write if name == 'write_file' else None,
        write_names={'write_file'}, idempotent_names=set(), confirmation_names=set())
    definitions = _definitions(write_file=_WRITE)
    calls = [_call('first', 'write_file', {'path': 'a.txt', 'content': 'a'}),
             _call('second', 'write_file', {'path': 'b.txt', 'content': 'b'})]

    async def run(executor):
        gateway = _gateway(owner, executor, definitions, str(tmp_path), {'write_file': 'write'})
        try:
            assert _settle(gateway, owner, calls).plan == {}
            return await _run_round(gateway, owner, calls)
        finally:
            gateway.close()

    with InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='tool',
            thread_name_prefix='native-concurrent-writes') as executor:
        results = asyncio.run(run(executor))
    assert ['wrote a.txt' in results[0], 'wrote b.txt' in results[1]] == [True, True]
    assert [row['status'] for row in owner['toolRounds']] == ['done', 'done']


def test_settled_order_names_same_target_and_todo_predecessors_only(tmp_path):
    owner = task()
    owner['config']['projectPath'] = str(tmp_path)
    definitions = _definitions(write_file=_WRITE, edit_file=_EDIT, todo_write=_TODO)
    calls = [_call('first', 'write_file', {'path': 'notes.txt', 'content': 'a'}),
             _call('other', 'write_file', {'path': 'other.txt', 'content': 'b'}),
             _call('second', 'edit_file', {'path': str(tmp_path / 'notes.txt'), 'edits': []}),
             _call('plan-1', 'todo_write', {'todos': []}),
             _call('plan-2', 'todo_write', {'todos': []})]
    with InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='tool') as executor:
        gateway = _gateway(owner, executor, definitions, str(tmp_path),
                           {'write_file': 'write', 'edit_file': 'write', 'todo_write': 'read'})
        try:
            order = _settle(gateway, owner, calls)
        finally:
            gateway.close()
    # Two spellings of one file are ordered; the unrelated file is not.
    assert order.plan == {'second': ['first'], 'plan-2': ['plan-1']}


def test_command_directories_freeze_in_call_order_and_sticky_cwd_ends_in_call_order(
        tmp_path, monkeypatch):
    """A sticky working_dir set earlier in the round moves later commands, and
    the sticky directory ends where call order leaves it whatever finishes last."""
    from lib.project_mod.config import clear_conv_state, get_conv_cwd, set_conv_cwd
    from lib.tasks_pkg.workspace_delivery import execution_root_key
    monkeypatch.delenv('TOFU_STICKY_CWD', raising=False)
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'other').mkdir()
    owner = task()
    owner['config']['projectPath'] = str(tmp_path)
    key = execution_root_key(owner)
    seen, other_started = {}, threading.Event()

    def command(task_, tc, name, call_id, args, rn, row, *_rest):
        seen[call_id] = args['working_dir']
        if call_id == 'rc-1':
            # Starts last and finishes last, so its own sticky write would win.
            assert other_started.wait(5)
            time.sleep(0.05)
        set_conv_cwd(key, args['working_dir'])
        if call_id == 'rc-3':
            other_started.set()
        return _finish(task_, rn, row, call_id)

    owner['_tool_env'] = SimpleNamespace(resolve=lambda name: command if name == 'run_command' else None,
        write_names={'run_command'}, idempotent_names=set(), confirmation_names=set())
    definitions = _definitions(run_command=_COMMAND)
    calls = [_call('rc-1', 'run_command', {'command': 'pwd', 'working_dir': 'sub'}),
             _call('rc-2', 'run_command', {'command': 'pwd'}),
             _call('rc-3', 'run_command', {'command': 'pwd', 'working_dir': 'other'})]

    async def run(executor):
        gateway = _gateway(owner, executor, definitions, str(tmp_path), {'run_command': 'write'})
        try:
            assert _settle(gateway, owner, calls).plan == {}
            await _run_round(gateway, owner, calls)
        finally:
            gateway.close()

    try:
        with InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='tool') as executor:
            asyncio.run(run(executor))
        sub, other = str((tmp_path / 'sub').resolve()), str((tmp_path / 'other').resolve())
        assert [seen[call['id']] for call in calls] == [sub, sub, other]
        assert get_conv_cwd(key) == other
    finally:
        clear_conv_state(key)


def test_stopped_response_binds_an_empty_order_and_order_composition_must_match():
    owner = task()
    definitions = _definitions(write_file=_WRITE)
    calls = [_call('write', 'write_file', {'path': 'a.txt', 'content': 'a'})]
    with InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='tool') as executor:
        qualified = NativeGateway(principal=principal(), task=owner, executor=executor,
            configuration={}, tool_list=definitions, permissions={'write_file': 'write'},
            project_path=None, project_enabled=False, model='fixture', round_source=lambda: 0,
            decision_policy=_Approve(), pending_calls=4, pending_bytes=65536)
        with pytest.raises(ValueError, match='call order composition'):
            _settle(qualified, owner, calls)
        qualified.close()
        gateway = _gateway(owner, executor, definitions, None, {'write_file': 'write'})
        with pytest.raises(ValueError, match='call order composition'):
            gateway.prepare_response(RoundSettlement(RoundRequest(principal(), owner['id'], 'r', 0, 0),
                {'kind': 'returned', 'model_calls': calls}))
        owner['abort_event'].set()
        assert _settle(gateway, owner, calls).plan == {}
        assert not owner['toolRounds']
        gateway.close()


def test_recycled_call_id_keeps_its_approval_and_the_order_names_model_ids(tmp_path, monkeypatch):
    """A positional-ID provider repeats ``call`` in every response: the Auto
    receipt follows the admission identity, while the call order sent to Rust
    still names the model's IDs."""
    from lib.tasks_pkg.tool_dispatch import _approval

    def human(*_args, **_kwargs):
        raise AssertionError('an approved native write fell back to a human decision')

    monkeypatch.setattr(_approval, 'request_write_approval', human)
    owner = task()
    written = []

    def write(task_, tc, name, call_id, args, rn, row, *_rest):
        written.append((call_id, args['path']))
        return _finish(task_, rn, row, call_id, 'wrote ' + args['path'])

    owner['_tool_env'] = SimpleNamespace(resolve=lambda name: write, write_names={'write_file'},
        idempotent_names=set(), confirmation_names=set())
    definitions = _definitions(write_file=_WRITE)
    owner['_executable_tool_catalog'] = definitions
    rounds = [[_call('call', 'write_file', {'path': 'first.txt', 'content': 'a'})],
              [_call('call', 'write_file', {'path': 'second.txt', 'content': 'b'}),
               _call('again', 'write_file', {'path': 'second.txt', 'content': 'c'})]]

    async def run(executor):
        gateway = NativeGateway(principal=principal(), task=owner, executor=executor,
            configuration={'autoApply': True}, tool_list=definitions, permissions={'write_file': 'write'},
            project_path=str(tmp_path), project_enabled=True, model='fixture', round_source=None,
            decision_policy=None, pending_calls=16, pending_bytes=1 << 20)
        gateway.select_concurrent_dispatch()
        try:
            plans, results = [], []
            for round_num, calls in enumerate(rounds):
                gateway.round_source = lambda round_num=round_num: round_num
                plans.append(_settle(gateway, owner, calls, round_num=round_num).plan)
                results += await _run_round(gateway, owner, calls, round_num, authority='auto_write_mode')
            return plans, results
        finally:
            gateway.close()

    with InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='tool') as executor:
        plans, results = asyncio.run(run(executor))
    assert plans == [{}, {'again': ['call']}]
    assert ['wrote first.txt' in results[0], 'wrote second.txt' in results[1]] == [True, True]
    reissued = next(call_id for call_id, path in written if path == 'second.txt' and call_id != 'again')
    assert written[0] == ('call', 'first.txt') and reissued != 'call'
    assert sorted(row['toolCallId'] for row in owner['toolRounds']) == sorted(['call', reissued, 'again'])
    assert [row['status'] for row in owner['toolRounds']] == ['done', 'done', 'done']


def test_inheriting_command_falls_back_when_its_sticky_sibling_is_denied(tmp_path, monkeypatch):
    """Sequential execution never runs a denied command, so it never moves the
    sticky directory: a later command without ``working_dir`` runs where the
    round started, and only an approved sibling's directory is inherited."""
    import os
    from lib.project_mod.config import clear_conv_state
    from lib.tasks_pkg.workspace_delivery import execution_root_key
    monkeypatch.delenv('TOFU_STICKY_CWD', raising=False)
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'other').mkdir()
    owner = task()
    owner['config']['projectPath'] = str(tmp_path)
    key = execution_root_key(owner)
    seen = {}

    def command(task_, tc, name, call_id, args, rn, row, *_rest):
        seen[call_id] = os.path.realpath(args['working_dir'])
        return _finish(task_, rn, row, call_id)

    owner['_tool_env'] = SimpleNamespace(resolve=lambda name: command if name == 'run_command' else None,
        write_names={'run_command'}, idempotent_names=set(), confirmation_names=set())

    class DenySub:
        async def decide(self, request):
            return 'denied' if request.call_id == 'rc-1' else 'human'

    definitions = _definitions(run_command=_COMMAND)
    calls = [_call('rc-1', 'run_command', {'command': 'pwd', 'working_dir': 'sub'}),
             _call('rc-2', 'run_command', {'command': 'pwd'}),
             _call('rc-3', 'run_command', {'command': 'pwd', 'working_dir': 'other'}),
             _call('rc-4', 'run_command', {'command': 'pwd'})]

    async def run(executor):
        gateway = NativeGateway(principal=principal(), task=owner, executor=executor,
            configuration={'autoApply': False}, tool_list=definitions, permissions={'run_command': 'write'},
            project_path=str(tmp_path), project_enabled=True, model='fixture', round_source=lambda: 0,
            decision_policy=DenySub(), pending_calls=16, pending_bytes=1 << 20)
        gateway.select_concurrent_dispatch()
        try:
            assert _settle(gateway, owner, calls).plan == {}
            decisions = []
            for call in calls:  # Rust decides a round's approvals in call order.
                decisions.append(await gateway.decide(ApprovalRequest(principal(), owner['id'],
                    'approval-' + call['id'], call['id'], 'run_command', 'epoch',
                    call['function']['arguments'].encode(), b'null', asyncio.Event(), 5)))
            assert decisions == ['denied', 'human', 'human', 'human']
            approved = calls[1:]
            await asyncio.gather(*(gateway.execute(ToolRequest(principal(), owner['id'], 'run-' + call['id'],
                'run_command', json.loads(call['function']['arguments']), call['id'],
                model_round=0, model_call_id=call['id'])) for call in approved))
        finally:
            gateway.close()

    try:
        with InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='tool') as executor:
            asyncio.run(run(executor))
        root, other = os.path.realpath(tmp_path), os.path.realpath(tmp_path / 'other')
        assert seen == {'rc-2': root, 'rc-3': other, 'rc-4': other}
    finally:
        clear_conv_state(key)
