"""tests/test_run_command_interrupt.py — per-command interrupt (pt_232244fb).

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
  2. The reaper, seeing a wedged task blocked INSIDE a run_command
     (``_subprocess_pid`` set), plants the watchdog interrupt instead of
     reaping — escalating to a full reap only when the flag sits unconsumed
     past the grace window (the read loop itself is wedged).
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
    out = tool_run_command(str(tmp_path), 'echo part1 && sleep 30', task=task)
    dt = time.monotonic() - t0
    assert dt < 15
    assert 'part1' in out
    assert '[Command interrupted by stall-watchdog: no output for 1818s' in out
    assert 'the task was NOT stopped' in out


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
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    from lib.tasks_pkg.manager import _maintenance

    monkeypatch.setattr(_maintenance, '_finalize_reaped_stuck_task',
                        lambda t: None, raising=True)
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


def test_reaper_interrupts_command_instead_of_killing_task(reaper_env, put_task):
    """The incident shape: wedged-looking task blocked INSIDE run_command."""
    now = time.time()
    stale = now - 400
    put_task(_mk_task(
        'cmd-blocked-1',
        content='ok',
        events=[{'type': 'delta', 'seq': 0}],
        _t_last_event=stale,
        _dispatch_heartbeat=stale,
        created_at=stale,
        _subprocess_pid=999999,     # inside a run_command subprocess
        _subprocess_pgid=999999,
    ))
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
    put_task(_mk_task(
        'cmd-blocked-2',
        _t_last_event=stale, _dispatch_heartbeat=stale, created_at=stale,
        _subprocess_pid=999999,
        _cmd_interrupt={'source': 'watchdog', 'ts': issued, 'note': 'x'},
    ))
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
    put_task(_mk_task(
        'cmd-wedged-loop-1',
        _t_last_event=stale, _dispatch_heartbeat=stale, created_at=stale,
        _subprocess_pid=999999,
        _cmd_interrupt={'source': 'watchdog', 'ts': now - 300, 'note': 'x'},  # > 120s grace
    ))
    n = _reap()
    assert n == 1, 'an unconsumed interrupt past grace escalates to a full reap'
    t = _get('cmd-wedged-loop-1')
    assert t['status'] == 'error'
    assert t['aborted'] is True


def test_reaper_without_subprocess_still_reaps(reaper_env, put_task):
    """NEUTER guard for the new branch: no ``_subprocess_pid`` → the classic
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
    resp = flask_client.post('/api/v1/chat/interrupt-command/ep-intr-1')
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
    resp = flask_client.post('/api/v1/chat/interrupt-command/ep-intr-2')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['interrupted'] is False
    assert body['reason'] == 'no_active_command'
    assert '_cmd_interrupt' not in _get('ep-intr-2')


def test_endpoint_task_not_running(flask_client, reg_task):
    reg_task(_mk_task('ep-intr-3', status='done', _subprocess_pid=424242))
    resp = flask_client.post('/api/v1/chat/interrupt-command/ep-intr-3')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['interrupted'] is False
    assert body['reason'] == 'task_not_running'
    assert '_cmd_interrupt' not in _get('ep-intr-3')


def test_endpoint_unknown_task_404(flask_client):
    resp = flask_client.post('/api/v1/chat/interrupt-command/ep-intr-missing')
    assert resp.status_code == 404


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


