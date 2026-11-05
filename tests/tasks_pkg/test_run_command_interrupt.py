"""tests/tasks_pkg/test_run_command_interrupt.py — per-command interrupt (pt_232244fb).

The 2026-08-01 incident: a `find . -name 'bundle-*.js'` over the FUSE
workspace ran 1h12m with zero output; BOTH reaper liveness clocks went stale
and ``reap_stuck_running_tasks`` force-failed the WHOLE task
(``stuck_no_progress``). The owner directive: a stuck COMMAND is not a stuck
TASK — interrupt the command, hand the partial output back to the model, and
let the turn continue. Killing the task just pushed the recovery onto the
user, and a re-issued command would hit the exact same wall.

Four seams, one suite:

  1. ``run_command`` consumes ``task['_cmd_interrupt']`` (~0.2s granularity),
     kills the process tree, and formats the result as
     ``[Command interrupted by …]`` with the PARTIAL output preserved and
     ``task['aborted']`` untouched.
  2. The reaper, seeing a live run_command whose own output has been silent
     past the threshold, plants the watchdog interrupt for that command
     instead of reaping — escalating to a full reap only when the flag sits
     unconsumed past the grace window (the read loop itself is wedged).
  3. ``POST /api/v1/chat/interrupt-command/<task_id>`` plants the user
     interrupt; ``_build_run_command`` renders the interrupted badge.
  4. The SubAgent tool proxy (swarm workers AND FlowExecutor leaf workers)
     bridges the cooperative-control fields across its isolation membrane:
     ``_subprocess_pid`` proxy→parent so the reaper arms the gentle
     interrupt instead of force-failing the whole parent (an autopilot
     run), and ``_cmd_interrupt`` parent→proxy so the planted flag reaches
     the command's read loop.

Live-subprocess tests use real ``echo … && sleep 30`` commands — the
interrupt must arrive mid-run and the call must return long before the
sleep would end.
"""

import threading
import time

import pytest

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────
# 1. Flag consumption + result formatting
# ─────────────────────────────────────────────────────────────────────────
def test_pop_cmd_interrupt_user_consumed():
    from lib.project_mod.run_command import _pop_cmd_interrupt
    task = {'_cmd_interrupt': {'source': 'user', 'ts': time.time(), 'note': ''}}
    assert _pop_cmd_interrupt(task) == 'user'
    # Popped on read — a LATER command in the same task must not see it.
    assert '_cmd_interrupt' not in task
    assert _pop_cmd_interrupt(task) is None


def test_pop_cmd_interrupt_watchdog_label_carries_note_and_guidance():
    from lib.project_mod.run_command import _pop_cmd_interrupt
    task = {'_cmd_interrupt': {'source': 'watchdog', 'ts': 1.0,
                               'note': 'no output for 1818s'}}
    label = _pop_cmd_interrupt(task)
    assert label is not None
    assert 'stall-watchdog' in label
    assert 'no output for 1818s' in label
    # The model-facing guidance: the task continues; retry differently.
    assert 'NOT stopped' in label


def test_pop_cmd_interrupt_absent():
    from lib.project_mod.run_command import _pop_cmd_interrupt
    assert _pop_cmd_interrupt({}) is None
    assert _pop_cmd_interrupt(None) is None


def test_pop_cmd_interrupt_pid_match_consumed():
    from lib.project_mod.run_command import _pop_cmd_interrupt
    task = {'_cmd_interrupt': {'source': 'user', 'ts': 1.0, 'note': '', 'pid': 4321}}
    assert _pop_cmd_interrupt(task, 4321) == 'user'
    assert '_cmd_interrupt' not in task


def test_pop_cmd_interrupt_stale_pid_voided_never_honoured():
    """The race guard: a flag planted for a command that already EXITED must
    be voided, never consumed by the NEXT command in the same task —
    otherwise an interrupt meant for a finished command kills the next one
    at spawn."""
    from lib.project_mod.run_command import _pop_cmd_interrupt
    task = {'_cmd_interrupt': {'source': 'user', 'ts': 1.0, 'note': '', 'pid': 1111}}
    assert _pop_cmd_interrupt(task, 2222) is None, 'pid mismatch → not honoured'
    assert '_cmd_interrupt' not in task, 'stale flag is voided (popped), not left for later'


