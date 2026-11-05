"""One model round's tool calls overlap through the real dispatch path.

Owner decision 2026-10-28 (docs/contracts/TOOL_EXECUTION_POLICY.md,
"Concurrent tool rounds"): every admitted call of a round starts at once —
reads, writes, ``run_command`` and human waits alike. Order survives only for
mutations of the same resolved file, ``todo_write`` revisions and each
command's working directory, which admission resolves in call order.

The pool-level spec in ``test_tool_pool_cancellation`` replaces
``_execute_tool_one_in_pool``. These keep the whole per-call path — partition,
admission, approval gate, ordering, runtime context and settlement — and
replace only the executor at its two existing seams, so a serial lane anywhere
on the path breaks a rendezvous instead of letting the calls enter together.
"""
from __future__ import annotations

import json
import threading
import time
import uuid

import pytest

from lib.tasks_pkg.executor import tool_registry
from tests.tools.test_tool_settle_immediacy import _mk_task, _mk_tc, _run_pipeline
from tests.tools.test_tool_settle_immediacy import rec as recorder_fixture

pytestmark = pytest.mark.unit

rec = recorder_fixture

_TOOL = 'overlap_probe_read'
_CALLS = 4
_STOPPED = 'Task aborted by user.'


@pytest.fixture
def rendezvous_tool():
    gate = threading.Barrier(_CALLS)
    entered = []

    def handler(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(task, rn, row, [{
            'toolName': name, 'title': name, 'snippet': 'ok', 'source': 'Test',
            'fetched': True, 'fetchedChars': 2}])
        entered.append(call_id)
        gate.wait(timeout=10)
        return call_id, f'ok {call_id}', False

    tool_registry.register(_TOOL, handler)
    try:
        yield gate, entered
    finally:
        tool_registry._exact.pop(_TOOL, None)
        tool_registry._metadata.pop(_TOOL, None)


def test_independent_read_calls_enter_their_handlers_together(rendezvous_tool, rec):
    gate, entered = rendezvous_tool
    task = _mk_task(_tool_schema=[{'type': 'function', 'function': {
        'name': _TOOL, 'parameters': {'type': 'object', 'properties': {}}}}])
    items = [_mk_tc(f'overlap-{index}', _TOOL, index + 1) for index in range(_CALLS)]

    messages = _run_pipeline(task, items)

    assert not gate.broken, 'a dispatch-path lock serialized independent read calls'
    assert sorted(entered) == sorted(item[2] for item in items)
    replies = {row['tool_call_id']: row['content'] for row in messages
               if row.get('role') == 'tool'}
    assert replies == {item[2]: f'ok {item[2]}' for item in items}
    assert all(rec.count(item[2], 'tool_complete') == 1 for item in items)


# ═══════════════════════════════════════════════════════════════════
#  Writes, commands and waits — the executor is scripted per call id
# ═══════════════════════════════════════════════════════════════════

class _Script:
    """Per-call behaviour plus one globally ordered enter/exit log."""

    def __init__(self):
        self.behaviors = {}
        self.arguments = {}
        self.log: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)

    def mark(self, kind, call_id):
        with self._changed:
            self.log.append((kind, call_id))
            self._changed.notify_all()

    def seen(self, kind, call_id):
        with self._lock:
            return (kind, call_id) in self.log

    def index(self, kind, call_id):
        with self._lock:
            return self.log.index((kind, call_id))

    def wait_for(self, kind, *call_ids, timeout=10.0):
        deadline = time.monotonic() + timeout
        with self._changed:
            while not all((kind, call_id) in self.log for call_id in call_ids):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
        return True

    def rendezvous(self, *call_ids):
        """Every named call must be inside its handler at the same time."""
        barrier = threading.Barrier(len(call_ids))
        for call_id in call_ids:
            self.behaviors[call_id] = lambda task, _barrier=barrier: _barrier.wait(10)
        return barrier

    def overlapped(self, barrier, *call_ids):
        """True only when every named handler ran and met the others inside."""
        return not barrier.broken and all(
            self.seen('exit', call_id) for call_id in call_ids)


