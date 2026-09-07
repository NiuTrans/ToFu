"""tests/test_remote_stream_ui.py — RWA P4b-2b:远程 run_command 流帧 UI.

设计要点(docs/modules/remote_execution.md):**前端零改动** ——
服务器 run_command 的实时输出通道(``_make_run_command_progress_cb`` →
``tool_progress`` SSE → tool_rounds 终端块)已存在,远程路径只做:
  * ``send_desktop_command`` 接受预置 ``cmd_id``;
  * 远程 handler 在阻塞等待期间起 watcher,把桥内流帧增量喂进同一个
    progress cb(去重:按偏移只发新增);
  * meta 按终端块契约成型(toolName/command/output/exitCode);
  * ``GET /api/v1/desktop/streams/<cmd_id>`` 调试端点。

e2e 用**真桥 + 真 agent 执行器**(无 mock):handler 线程 ↔ 假 agent
poll 线程经 command_queue/streams 真互通。

Run:  pytest tests/test_remote_stream_ui.py -m unit -v
"""

from __future__ import annotations

import threading
import time

import pytest

from lib.desktop import bridge as db


@pytest.fixture(autouse=True)
def _clean_bridge(monkeypatch):
    monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
    monkeypatch.setattr(db, '_last_poll', [0.0])
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
        db._streams.clear()
    with db._async_waiters_lock:
        db._async_waiters.clear()
    yield
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
        db._streams.clear()
    with db._async_waiters_lock:
        db._async_waiters.clear()


def _run_async(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
#  cmd_id 预置
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_send_accepts_preset_cmd_id():
    captured = {}

    def agent():
        cmds = _run_async(db.take_pending_commands_async(
            timeout=1, agent_id='agent-A', user_id='owner'))
        captured['cmds'] = cmds

    db.register_agent('agent-A', {'name': 'mac'}, user_id='owner')
    t = threading.Thread(target=agent)
    t.start()
    time.sleep(0.1)

    def producer():
        db.send_desktop_command('desktop_list_files', {}, timeout=2,
                                target_agent_id='agent-A',
                                user_id='owner',
                                cmd_id='cmd-preset-1')

    p = threading.Thread(target=producer)
    p.start()
    t.join(timeout=3)
    assert captured['cmds'][0]['id'] == 'cmd-preset-1'
    db.resolve_results(
        [{'id': 'cmd-preset-1', 'result': {}, 'error': None}],
        agent_id='agent-A', user_id='owner')
    p.join(timeout=3)


# ═══════════════════════════════════════════════════════════
#  e2e:真桥 + 真 agent 流式执行 → tool_progress 增量事件
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_remote_run_streams_via_tool_progress(tmp_path, monkeypatch):
    """handler(任务线程) ↔ 假 agent(poll 线程)真互通:
    命令在飞时 progress 事件已含前半输出,终态 meta 按终端块契约成型."""
    import lib.tasks_pkg.handlers.project as hp
    from lib.desktop_agent import _project as pj

    root = tmp_path / 'app'
    root.mkdir()
    cfg_file = tmp_path / 'cfg.json'
    cfg_file.write_text(
        '{"share_roots": [{"name": "myapp", "path": "%s"}]}' % root,
        encoding='utf-8')
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(cfg_file))

    db.register_agent('agent-A', {
        'name': 'mac',
        'share_roots': [{'name': 'myapp', 'path': str(root)}]},
        user_id='owner')

    events = []
    monkeypatch.setattr(hp, '_finalize_tool_round', lambda *a: None)
    # progress cb 走 code_exec 模块的 append_event,patch 它的导入点
    import lib.tasks_pkg.handlers.code_exec as ce
    monkeypatch.setattr(ce, 'append_event',
                        lambda task, ev: events.append(ev) or None)

    task = {'id': 'task-e2e', 'convId': 'conv-e2e', '_userId': 'owner'}
    round_entry = {'toolCallId': 'tc-1', 'toolName': 'run_command'}
    outcome = {}

    def run_handler():
        outcome['r'] = hp._execute_remote_project_tool(
            task, 'run_command', 'tc-1',
            {'command': "printf 'AAA\\n'; sleep 1.0; printf 'BBB\\n'"},
            1, round_entry, {'agent_id': 'agent-A', 'root': 'myapp'})

    def fake_agent():
        # 真 poll:等到命令到达 → 真流式执行 → 真流帧/结果回桥
        cmds = _run_async(db.take_pending_commands_async(
            timeout=5, agent_id='agent-A', user_id='owner'))
        assert cmds and cmds[0]['type'] == 'project_run_command'
        cmd = cmds[0]
        seq = [0]
        frames = []

        def on_chunk(stream, data):
            seq[0] += 1
            frame = {'cmd_id': cmd['id'], 'seq': seq[0],
                     'stream': stream, 'data': data, 'done': False}
            frames.append(frame)
            # 仿真真 agent 的 outbox:帧随下一次 poll 即时上行,
            # 不是等命令退出才批量回传(否则桥内 mid-flight 无数据)。
            db.resolve_streams(
                [frame], agent_id='agent-A', user_id='owner')

        def on_exit(res):
            seq[0] += 1
            frames.append({'cmd_id': cmd['id'], 'seq': seq[0],
                           'stream': 'meta', 'data': '', 'done': True})
            # agent 的下一次 poll 会把 frames + result 一起带上
            db.resolve_streams(
                frames, agent_id='agent-A', user_id='owner')
            db.resolve_results(
                [{'id': cmd['id'], 'result': res, 'error': None}],
                agent_id='agent-A', user_id='owner')

        err = pj.start_project_run(cmd['id'], cmd['params'],
                                   on_chunk, on_exit)
        assert err is None

    ht = threading.Thread(target=run_handler)
    at = threading.Thread(target=fake_agent)
    ht.start()
    time.sleep(0.15)   # handler 先入队
    at.start()
    # 命令在飞期间(sleep 1.0):progress 事件应已含 AAA
    time.sleep(0.7)
    mid = ''.join(ev.get('chunk', '') for ev in events
                  if ev.get('type') == 'tool_progress')
    ht.join(timeout=10)
    at.join(timeout=10)

    tc_id, content, _ = outcome['r']
    assert 'AAA' in mid, f'在飞期间无流式增量: mid={mid!r}'
    all_chunks = ''.join(ev.get('chunk', '') for ev in events
                         if ev.get('type') == 'tool_progress')
    assert 'AAA' in all_chunks and 'BBB' in all_chunks
    assert 'AAA' in content and 'BBB' in content
    # 事件关联字段(前端终端块按 roundNum + toolCallId 归位)
    for ev in events:
        if ev.get('type') == 'tool_progress':
            assert ev['roundNum'] == 1 and ev['toolCallId'] == 'tc-1'
            break