def test_pop_cmd_interrupt_legacy_flag_without_pid_still_honoured():
    from lib.project_mod.run_command import _pop_cmd_interrupt
    task = {'_cmd_interrupt': {'source': 'user', 'ts': 1.0, 'note': ''}}
    assert _pop_cmd_interrupt(task, 2222) == 'user'


def test_format_run_output_interrupted_preserves_partial():
    from lib.project_mod.run_command import _format_run_output
    out = _format_run_output('find . -name x', 'partial-line\n', '', -1,
                             interrupted_by='user')
    assert out.startswith('$ find . -name x\n')
    assert 'partial-line' in out
    assert '[Command interrupted by user]' in out
    assert out.rstrip().endswith('[exit code: -1]')


# ─────────────────────────────────────────────────────────────────────────
# 2. Live subprocess: interrupt mid-run, partial output preserved, task spared
# ─────────────────────────────────────────────────────────────────────────
def _interrupt_after(task, delay, flag):
    def _fire():
        time.sleep(delay)
        task['_cmd_interrupt'] = flag
    threading.Thread(target=_fire, daemon=True).start()


def test_run_command_simple_user_interrupt_live(tmp_path):
    from lib.project_mod.run_command import tool_run_command
    task = {'aborted': False}
    _interrupt_after(task, 0.6, {'source': 'user', 'ts': time.time(), 'note': ''})
    t0 = time.monotonic()
    out = tool_run_command(str(tmp_path), 'echo part1 && sleep 30', task=task)
    dt = time.monotonic() - t0
    assert dt < 15, f'interrupt must end the call long before the 30s sleep (took {dt:.1f}s)'
    assert 'part1' in out, 'partial stdout produced BEFORE the interrupt is preserved'
    assert '[Command interrupted by user]' in out
    assert out.rstrip().endswith('[exit code: -1]')
    # The interrupt is NOT an abort: the task flag is never touched, so the
    # orchestrator continues the turn with this tool result.
    assert task.get('aborted') is False
    assert '_subprocess_pid' not in task  # cleaned up


def test_run_command_simple_watchdog_interrupt_live(tmp_path):
    from lib.project_mod.run_command import tool_run_command
    task = {'aborted': False}
    _interrupt_after(task, 0.6, {'source': 'watchdog', 'ts': time.time(),
                                 'note': 'no output for 1818s'})
    t0 = time.monotonic()
    out = tool_run_command(str(tmp_path),
                           'echo committed >> effect.txt && echo part1 && sleep 30',
                           task=task)
    dt = time.monotonic() - t0
    assert dt < 15
    assert 'part1' in out
    assert '[Command interrupted by stall-watchdog: no output for 1818s' in out
    assert 'the task was NOT stopped' in out
    assert (tmp_path / 'effect.txt').read_text() == 'committed\n'
    assert 'may have partially taken effect' in out
    assert 'do not automatically replay writes' in out
    assert task['aborted'] is False


def test_run_command_interactive_user_interrupt_live(tmp_path):
    from lib.project_mod.run_command import tool_run_command
    task = {'aborted': False}
    _interrupt_after(task, 0.6, {'source': 'user', 'ts': time.time(), 'note': ''})
    t0 = time.monotonic()
    out = tool_run_command(str(tmp_path), 'echo part1 && sleep 30',
                           stdin_callback=lambda hint: None, task=task)
    dt = time.monotonic() - t0
    assert dt < 15, f'interactive interrupt must end the call promptly (took {dt:.1f}s)'
    assert 'part1' in out
    assert '[Command interrupted by user]' in out
    assert task.get('aborted') is False