@pytest.fixture
def script(monkeypatch):
    import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    scripted = _Script()

    def execute(task, tc, name, call_id, arguments, rn, row, *rest, **kwargs):
        scripted.arguments[call_id] = dict(arguments)
        scripted.mark('enter', call_id)
        try:
            behavior = scripted.behaviors.get(call_id)
            content = behavior(task) if behavior is not None else None
        finally:
            scripted.mark('exit', call_id)
        _finalize_tool_round(task, rn, row, [{
            'toolName': name, 'title': name, 'snippet': 'ok', 'source': 'Test',
            'fetched': True, 'fetchedChars': 2}])
        return call_id, content if isinstance(content, str) else f'{name} {call_id}', False

    # The two existing executor seams: unbounded calls (writes, ordered state,
    # human waits) and bounded calls (the declared-timeout wrapper).
    monkeypatch.setattr(heartbeat, '_execute_tool_one', execute)
    monkeypatch.setattr(pipeline, '_execute_tool_one', execute)
    return scripted


def _task(**config):
    task = _mk_task(id=f'parallel-{uuid.uuid4().hex[:12]}')
    task['config'] = {'tools': {'resultEnvelope': 'legacy'}, **config}
    return task


def _call(call_id, name, number, arguments, project_enabled=False):
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    _number, row, _event = _build_tool_round_entry(
        name, dict(arguments), call_id, json.dumps(arguments), number, project_enabled)
    tc = {'id': call_id, 'type': 'function',
          'function': {'name': name, 'arguments': json.dumps(arguments)}}
    return tc, name, call_id, dict(arguments), row['roundNum'], row, None


def _run(task, calls, *, cfg=None, project_path=None):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    messages: list = []
    execute_tool_pipeline(
        task, calls, cfg=cfg or {'autoApply': True}, project_path=project_path,
        project_enabled=bool(project_path), tool_list=[], messages=messages,
        all_search_results_text=[], round_num=0, model='test-model')
    return {row['tool_call_id']: row['content'] for row in messages
            if row.get('role') == 'tool'}, messages


@pytest.fixture
def project(tmp_path):
    """A disposable project root plus a task factory bound to it.

    Execution roots and the sticky cwd are registered per task id; release
    them so no later test in this worker inherits them.
    """
    keys: list[str] = []

    def make_task():
        task = _task(projectPath=str(tmp_path))
        # No conversation: the Coordinator role lookup needs real storage.
        task['convId'] = ''
        keys.append(task['id'])
        return task

    yield tmp_path, make_task
    from lib.project_mod.config import clear_conv_state
    for key in keys:
        clear_conv_state(key)


def test_two_run_commands_overlap(script, rec):
    barrier = script.rendezvous('rc-1', 'rc-2')
    calls = [_call('rc-1', 'run_command', 1, {'command': 'sleep 1'}),
             _call('rc-2', 'run_command', 2, {'command': 'sleep 1'})]

    replies, _messages = _run(_task(), calls)

    assert script.overlapped(barrier, 'rc-1', 'rc-2'), (
        'run_command calls still execute one at a time')
    assert set(replies) == {'rc-1', 'rc-2'}


def test_write_file_and_run_command_overlap(script, rec):
    barrier = script.rendezvous('write', 'command')
    calls = [_call('write', 'write_file', 1, {'path': 'notes.txt', 'content': 'x'}),
             _call('command', 'run_command', 2, {'command': 'make test'})]

    _run(_task(), calls)

    assert script.overlapped(barrier, 'write', 'command'), (
        'a write still holds back its sibling command')


def test_edits_to_different_files_overlap(script, rec, project):
    root, make_task = project
    barrier = script.rendezvous('edit-a', 'edit-b')
    calls = [_call('edit-a', 'edit_file', 1, {'path': 'a.txt', 'edits': []}),
             _call('edit-b', 'edit_file', 2, {'path': 'b.txt', 'edits': []})]

    _run(make_task(), calls, project_path=str(root))

    assert script.overlapped(barrier, 'edit-a', 'edit-b'), (
        'edits to different files must not wait for each other')


def test_edits_to_the_same_file_apply_in_call_order(script, rec, project):
    """Two spellings of one file are ordered; an unrelated file still overlaps."""
    root, make_task = project
    first_and_other = threading.Barrier(2)
    script.behaviors['first'] = lambda task: (first_and_other.wait(10), time.sleep(0.2))
    script.behaviors['other'] = lambda task: first_and_other.wait(10)
    calls = [
        _call('first', 'edit_file', 1, {'path': 'notes.txt', 'edits': []}),
        _call('second', 'write_file', 2, {'path': str(root / 'notes.txt'), 'content': 'y'}),
        _call('other', 'edit_file', 3, {'path': 'other.txt', 'edits': []}),
    ]

    replies, _messages = _run(make_task(), calls, project_path=str(root))

    assert script.overlapped(first_and_other, 'first', 'other'), (
        'a different file was serialized behind notes.txt')
    assert script.index('enter', 'second') > script.index('exit', 'first'), (
        'the second mutation of notes.txt started before the first finished: %r'
        % (script.log,))
    assert list(replies) == ['first', 'second', 'other']