@pytest.mark.unit
def test_remote_run_meta_terminal_shape(tmp_path, monkeypatch):
    """终态 meta 按终端块契约:command/output/exitCode + remoteRoot."""
    import lib.tasks_pkg.handlers.project as hp

    metas = []
    monkeypatch.setattr(hp, '_finalize_tool_round',
                        lambda task, rn, re_, m: metas.extend(m))
    import lib.desktop

    def fake_send(cmd_type, params=None, timeout=30, target_agent_id=None,
                  user_id='', cmd_id=None):
        return {'stdout': 'hello\n', 'stderr': '', 'exit_code': 0,
                'timed_out': False, 'killed_tree': False, 'truncated': False}, None

    monkeypatch.setattr(lib.desktop, 'send_desktop_command', fake_send)
    task = {'id': 't-meta', 'convId': 'c', '_userId': ''}
    hp._execute_remote_project_tool(
        task, 'run_command', 'tc-9', {'command': 'echo hello'}, 2,
        {'toolCallId': 'tc-9', 'toolName': 'run_command'},
        {'agent_id': 'agent-A', 'root': 'myapp'})
    meta = metas[0]
    assert meta['toolName'] == 'run_command'
    assert meta['command'] == 'echo hello'
    assert 'hello' in meta['output']
    assert meta['exitCode'] == 0
    assert meta['remoteRoot'] == 'myapp'


# ═══════════════════════════════════════════════════════════
#  streams 调试端点
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
def test_streams_endpoint_returns_assembled(flask_client):
    with db.command_queue_lock:
        db.command_queue['c9'] = {
            'id': 'c9', 'type': 'project_run_command', 'params': {},
            'created_at': time.time(), 'event': threading.Event(),
            'result': None, 'error': None, 'user_id': '1',
            'target_agent_id': 'agent-A', 'claimed_agent_id': 'agent-A',
        }
    db.resolve_streams(
        [
            {'cmd_id': 'c9', 'seq': 1, 'stream': 'stdout',
             'data': 'A', 'done': False},
            {'cmd_id': 'c9', 'seq': 2, 'stream': 'stdout',
             'data': 'B', 'done': True},
        ],
        agent_id='agent-A', user_id='1',
    )
    from lib.api_keys import create_key
    _row, token = create_key(owner_user_id=1, name='stream-reader', scopes=['chat'])
    r = flask_client.get('/api/v1/desktop/streams/c9',
                         headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['stdout'] == 'AB' and body['done'] is True


@pytest.mark.unit
def test_remote_feature_switch_does_not_disable_bridge_authority(monkeypatch):
    """Disabling remote worktrees never disables device routing isolation."""
    monkeypatch.delenv('TOFU_REMOTE_WORKTREE', raising=False)
    from lib.conv_config import resolve_conv_config
    out = resolve_conv_config(
        conv_settings={'projectPath': 'remote:a:r'}, is_active=False)
    assert out['projectPath'] == 'remote:a:r'
    assert out.get('project_remote') is None
    db.register_agent('agent-A', {'name': 'mac'}, user_id='owner')
    db.register_agent('agent-B', {'name': 'win'}, user_id='owner')
    cmd = {'id': 'drill-1', 'type': 'desktop_list_files', 'params': {},
           'created_at': time.time(), 'event': threading.Event(),
           'result': None, 'error': None, 'target_agent_id': 'agent-A',
           'user_id': 'owner'}
    with db.command_queue_lock:
        db.command_queue['drill-1'] = cmd
    cmds = _run_async(db.take_pending_commands_async(
        timeout=0.3, agent_id='agent-B', user_id='owner'))
    assert cmds == []


@pytest.mark.api
def test_streams_endpoint_unknown_404(flask_client):
    from lib.api_keys import create_key
    _row, token = create_key(owner_user_id=1, name='stream-reader-2', scopes=['chat'])
    r = flask_client.get('/api/v1/desktop/streams/nobody',
                         headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 404