# ─────────────────────────────────────────────────────────────────────────
# 2b. code_exec (standalone path, pt_0bde0fd8): execute_standalone_command
#     forwards task= — the subprocess REGISTERS, so the reaper can interrupt
#     it AND Stop can kill it. Before the passthrough both were dead code
#     under task=None.
# ─────────────────────────────────────────────────────────────────────────
def test_standalone_passthrough_registers_pid_and_interrupts(tmp_path):
    from lib.project_mod import execute_standalone_command
    task = {'aborted': False}
    seen_pid = []

    def _watch():
        for _ in range(50):
            if task.get('_subprocess_pid'):
                seen_pid.append(task['_subprocess_pid'])
                return
            time.sleep(0.05)
    threading.Thread(target=_watch, daemon=True).start()
    _interrupt_after(task, 0.8, {'source': 'user', 'ts': time.time(), 'note': ''})
    t0 = time.monotonic()
    out = execute_standalone_command('run_command',
                                     {'command': 'echo part1 && sleep 30'},
                                     working_dir=str(tmp_path), task=task)
    dt = time.monotonic() - t0
    assert seen_pid, ('the standalone path must REGISTER _subprocess_pid — '
                      'without it the reaper cannot interrupt a code_exec')
    assert dt < 15, f'interrupt must end the call promptly (took {dt:.1f}s)'
    assert 'part1' in out
    assert '[Command interrupted by user]' in out
    assert task.get('aborted') is False


def test_standalone_passthrough_stop_kills_subprocess(tmp_path):
    """The pre-existing hole this passthrough also closes: with task=None the
    aborted poll was dead code, so even STOP could not kill a code_exec
    subprocess — it ran to completion no matter what the user pressed."""
    from lib.project_mod import execute_standalone_command
    task = {'aborted': False}

    def _stop():
        time.sleep(0.8)
        task['aborted'] = True
    threading.Thread(target=_stop, daemon=True).start()
    t0 = time.monotonic()
    out = execute_standalone_command('run_command',
                                     {'command': 'echo part1 && sleep 30'},
                                     working_dir=str(tmp_path), task=task)
    dt = time.monotonic() - t0
    assert dt < 15, f'Stop must kill the standalone subprocess (took {dt:.1f}s)'
    assert '[Command aborted by user]' in out


def test_code_exec_handler_interrupted_meta(tmp_path, monkeypatch):
    """_handle_code_exec end-to-end: interrupted command → amber meta
    (interrupted badge, marker stripped, partial output kept), never the
    red `exit -1` frame — the task CONTINUED."""
    import lib.tasks_pkg.handlers.code_exec as ce
    captured = {}
    monkeypatch.setattr(ce, '_finalize_tool_round',
                        lambda task, rn, round_entry, metas: captured.update(
                            {'metas': metas}))
    monkeypatch.setattr(ce, 'append_event', lambda *a, **k: None)
    task = {'id': 'ce-intr-1', 'aborted': False}
    # Delay the interrupt so 'part1' lands in the pipe FIRST — an upfront
    # flag is consumed at the loop's first tick, before any output arrives.
    _interrupt_after(task, 0.6, {'source': 'user', 'ts': time.time(), 'note': ''})
    round_entry = {'toolCallId': 'tc-ce-1', 'toolName': 'code_exec',
                   'status': 'searching'}
    t0 = time.monotonic()
    # fn_name stays the MODEL's call name ('run_command') — the special
    # dispatch keys off round_entry['toolName'] == 'code_exec', and
    # execute_standalone_command only accepts 'run_command'.
    tc_id, content, _ = ce._handle_code_exec(
        task, None, 'run_command', 'tc-ce-1',
        {'command': 'echo part1 && sleep 30'}, 1, round_entry,
        {}, str(tmp_path), False)
    dt = time.monotonic() - t0
    assert dt < 15, f'interrupt must end the handler promptly (took {dt:.1f}s)'
    assert '[Command interrupted by user]' in content
    meta = captured['metas'][0]
    assert meta['interrupted'] is True
    assert meta['badge'] == 'interrupted'
    assert meta['exitCode'] == '-1'
    assert not meta.get('notRun'), 'an interrupted command DID run'
    assert 'part1' in meta['output']
    assert '[Command interrupted' not in meta['output']
    assert task.get('aborted') is False


# ─────────────────────────────────────────────────────────────────────────
# 3. Reaper: interrupt the command, spare the task (escalate only on a
#    genuinely wedged read loop)
# ─────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def reaper_env(monkeypatch):
    monkeypatch.setenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', '300')
    monkeypatch.setenv('TOFU_CMD_INTERRUPT_GRACE_SECS', '120')
    return 300