@pytest.mark.parametrize('name,first,second,other', [
    ('update_memory', {'memory_id': 'm1', 'body': 'a'}, {'memory_id': 'm1', 'body': 'b'},
     {'memory_id': 'm2', 'body': 'c'}),
    ('desktop_write_file', {'path': 'C:/notes.txt', 'content': 'a'},
     {'path': 'C:/notes.txt', 'content': 'b'}, {'path': 'C:/other.txt', 'content': 'c'}),
    ('browser_click', {'tab_id': 7, 'selector': '#a'}, {'tab_id': 7, 'selector': '#b'},
     {'tab_id': 8, 'selector': '#c'}),
])
def test_same_target_effects_apply_in_call_order(script, rec, name, first, second, other):
    """Memory ids, desktop paths and browser tabs order like project files."""
    first_and_other = threading.Barrier(2)
    script.behaviors['first'] = lambda task: (first_and_other.wait(10), time.sleep(0.2))
    script.behaviors['other'] = lambda task: first_and_other.wait(10)
    calls = [_call('first', name, 1, first), _call('second', name, 2, second),
             _call('other', name, 3, other)]

    _run(_task(), calls)

    assert script.overlapped(first_and_other, 'first', 'other'), (
        'a different %s target was serialized' % name)
    assert script.index('enter', 'second') > script.index('exit', 'first'), (
        'the second %s on one target started before the first finished' % name)


def test_todo_write_revisions_apply_in_call_order(script, rec):
    script.behaviors['plan-1'] = lambda task: time.sleep(0.2)
    calls = [_call('plan-1', 'todo_write', 1, {'todos': []}),
             _call('plan-2', 'todo_write', 2, {'todos': []})]

    _run(_task(), calls)

    assert script.index('enter', 'plan-2') > script.index('exit', 'plan-1')


@pytest.mark.parametrize('wait_name,wait_arguments', [
    ('await_task', {'action': 'wait', 'task_id': 'child'}),
    ('ask_human', {'question': 'Which branch?'}),
])
def test_a_human_wait_does_not_block_sibling_calls(script, rec, wait_name, wait_arguments):
    released = threading.Event()

    def wait_for_sibling(task):
        assert released.wait(10), 'the wait was never released by its sibling'
        return 'answered'

    def sibling(task):
        released.set()
        return 'sibling done'

    script.behaviors['wait'] = wait_for_sibling
    script.behaviors['command'] = sibling
    calls = [_call('wait', wait_name, 1, wait_arguments),
             _call('command', 'run_command', 2, {'command': 'make'})]

    replies, _messages = _run(_task(), calls)

    assert replies == {'wait': 'answered', 'command': 'sibling done'}
    assert script.index('exit', 'command') < script.index('exit', 'wait'), (
        'the sibling had to wait for the human wait to finish: %r' % (script.log,))


def test_a_pending_approval_does_not_delay_unrelated_calls(script, rec, monkeypatch):
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    siblings_done = threading.Event()
    decisions = []

    def finish(call_id):
        def behavior(task):
            if script.seen('exit', 'search' if call_id == 'command' else 'command'):
                siblings_done.set()
            return f'{call_id} done'
        return behavior

    def human_decides_after_siblings(task, name, arguments, rn, row, *args, **kwargs):
        decisions.append(row['toolCallId'])
        assert script.wait_for('exit', 'command', 'search', timeout=10), (
            'unrelated calls waited for a pending approval')
        return True, None

    script.behaviors['command'] = finish('command')
    script.behaviors['search'] = finish('search')
    monkeypatch.setattr(pipeline, '_handle_approval', human_decides_after_siblings)
    calls = [_call('gated', 'write_file', 1, {'path': 'notes.txt', 'content': 'x'}),
             _call('command', 'run_command', 2, {'command': 'echo hi'}),
             _call('search', 'web_search', 3, {'query': 'tofu'})]

    task = _task()
    task['_attended'] = True
    replies, _messages = _run(task, calls, cfg={'autoApply': False})

    assert decisions == ['gated']
    assert script.index('enter', 'gated') > script.index('exit', 'command')
    assert script.index('enter', 'gated') > script.index('exit', 'search')
    assert list(replies) == ['gated', 'command', 'search']