def test_subagent_bridge_mirrors_pid_and_transfers_interrupt(monkeypatch):
    parent = {'id': 'p1', 'convId': 'cv-p1', 'config': {}, 'aborted': False}
    agent = _bare_subagent(parent)
    seen = {}

    def fake_exec(task_proxy, *a):
        # run_command registers its subprocess on the PROXY.
        task_proxy['_subprocess_pid'] = 4321
        task_proxy['_subprocess_pgid'] = 4321
        assert _wait_for(lambda: parent.get('_subprocess_pid') == 4321), \
            'the bridge must mirror the live pid onto the parent (the reaper scans the parent)'
        seen['mirrored'] = True
        # The reaper plants a watchdog interrupt on the PARENT.
        parent['_cmd_interrupt'] = {'source': 'watchdog', 'ts': time.time(),
                                    'note': 'no output for 1818s', 'pid': 4321}
        assert _wait_for(lambda: '_cmd_interrupt' in task_proxy), \
            'the bridge must transfer the planted interrupt into the proxy'
        seen['transferred'] = True
        assert '_cmd_interrupt' in parent, \
            'the parent copy is retained until the read loop consumes it'
        # The read loop consumes it.
        task_proxy.pop('_cmd_interrupt')
        assert _wait_for(lambda: '_cmd_interrupt' not in parent), \
            'consumption must retract the parent copy (the reaper treats it as acknowledged)'
        seen['acked'] = True
        task_proxy.pop('_subprocess_pid')
        task_proxy.pop('_subprocess_pgid')
        return 'tc-1', 'partial output', None

    monkeypatch.setattr('lib.tasks_pkg.executor._execute_tool_one', fake_exec)
    out = agent._dispatch_tool({'id': 'tc-1'}, 'run_command',
                               {'command': 'sleep 30'}, 1)
    assert out == 'partial output'
    assert seen == {'mirrored': True, 'transferred': True, 'acked': True}
    assert '_subprocess_pid' not in parent, \
        'the mirrored pid is retracted when the command finishes'
    assert '_subprocess_pgid' not in parent


def test_subagent_bridge_skips_foreign_pid_interrupt(monkeypatch):
    """An interrupt stamped for a DIFFERENT pid (its command already exited)
    is stale — the bridge must not push it into this command's proxy."""
    parent = {'id': 'p2', 'convId': 'cv-p2', 'config': {}, 'aborted': False}
    agent = _bare_subagent(parent)
    observed = {}

    def fake_exec(task_proxy, *a):
        task_proxy['_subprocess_pid'] = 4321
        assert _wait_for(lambda: parent.get('_subprocess_pid') == 4321)
        parent['_cmd_interrupt'] = {'source': 'watchdog', 'ts': time.time(),
                                    'note': 'x', 'pid': 9999}
        time.sleep(0.5)
        observed['proxy_saw'] = '_cmd_interrupt' in task_proxy
        task_proxy.pop('_subprocess_pid', None)
        return 'tc-2', 'out', None

    monkeypatch.setattr('lib.tasks_pkg.executor._execute_tool_one', fake_exec)
    agent._dispatch_tool({'id': 'tc-2'}, 'run_command', {'command': 'sleep 1'}, 1)
    assert observed['proxy_saw'] is False
    assert parent['_cmd_interrupt']['pid'] == 9999, \
        'the foreign flag stays on the parent untouched'


def test_reaper_interrupt_reaches_subagent_command_via_bridge(
        reaper_env, put_task, monkeypatch):
    """End-to-end: a leaf worker blocked in a silent run_command — the reaper
    sees the BRIDGED pid on the parent, plants the watchdog interrupt, the
    bridge delivers it to the proxy read loop, and the parent task (the
    autopilot run) is NOT reaped."""
    now = time.time()
    stale = now - 400
    parent = _mk_task('subagent-cmd-1',
                      _t_last_event=stale, _dispatch_heartbeat=stale,
                      created_at=stale)
    put_task(parent)
    agent = _bare_subagent(parent)
    consumed = []

    def fake_exec(task_proxy, *a):
        task_proxy['_subprocess_pid'] = 4321
        assert _wait_for(lambda: '_cmd_interrupt' in task_proxy, timeout=10), \
            'the planted watchdog interrupt must reach the proxy read loop'
        consumed.append(task_proxy.pop('_cmd_interrupt'))
        task_proxy.pop('_subprocess_pid', None)
        return 'tc-3', 'partial output', None

    monkeypatch.setattr('lib.tasks_pkg.executor._execute_tool_one', fake_exec)
    worker = threading.Thread(
        target=lambda: agent._dispatch_tool({'id': 'tc-3'}, 'run_command',
                                            {'command': 'sleep 30'}, 1),
        daemon=True)
    worker.start()
    assert _wait_for(lambda: parent.get('_subprocess_pid') == 4321)
    n = _reap()
    assert n == 0, 'a bridged command must interrupt, not reap, the parent task'
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert consumed, 'the proxy read loop consumed the interrupt'
    t = _get('subagent-cmd-1')
    assert t['status'] == 'running'
    assert t['aborted'] is False
    assert '_cmd_interrupt' not in t, 'consumption retracts the parent copy'
    assert '_subprocess_pid' not in t