@pytest.fixture()
def put_task(monkeypatch):
    """Insert synthetic tasks into the registry; stub the finalizer (no DB)."""
    from tests.support.chat_tasks import (
        chat_task_fixture_guard as tasks_lock,
        chat_task_registry as tasks,
        isolated_reaper_registry,
    )
    from lib.tasks_pkg.manager import _maintenance

    reaper_tasks = isolated_reaper_registry(monkeypatch)
    monkeypatch.setattr(_maintenance, '_finalize_reaped_stuck_task',
                        lambda t: None, raising=True)
    added = []

    def _put(task):
        with tasks_lock:
            tasks[task['id']] = task
            reaper_tasks[task['id']] = task
        added.append(task['id'])
        return task['id']

    yield _put

    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)


def _mk_task(task_id, **fields):
    t = {
        'id': task_id,
        'convId': 'cv-' + task_id,
        'status': 'running',
        'aborted': False,
        'content': '',
        'thinking': '',
        'events': [],
        'events_lock': threading.Lock(),
        'config': {'model': 'kimi-k3'},
        'created_at': time.time(),
    }
    t.update(fields)
    return t


def _reap():
    from lib.tasks_pkg.manager import reap_stuck_running_tasks
    return reap_stuck_running_tasks()


def _get(task_id):
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    with tasks_lock:
        return dict(tasks.get(task_id) or {})


def _live_command(task, pid):
    """Publish one foreground command the way a run_command runner does."""
    from lib.project_mod.foreground_command_slots import command_task_view
    view = command_task_view(task)
    view['_subprocess_pid'] = pid
    view['_subprocess_pgid'] = pid
    return view


def test_reaper_interrupts_command_instead_of_killing_task(monkeypatch, put_task):
    """The incident shape: wedged-looking task blocked INSIDE run_command."""
    monkeypatch.setenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', '1')
    now = time.time()
    stale = now - 400
    task = _mk_task(
        'cmd-blocked-1',
        content='ok',
        events=[{'type': 'delta', 'seq': 0}],
        _t_last_event=stale,
        _dispatch_heartbeat=stale,
        created_at=stale,
    )
    put_task(task)
    _live_command(task, 999999)     # inside a run_command subprocess
    time.sleep(1.2)                 # the command itself prints nothing
    n = _reap()
    assert n == 0, 'a command-blocked task must NOT be force-failed'
    t = _get('cmd-blocked-1')
    assert t['status'] == 'running'
    assert t['aborted'] is False
    intr = t.get('_cmd_interrupt')
    assert isinstance(intr, dict), 'the watchdog interrupt must be planted'
    assert intr['source'] == 'watchdog'
    assert 'no output for' in intr['note']
    assert intr['pid'] == 999999, 'the planter stamps the pid it saw (stale-flag guard)'


def test_reaper_does_not_replant_fresh_interrupt(reaper_env, put_task):
    now = time.time()
    stale = now - 400
    issued = now - 5  # planted 5s ago, well inside the 120s grace
    task = _mk_task(
        'cmd-blocked-2',
        _t_last_event=stale, _dispatch_heartbeat=stale, created_at=stale,
        _cmd_interrupt={'source': 'watchdog', 'ts': issued, 'note': 'x'},
    )
    put_task(task)
    _live_command(task, 999999)
    n = _reap()
    assert n == 0
    t = _get('cmd-blocked-2')
    assert t['status'] == 'running'
    assert t['_cmd_interrupt']['ts'] == issued, 'a fresh pending interrupt is left alone'


def test_reaper_escalates_when_interrupt_unconsumed(reaper_env, put_task):
    """The flag sat UNCONSUMED past the grace → the read loop itself is wedged
    (not just the subprocess) → the full task reap is the only recovery left."""
    now = time.time()
    stale = now - 400
    task = _mk_task(
        'cmd-wedged-loop-1',
        _t_last_event=stale, _dispatch_heartbeat=stale, created_at=stale,
        _cmd_interrupt={'source': 'watchdog', 'ts': now - 300, 'note': 'x'},  # > 120s grace
    )
    put_task(task)
    _live_command(task, 999999)
    n = _reap()
    assert n == 1, 'an unconsumed interrupt past grace escalates to a full reap'
    t = _get('cmd-wedged-loop-1')
    assert t['status'] == 'error'
    assert t['aborted'] is True