def test_results_replay_in_call_order_whatever_finishes_first(script, rec):
    script.behaviors['slow-write'] = lambda task: time.sleep(0.3)
    script.behaviors['mid-command'] = lambda task: time.sleep(0.15)
    calls = [_call('slow-write', 'write_file', 1, {'path': 'a.txt', 'content': 'a'}),
             _call('mid-command', 'run_command', 2, {'command': 'make'}),
             _call('fast-read', 'web_search', 3, {'query': 'tofu'})]

    replies, messages = _run(_task(), calls)

    assert script.index('exit', 'fast-read') < script.index('exit', 'slow-write')
    assert [row['tool_call_id'] for row in messages if row.get('role') == 'tool'] == [
        'slow-write', 'mid-command', 'fast-read']
    assert replies['slow-write'] == 'write_file slow-write'


def test_stop_mid_round_joins_effects_and_skips_unstarted_calls(script, rec):
    """Stop keeps each former lane's contract.

    In-flight effects (command, write) are joined and keep their own receipt;
    an in-flight read gets the Stop placeholder; a same-file successor that
    never started settles aborted without entering its handler.
    """
    task = _task()

    def until_stopped(result):
        def behavior(task_):
            deadline = time.monotonic() + 10
            while not task_.get('aborted') and time.monotonic() < deadline:
                time.sleep(0.01)
            return result
        return behavior

    script.behaviors['command'] = until_stopped('partial output')
    script.behaviors['search'] = until_stopped('late search result')
    script.behaviors['write-1'] = until_stopped('wrote notes.txt')

    def press_stop():
        assert script.wait_for('enter', 'command', 'search', 'write-1')
        task['aborted'] = True

    stopper = threading.Thread(target=press_stop, daemon=True)
    stopper.start()
    calls = [_call('command', 'run_command', 1, {'command': 'make test'}),
             _call('search', 'web_search', 2, {'query': 'tofu'}),
             _call('write-1', 'write_file', 3, {'path': 'notes.txt', 'content': '1'}),
             _call('write-2', 'write_file', 4, {'path': 'notes.txt', 'content': '2'})]

    replies, messages = _run(task, calls)
    stopper.join(5)

    assert [row['tool_call_id'] for row in messages if row.get('role') == 'tool'] == [
        'command', 'search', 'write-1', 'write-2']
    assert all(script.seen('exit', call_id) for call_id in ('command', 'search', 'write-1')), (
        'the pipeline returned while a stopped worker still owned its resources')
    assert not script.seen('enter', 'write-2'), 'Stop must win before an unstarted write'
    assert 'partial output' in replies['command']
    assert 'wrote notes.txt' in replies['write-1']
    assert replies['search'] == _STOPPED
    assert replies['write-2'] == _STOPPED
    rows = {call[2]: call[5] for call in calls}
    assert rows['search']['status'] == 'aborted'
    assert rows['write-2']['status'] == 'aborted'
    assert rec.find('write-2', 'tool_complete')['status'] == 'aborted'


def test_run_command_directories_follow_call_order(script, rec, project, monkeypatch):
    """A sticky ``working_dir`` set earlier in the round moves later commands.

    Admission freezes every directory in call order, so concurrent starts can
    neither move a command nor leave the conversation's sticky directory at an
    arbitrary sibling's choice.
    """
    monkeypatch.delenv('TOFU_STICKY_CWD', raising=False)
    root, make_task = project
    (root / 'sub').mkdir()
    (root / 'other').mkdir()
    task = make_task()
    barrier = script.rendezvous('rc-1', 'rc-2', 'rc-3', 'rc-4')
    calls = [_call('rc-1', 'run_command', 1, {'command': 'pwd', 'working_dir': 'sub'}),
             _call('rc-2', 'run_command', 2, {'command': 'pwd'}),
             _call('rc-3', 'run_command', 3, {'command': 'pwd', 'working_dir': 'other'}),
             _call('rc-4', 'run_command', 4, {'command': 'pwd'})]

    _run(task, calls, project_path=str(root))

    sub, other = str((root / 'sub').resolve()), str((root / 'other').resolve())
    assert script.overlapped(barrier, 'rc-1', 'rc-2', 'rc-3', 'rc-4'), (
        'commands with frozen directories must still overlap')
    assert [script.arguments[call[2]]['working_dir'] for call in calls] == [
        sub, sub, other, other]
    from lib.project_mod.config import get_conv_cwd
    from lib.tasks_pkg.workspace_delivery import execution_root_key
    assert get_conv_cwd(execution_root_key(task)) == other