def test_reaper_without_subprocess_still_reaps(reaper_env, put_task):
    """NEUTER guard for the new branch: no live command → the classic
    force-fail path is untouched (dead socket / hung MCP / stalled browser)."""
    now = time.time()
    stale = now - 400
    put_task(_mk_task(
        'truly-wedged-1',
        _t_last_event=stale, _dispatch_heartbeat=stale, created_at=stale,
    ))
    n = _reap()
    assert n == 1, 'a wedged task NOT inside run_command is still reaped'
    t = _get('truly-wedged-1')
    assert t['status'] == 'error'
    assert t['_abort_reason'] == 'stuck_no_progress'


# ─────────────────────────────────────────────────────────────────────────
# 4. Meta builder: interrupted badge, marker stripped, never "not run"
# ─────────────────────────────────────────────────────────────────────────
def test_meta_run_command_interrupted_badge():
    from lib.tools.meta import build_project_tool_meta
    content = ('$ find . -name bundle.js\n'
               './static/js/bundle-7cf4e429.js\n'
               '\n[Command interrupted by user]\n[exit code: -1]')
    meta = build_project_tool_meta('run_command',
                                   {'command': 'find . -name bundle.js'}, content)
    assert meta['interrupted'] is True
    assert meta['badge'] == 'interrupted'
    assert meta['exitCode'] == '-1'
    assert not meta.get('notRun'), 'an interrupted command DID run — never render "not run"'
    assert '[Command interrupted' not in meta['output']
    assert 'bundle-7cf4e429.js' in meta['output']


def test_meta_run_command_watchdog_interrupted_badge():
    from lib.tools.meta import build_project_tool_meta
    content = ('$ grep -rn foo .\n'
               '\n[Command interrupted by stall-watchdog: no output for 1818s — '
               'partial output above; the task was NOT stopped. Retry with a '
               'narrower scope or an explicit timeout.]\n[exit code: -1]')
    meta = build_project_tool_meta('run_command',
                                   {'command': 'grep -rn foo .'}, content)
    assert meta['interrupted'] is True
    assert meta['badge'] == 'interrupted'
    assert '[Command interrupted' not in meta['output']


# ─────────────────────────────────────────────────────────────────────────
# 5. Endpoint: POST /api/v1/chat/interrupt-command/<task_id>
# ─────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def reg_task():
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    added = []

    def _put(task):
        with tasks_lock:
            tasks[task['id']] = task
        added.append(task['id'])
        return task['id']

    yield _put

    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)


def test_endpoint_interrupts_active_command(flask_client, reg_task):
    reg_task(_mk_task('ep-intr-1', _subprocess_pid=424242, _subprocess_pgid=424242))
    resp = flask_client.post('/api/v4/chat/interrupt-command/ep-intr-1')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True
    assert body['interrupted'] is True
    assert body['pid'] == 424242
    t = _get('ep-intr-1')
    intr = t.get('_cmd_interrupt')
    assert isinstance(intr, dict) and intr['source'] == 'user'
    assert intr['pid'] == 424242, 'the planter stamps the pid it saw (stale-flag guard)'
    assert t.get('aborted') is False, 'interrupt must never abort the task'


def test_endpoint_no_active_command(flask_client, reg_task):
    reg_task(_mk_task('ep-intr-2'))
    resp = flask_client.post('/api/v4/chat/interrupt-command/ep-intr-2')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['interrupted'] is False
    assert body['reason'] == 'no_active_command'
    assert '_cmd_interrupt' not in _get('ep-intr-2')


def test_endpoint_task_not_running(flask_client, reg_task):
    reg_task(_mk_task('ep-intr-3', status='done', _subprocess_pid=424242))
    resp = flask_client.post('/api/v4/chat/interrupt-command/ep-intr-3')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['interrupted'] is False
    assert body['reason'] == 'task_not_running'
    assert '_cmd_interrupt' not in _get('ep-intr-3')


def test_endpoint_unknown_task_404(flask_client):
    resp = flask_client.post('/api/v4/chat/interrupt-command/ep-intr-missing')
    assert resp.status_code == 404


def test_endpoint_handoff_requires_owned_exact_running_command(flask_client, reg_task):
    from lib.tasks_pkg.handlers._background_command import CommandHandoffControl

    owner = _mk_task('ep-handoff-1', _subprocess_pid=424242)
    owner['toolRounds'] = []
    control = CommandHandoffControl(owner, 3, 'call-3')
    control.note_pid(424242)
    owner['_cmd_handoff_control'] = control
    reg_task(owner)
    old = flask_client.post('/api/v4/chat/background-command/ep-handoff-1',
                            json={'roundNum': 2, 'toolCallId': 'call-2'})
    assert old.status_code == 200
    assert old.get_json()['reason'] == 'command_changed'
    # No orphaned handoff request can later affect a different command.
    assert control.requested_for(424242) is False
    bad = flask_client.post('/api/v4/chat/background-command/ep-handoff-1',
                            json={'roundNum': 3})
    assert bad.status_code == 400
    control.resolve(True)
    ok = flask_client.post('/api/v4/chat/background-command/ep-handoff-1',
                           json={'roundNum': 3, 'toolCallId': 'call-3'})
    assert ok.status_code == 200
    assert ok.get_json()['backgrounded'] is True


def test_endpoint_handoff_cannot_cross_owner(flask_client, reg_task):
    from lib.tasks_pkg.handlers._background_command import CommandHandoffControl

    foreign = _mk_task('ep-handoff-foreign', _userId=9837, _subprocess_pid=42)
    control = CommandHandoffControl(foreign, 1, 'call-foreign')
    control.note_pid(42)
    foreign['_cmd_handoff_control'] = control
    reg_task(foreign)
    result = flask_client.post('/api/v4/chat/background-command/ep-handoff-foreign',
                               json={'roundNum': 1, 'toolCallId': 'call-foreign'})
    assert result.status_code == 404
    assert control.requested_for(42) is False


def test_endpoint_handoff_missing_or_finished_is_never_accepted(flask_client, reg_task):
    assert flask_client.post('/api/v4/chat/background-command/unknown',
                             json={'roundNum': 1, 'toolCallId': 'call'}).status_code == 404
    reg_task(_mk_task('ep-handoff-finished', status='done', _subprocess_pid=123))
    finished = flask_client.post('/api/v4/chat/background-command/ep-handoff-finished',
                                 json={'roundNum': 1, 'toolCallId': 'call'})
    assert finished.get_json()['reason'] == 'task_not_running'
    reg_task(_mk_task('ep-handoff-empty'))
    empty = flask_client.post('/api/v4/chat/background-command/ep-handoff-empty',
                              json={'roundNum': 1, 'toolCallId': 'call'})
    assert empty.get_json()['reason'] == 'no_active_command'


def test_endpoint_handoff_refuses_stop_stdin_and_interrupt(
        flask_client, reg_task):
    from lib.tasks_pkg.handlers._background_command import CommandHandoffControl

    task = _mk_task('ep-handoff-refuse', _subprocess_pid=424242,
                    abort_event=threading.Event(), toolRounds=[])
    control = CommandHandoffControl(task, 3, 'call-3')
    control.note_pid(424242)
    task['_cmd_handoff_control'] = control
    reg_task(task)
    url = '/api/v4/chat/background-command/ep-handoff-refuse'
    body = {'roundNum': 3, 'toolCallId': 'call-3'}

    task['toolRounds'].append({'status': 'awaiting_stdin'})
    assert flask_client.post(url, json=body).get_json()['reason'] == 'awaiting_stdin'
    task['toolRounds'].clear()
    task['_cmd_interrupt'] = {'pid': 424242, 'source': 'user'}
    assert flask_client.post(url, json=body).get_json()['reason'] == 'command_interrupting'
    task.pop('_cmd_interrupt')
    task['abort_event'].set()
    assert flask_client.post(url, json=body).get_json()['reason'] == 'task_not_running'
    assert control.requested_for(424242) is False