@pytest.mark.parametrize('refusal', ['approval', 'pre_hook'])
def test_a_refused_command_moves_no_directory_for_later_commands(
        script, rec, project, monkeypatch, refusal):
    """A sibling refused before execution never set its ``working_dir``.

    Sequential execution would run a later command without ``working_dir`` in
    the directory left by the last earlier command that actually ran, so the
    call-order freeze must not hand it the directory of a refused sibling.
    """
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    from lib.tasks_pkg.tool_hooks import HookResult
    monkeypatch.delenv('TOFU_STICKY_CWD', raising=False)
    root, make_task = project
    for name in ('sub', 'other', 'third'):
        (root / name).mkdir()
    refused = {'rc-1', 'rc-4'}
    task = make_task()
    cfg = {'autoApply': True}
    if refusal == 'approval':
        task['_attended'] = True
        cfg = {'autoApply': False}
        monkeypatch.setattr(pipeline, '_handle_approval', lambda task_, name, arguments, rn, row, *a, **k: (
            (False, 'User rejected the tool call.') if row['toolCallId'] in refused else (True, None)))
    else:
        monkeypatch.setattr(pipeline, 'run_pre_hooks', lambda name, arguments, task_: (
            HookResult('block', 'refused by policy') if 'rm -rf' in arguments.get('command', '')
            else None))
    calls = [_call(call_id, 'run_command', number, arguments, project_enabled=True)
             for number, (call_id, arguments) in enumerate((
                 ('rc-1', {'command': 'rm -rf build', 'working_dir': 'sub'}),
                 ('rc-2', {'command': 'pwd'}),
                 ('rc-3', {'command': 'pwd', 'working_dir': 'other'}),
                 ('rc-4', {'command': 'rm -rf build', 'working_dir': 'third'}),
                 ('rc-5', {'command': 'pwd'})), start=1)]

    replies, _messages = _run(task, calls, cfg=cfg, project_path=str(root))

    assert set(replies) == {'rc-1', 'rc-2', 'rc-3', 'rc-4', 'rc-5'}
    assert not script.seen('enter', 'rc-1') and not script.seen('enter', 'rc-4')
    other = str((root / 'other').resolve())
    assert {call_id: script.arguments[call_id]['working_dir']
            for call_id in ('rc-2', 'rc-3', 'rc-5')} == {
        'rc-2': str(root.resolve()), 'rc-3': other, 'rc-5': other}, (
        'a later command inherited the directory of a refused sibling')
    from lib.project_mod.config import get_conv_cwd
    from lib.tasks_pkg.workspace_delivery import execution_root_key
    assert get_conv_cwd(execution_root_key(task)) == other


@pytest.mark.parametrize('idle_reuse_hint', ['1', '32'])
def test_a_round_of_slow_writes_takes_about_its_slowest_call(
        script, rec, monkeypatch, idle_reuse_hint):
    """Measurement: N slow writes cost about max(call), not sum(calls).

    The idle-reuse hint never caps concurrency (docs/RESOURCE_CONTROL.md), so a
    one-thread hint still starts every write at once.
    """
    monkeypatch.setenv('TOOL_MAX_PARALLEL_WORKERS', idle_reuse_hint)
    seconds, count = 0.4, 6
    calls = []
    for number in range(count):
        call_id = f'write-{number}'
        script.behaviors[call_id] = lambda task: time.sleep(seconds)
        if number % 2:
            calls.append(_call(call_id, 'run_command', number + 1, {'command': 'make'}))
        else:
            calls.append(_call(call_id, 'write_file', number + 1,
                               {'path': f'out-{number}.txt', 'content': 'x'}))

    started = time.monotonic()
    replies, _messages = _run(_task(), calls)
    elapsed = time.monotonic() - started

    assert len(replies) == count
    assert seconds <= elapsed < seconds * count * 0.6, (
        f'{count} writes of {seconds}s took {elapsed:.2f}s; serial would be '
        f'{seconds * count:.1f}s')