# ─────────────────────────────────────────────────────────────────────────
# 6. SubAgent proxy bridge: the reaper's gentle interrupt must arm for
#    swarm workers and FlowExecutor leaf workers (autopilot), which execute
#    tools against an ISOLATED task_proxy — before the bridge, the live
#    subprocess was invisible on the parent, so a silent leaf-worker command
#    escalated straight to force-failing the whole parent task/autopilot run.
# ─────────────────────────────────────────────────────────────────────────
def _wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _bare_subagent(parent):
    """A SubAgent shell carrying only the attributes ``_dispatch_tool`` reads
    (the full __init__ wires LLM state these tests never touch)."""
    from types import SimpleNamespace
    from lib.swarm.agent import SubAgent
    agent = object.__new__(SubAgent)
    agent.agent_id = 'agent:test'
    agent.spec = SimpleNamespace(id='sub-1')
    agent.parent_task = parent
    agent.abort_check = lambda: False
    agent._run_deadline_monotonic = None
    agent.project_path = ''
    agent.tools = []
    agent.model = 'kimi-k3'
    agent.thinking_enabled = False
    agent._tool_contract_documents_by_name = {}
    agent._ptc_local = None
    return agent







# ─────────────────────────────────────────────────────────────────────────
# 7. Concurrent foreground commands of one unattended task (swarm, Goal and
#    Flow workers, headless requests). Every call of a round runs at once
#    (owner decision 2026-10-28) and these tasks never hand a command off, so
#    their commands run on the task itself: each must stay individually
#    interruptible, watched and stoppable.
# ─────────────────────────────────────────────────────────────────────────
class _WorkerCommand:
    """One real foreground command of an unattended worker, on its own thread."""

    def __init__(self, task, workdir, name, script):
        from lib.project_mod.run_command import tool_run_command
        from lib.tasks_pkg.handlers._background_command import run_with_steer_handoff
        self.results = []
        pid_file = workdir / f'{name}.pid'
        command = f'echo $$ > {name}.pid; {script}'

        def run():
            self.results.append(run_with_steer_handoff(
                task=task, config={}, command=command,
                execute=lambda command_task: tool_run_command(
                    str(workdir), command, task=command_task)))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert _wait_for(lambda: pid_file.exists() and pid_file.read_text().strip())
        self.pid = int(pid_file.read_text())
        assert _wait_for(lambda: task.get('_subprocess_pid') == self.pid), (
            'the newest command must own the task PID mirror')

    def result(self, timeout):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), 'the command is still running'
        return self.results[0]


def _worker_task(task_id, **fields):
    return _mk_task(task_id, _unattended=True, _userId=7, **fields)


def _assert_no_command_slots(task):
    leftovers = {key for key in ('_subprocess_pid', '_subprocess_pgid',
                                 '_cmd_interrupt', '_foregroundCommands')
                 if key in task}
    assert not leftovers, f'command slots outlived their commands: {leftovers}'


def test_worker_interrupt_stops_only_the_command_it_names(tmp_path):
    task = _worker_task('worker-intr-1')
    older = _WorkerCommand(task, tmp_path, 'older', 'echo older-start; sleep 30')
    # The chatty sibling's read loop sees the flag long before its silent
    # target's 0.2 s tick does; it must leave the flag for the target.
    newer = _WorkerCommand(
        task, tmp_path, 'newer',
        'for i in $(seq 1 300); do echo newer-$i; sleep 0.01; done; echo newer-done')

    task['_cmd_interrupt'] = {'source': 'user', 'ts': time.time(), 'note': '',
                              'pid': older.pid}

    interrupted = older.result(10)
    assert 'older-start' in interrupted
    assert '[Command interrupted by user]' in interrupted
    assert newer.thread.is_alive(), 'interrupting one command stopped its sibling'
    assert task.get('_subprocess_pid') == newer.pid, (
        'the live command lost its PID mirror when its sibling exited')
    finished = newer.result(15)
    assert 'newer-done' in finished and '[Command interrupted' not in finished
    assert finished.rstrip().endswith('[exit code: 0]')
    assert task['aborted'] is False
    _assert_no_command_slots(task)


def test_worker_watchdog_interrupts_a_stalled_command_after_its_sibling_exits(
        monkeypatch, put_task, tmp_path):
    monkeypatch.setenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', '1')
    stale = time.time() - 400
    task = _worker_task('worker-watchdog-1', created_at=stale,
                        _t_last_event=stale, _dispatch_heartbeat=stale)
    put_task(task)
    stalled = _WorkerCommand(task, tmp_path, 'stalled', 'echo stalled-start; sleep 30')
    quick = _WorkerCommand(task, tmp_path, 'quick', 'sleep 0.3; echo quick-done')
    assert 'quick-done' in quick.result(10)
    assert task.get('_subprocess_pid') == stalled.pid, (
        'the stalled command vanished from the PID mirror')
    time.sleep(1.2)

    assert _reap() == 0, 'a stalled command must not fail its worker task'

    result = stalled.result(10)
    assert '[Command interrupted by stall-watchdog: no output for' in result
    assert 'stalled-start' in result
    assert task['status'] == 'running' and task['aborted'] is False
    _assert_no_command_slots(task)


def test_worker_watchdog_interrupts_only_the_silent_command(monkeypatch, put_task, tmp_path):
    monkeypatch.setenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', '1')
    stale = time.time() - 400
    task = _worker_task('worker-watchdog-2', created_at=stale,
                        _t_last_event=stale, _dispatch_heartbeat=stale)
    put_task(task)
    silent = _WorkerCommand(task, tmp_path, 'silent', 'echo silent-start; sleep 30')
    chatty = _WorkerCommand(
        task, tmp_path, 'chatty',
        'for i in $(seq 1 50); do echo chatty-$i; sleep 0.1; done; echo chatty-done')
    time.sleep(1.5)

    assert _reap() == 0

    assert '[Command interrupted by stall-watchdog' in silent.result(10)
    assert chatty.thread.is_alive(), 'the watchdog interrupted a command that prints'
    finished = chatty.result(15)
    assert 'chatty-done' in finished and '[Command interrupted' not in finished
    assert task['status'] == 'running' and task['aborted'] is False
    _assert_no_command_slots(task)


def test_worker_stop_kills_every_foreground_command(tmp_path):
    task = _worker_task('worker-stop-1')
    commands = [_WorkerCommand(task, tmp_path, name, f'echo {name}-start; sleep 30')
                for name in ('first', 'second')]

    task['aborted'] = True

    for command in commands:
        assert '[Command aborted by user]' in command.result(10)
    _assert_no_command_slots(task)


def test_worker_commands_each_run_in_the_background_beside_a_foreground_one(
        monkeypatch, tmp_path):
    """A worker never hands a running command off (it has no conversation
    delivery); each concurrent call chooses ``background=true`` instead and
    reads its own handle with ``poll``."""
    from lib.project_mod.run_command import tool_run_command
    from lib.tasks_pkg import background_commands as bg
    monkeypatch.setattr(bg, '_records_root', lambda: str(tmp_path / 'records'))
    task = _worker_task('worker-bg-1', convId='coordinator-conv')
    task['_parentTaskId'] = 'coordinator-task'
    foreground = _WorkerCommand(task, tmp_path, 'foreground', 'echo fg-start; sleep 30')

    def start(script):
        return bg.start_background_command(
            task=task, command=script,
            execute=lambda command_task, _context: tool_run_command(
                str(tmp_path), script, task=command_task))

    killed = start('echo kill-start; sleep 30')
    kept = start('sleep 0.5; echo kept-done')
    assert _wait_for(lambda: bg.check_background_command(
        killed, user_id=7)['status'] == 'running')
    assert bg.kill_background_command(killed, user_id=7)
    kept_status = bg.check_background_command(kept, user_id=7, wait_s=10)
    assert kept_status['status'] == 'completed'
    assert 'kept-done' in kept_status['result']
    assert foreground.thread.is_alive()
    assert task['_subprocess_pid'] == foreground.pid, (
        'a background command took over the foreground PID mirror')

    task['_cmd_interrupt'] = {'source': 'user', 'ts': time.time(), 'note': '',
                              'pid': foreground.pid}
    assert '[Command interrupted by user]' in foreground.result(10)
    assert bg.check_background_command(killed, user_id=7, wait_s=10)['status'] != 'running'
    _assert_no_command_slots(task)
