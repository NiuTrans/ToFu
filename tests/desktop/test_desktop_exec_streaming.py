"""tests/desktop/test_desktop_exec_streaming.py — RWA P2:run_command 平价.

docs/modules/remote_execution.md:
  * **流式分片**:agent 执行不再阻塞 poll 循环,stdout/stderr 分片
    (``{cmd_id, seq, stream, data, done}``)随每次 poll 批量上行;
    服务器 ``resolve_streams`` 按 seq 去重拼帧(断线重发不双计);
  * **进程树 kill**:超时先杀子进程树(psutil.children(recursive=True)),
    不留孤儿;
  * **``rm -rf ~`` 类拦下**:删除命令的绝对/~/env 目标必须 realpath
    落在 share root 内(agent 侧锁根,不靠服务器 is_restricted);
  * **长超时不误杀**:默认没有执行时限;显式 timeout 原值透传。

Run:  pytest tests/desktop/test_desktop_exec_streaming.py -m unit -v
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

# serial: process-tree kill/reap under wall-clock timeouts —
# oversubscribing the parallel unit lane starves the process group
# into flaky reds (TESTING_STRATEGY serial policy).
pytestmark = [pytest.mark.unit, pytest.mark.serial]

import lib.desktop_agent._project as pj
from lib.desktop_agent import _exec as ex
from lib.desktop import bridge as db


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    root = tmp_path / 'app'
    (root / 'sub').mkdir(parents=True)
    (root / 'sub' / 'keep.txt').write_text('keep', encoding='utf-8')
    cfg = tmp_path / 'cfg.json'
    cfg.write_text(json.dumps(
        {'share_roots': [{'name': 'app', 'path': str(root)}]}), encoding='utf-8')
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(cfg))
    pj._freshness.clear()
    yield {'root': root, 'tmp': tmp_path}
    pj._freshness.clear()


def _run_streamed(command, cwd, timeout=10):
    """Drive start_streamed_command to completion; return (chunks, outcome)."""
    chunks = []
    done = threading.Event()
    box = {}

    def on_chunk(stream, data):
        chunks.append((stream, data))

    def on_exit(outcome):
        box['outcome'] = outcome
        done.set()

    ex.start_streamed_command(command, cwd, timeout, on_chunk, on_exit)
    assert done.wait(timeout=15), 'streamed command never exited'
    return chunks, box['outcome']


# ═══════════════════════════════════════════════════════════
#  StreamedProcess:分片 / 最终结果 / 长超时
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestStreamedProcess:
    def test_chunks_and_final_result(self, proj):
        chunks, outcome = _run_streamed(
            "printf 'l1\\nl2\\n' 1>&2; printf 'o1\\n'",
            str(proj['root']))
        out_text = ''.join(d for s, d in chunks if s == 'stdout')
        err_text = ''.join(d for s, d in chunks if s == 'stderr')
        assert 'o1' in out_text and 'l1' in err_text
        assert outcome['exit_code'] == 0
        assert outcome['timed_out'] is False
        assert 'o1' in outcome['stdout'] and 'l1' in outcome['stderr']

    def test_start_is_nonblocking(self, proj):
        done = threading.Event()
        t0 = time.monotonic()
        ex.start_streamed_command('sleep 0.5', str(proj['root']), 10,
                                  lambda s, d: None, lambda o: done.set())
        assert time.monotonic() - t0 < 0.3, 'start_streamed_command blocked'
        assert done.wait(timeout=10)

    def test_long_timeout_not_prematurely_killed(self, proj):
        _, outcome = _run_streamed('sleep 2; echo survived', str(proj['root']),
                                   timeout=10)
        assert outcome['exit_code'] == 0
        assert outcome['timed_out'] is False
        assert 'survived' in outcome['stdout']

    def test_timeout_kills_process_tree(self, proj):
        psutil = pytest.importorskip('psutil')
        child_pids = []
        # 父进程派生一个 sleep 300 子进程,然后自己长睡;超时必须连子带孙全灭
        cmd = ("python3 -c \"import subprocess,time,sys;"
               "p=subprocess.Popen(['sleep','300']);print(p.pid,flush=True);"
               "time.sleep(300)\"")
        done = threading.Event()
        box = {}

        def on_chunk(stream, data):
            if stream == 'stdout' and data.strip().isdigit():
                child_pids.append(int(data.strip()))

        ex.start_streamed_command(cmd, str(proj['root']), 1,
                                  on_chunk, lambda o: (box.update(o=o), done.set()))
        assert done.wait(timeout=15)
        outcome = box['o']
        assert outcome['timed_out'] is True
        assert outcome['killed_tree'] is True
        # 轮询等待子进程消亡(高负载机上固定 sleep 是竞态)
        deadline = time.time() + 5
        for pid in child_pids:
            while psutil.pid_exists(pid) and time.time() < deadline:
                time.sleep(0.05)
            assert not psutil.pid_exists(pid), f'orphan child survived: {pid}'

    def test_output_capped_and_truncated(self, proj):
        _, outcome = _run_streamed(
            "python3 -c \"print('x' * 200000)\"", str(proj['root']), 15)
        assert len(outcome['stdout']) <= 100_500
        assert outcome['truncated'] is True


# ═══════════════════════════════════════════════════════════
#  删除目标锁根(rm -rf ~ 类)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDeleteTargetContainment:
    def test_rm_home_refused(self, proj):
        out = pj.cmd_project_run_command({'root': 'app', 'command': 'rm -rf ~'})
        assert 'error' in out and 'blocked' in out['error']

    def test_rm_absolute_outside_root_refused(self, proj):
        target = proj['tmp'] / 'elsewhere'
        target.mkdir()
        out = pj.cmd_project_run_command(
            {'root': 'app', 'command': f'rm -rf {target}'})
        assert 'error' in out and 'blocked' in out['error']
        assert target.exists()  # 拒于校验期,未被执行

    def test_sudo_delete_outside_root_refused(self, proj):
        """sudo 前缀不得绕开锁根守卫:sudo rm -rf <root外> 同拒。"""
        target = proj['tmp'] / 'elsewhere_sudo'
        target.mkdir()
        out = pj.cmd_project_run_command(
            {'root': 'app', 'command': f'sudo rm -rf {target}'})
        assert 'error' in out and 'blocked' in out['error']
        assert target.exists()

    def test_rm_relative_inside_root_allowed(self, proj):
        out = pj.cmd_project_run_command(
            {'root': 'app', 'command': 'rm -rf sub'})
        assert out.get('exit_code') == 0
        assert not (proj['root'] / 'sub').exists()

    def test_rm_rf_absolute_inside_root_allowed_parity(self, proj):
        """服务器平价(2026-07-28 起):scoped 绝对路径删除放行.

        旧 DANGEROUS_PATTERNS[0]=\\brm\\s+-rf\\s+/ 连根内绝对路径也拒 —— 与
        服务器同款误伤(``rm -rf /tmp/wt_fill`` 这类临时 worktree 清理被恒拒,
        见 tests/tasks_pkg/test_run_command_rm_rf_scoped.py)。该 regex 已全库移除,
        删除命令统一由参数解析守卫裁决:深度 <2 拒(catastrophic)、
        越 share root 拒(上方锁根守卫)、根内 scoped 删除放行 —— 与
        test_rm_relative_inside_root_allowed 的相对路径形态对齐。"""
        out = pj.cmd_project_run_command(
            {'root': 'app', 'command': f'rm -rf {proj["root"] / "sub"}'})
        assert out.get('exit_code') == 0
        assert not (proj['root'] / 'sub').exists()

    def test_neuter_containment_guard_lets_escape_through(self, proj, monkeypatch):
        """剥掉锁根守卫 → 绝对路径删除越界放过 = 守卫承重(用临时目录,不碰真路径)."""
        monkeypatch.setattr(pj, '_check_delete_targets_within',
                            lambda _cmd, _root: None)
        target = proj['tmp'] / 'would_die'
        target.mkdir()
        out = pj.cmd_project_run_command(
            {'root': 'app', 'command': f'rm -r {target}'})
        assert out.get('exit_code') == 0  # 坏结果:越界删除被放过
        assert not target.exists()


# ═══════════════════════════════════════════════════════════
#  agent 循环:流帧上行 + 非阻塞
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAgentLoopStreaming:
    def test_stream_frames_and_result_flow_through_polls(self, proj, monkeypatch):
        import lib.desktop_agent._run as ar

        bodies = []
        polls = {'n': 0}
        stop = threading.Event()

        class _Resp:
            status_code = 200

            def json(self_non):
                polls['n'] += 1
                if polls['n'] == 1:
                    return {'commands': [{
                        'id': 'cmd-stream-1',
                        'type': 'project_run_command',
                        'params': {'root': 'app',
                                   'command': "printf 'a\\n'; sleep 0.6; printf 'b\\n'"},
                    }]}
                if polls['n'] >= 200:
                    stop.set()
                return {'commands': []}

        def fake_post(url, json=None, headers=None, timeout=None, proxies=None,
                      allow_redirects=None, auth=None):
            assert allow_redirects is False and callable(auth)
            bodies.append(json)
            if json.get('results'):
                stop.set()
            return _Resp()

        monkeypatch.setattr(ar.requests.Session, 'post', lambda self, *a, **kw: fake_post(*a, **kw))
        ar.run_agent('http://server.example',
                     {'allow_write': True, 'allow_exec': True},
                     poll_interval=0.05, stop_event=stop)

        stream_frames = [f for b in bodies for f in b.get('streams', [])]
        results = [r for b in bodies for r in b.get('results', [])]
        # ① 流帧按 cmd 归组、seq 单调、含 done 帧
        frames = [f for f in stream_frames if f['cmd_id'] == 'cmd-stream-1']
        assert frames, 'no stream frames were uploaded'
        # 两条读者线程并发上帧,上送顺序可交错;契约是 seq 稠密唯一 + done 居尾
        seqs = [f['seq'] for f in frames]
        assert len(seqs) == len(set(seqs))
        assert min(seqs) == 1 and max(seqs) == len(seqs)
        by_seq = {f['seq']: f for f in frames}
        assert by_seq[max(seqs)]['done'] is True
        text = ''.join(f['data'] for f in frames if f['stream'] == 'stdout')
        assert 'a' in text and 'b' in text
        # ② 最终结果经 results 通道到达
        final = [r for r in results if r['id'] == 'cmd-stream-1']
        assert final and final[0]['result']['exit_code'] == 0
        # ③ 非阻塞:命令在飞期间已有不含其结果的 poll body(循环没卡住)
        early = [b for b in bodies[1:] if b.get('streams')
                 and not any(r['id'] == 'cmd-stream-1' for r in b.get('results', []))]
        assert early, 'poll loop blocked until the command finished'


# ═══════════════════════════════════════════════════════════
#  服务器桥:resolve_streams 拼帧 / 去重 / TTL
# ═══════════════════════════════════════════════════════════

@pytest.fixture()
def clean_streams():
    with db.command_queue_lock:
        db._streams.clear()
        db.command_queue['c1'] = {
            'id': 'c1', 'type': 'project_run_command', 'params': {},
            'created_at': time.time(), 'event': threading.Event(),
            'result': None, 'error': None, 'user_id': 'owner',
            'target_agent_id': 'agent-A', 'claimed_agent_id': 'agent-A',
        }
    yield
    with db.command_queue_lock:
        db._streams.clear()
        db.command_queue.pop('c1', None)
        db.command_queue.pop('c9', None)


@pytest.mark.unit
class TestBridgeStreams:
    def _frames(self, cmd='c1'):
        return [
            {'cmd_id': cmd, 'seq': 2, 'stream': 'stdout', 'data': 'B', 'done': False},
            {'cmd_id': cmd, 'seq': 1, 'stream': 'stdout', 'data': 'A', 'done': False},
            {'cmd_id': cmd, 'seq': 3, 'stream': 'stderr', 'data': 'E', 'done': False},
            {'cmd_id': cmd, 'seq': 4, 'stream': 'meta', 'data': '', 'done': True},
        ]

    def test_reassembly_orders_by_seq(self, clean_streams):
        db.resolve_streams(
            self._frames(), agent_id='agent-A', user_id='owner')
        stream = db.get_command_stream('c1')
        assert stream['stdout'] == 'AB'
        assert stream['stderr'] == 'E'
        assert stream['done'] is True

    def test_resend_dedupes_by_seq(self, clean_streams):
        # 断线重发同一批帧(agent outbox 前缀重传)不许双计
        db.resolve_streams(
            self._frames(), agent_id='agent-A', user_id='owner')
        db.resolve_streams(
            self._frames(), agent_id='agent-A', user_id='owner')
        assert db.get_command_stream('c1')['stdout'] == 'AB'

    def test_done_persists_across_batches(self, clean_streams):
        db.resolve_streams(
            self._frames()[:2], agent_id='agent-A', user_id='owner')
        assert db.get_command_stream('c1')['done'] is False
        db.resolve_streams(
            self._frames()[2:], agent_id='agent-A', user_id='owner')
        assert db.get_command_stream('c1')['done'] is True

    def test_ttl_sweep_retains_unsettled_then_drops_completed(self, clean_streams, monkeypatch):
        db.resolve_streams(
            self._frames(), agent_id='agent-A', user_id='owner')
        with db.command_queue_lock:
            db._streams['c1']['updated_at'] = time.time() - 7200
        assert db.get_command_stream('c1') is not None
        with db.command_queue_lock:
            db.command_queue['c1']['event'].set()
        assert db.get_command_stream('c1') is None

    def test_unknown_command_stream_is_none(self, clean_streams):
        assert db.get_command_stream('nobody') is None


@pytest.mark.api
class TestPollRouteStreams:
    @pytest.fixture(autouse=True)
    def _fast_long_poll(self, monkeypatch):
        monkeypatch.setattr(db, 'POLL_WAIT_TIMEOUT', 0.2)

    @staticmethod
    def _auth():
        """Bridge endpoints are credential-gated (B0); use the in-process
        process capability like TestPollRouteV2 does."""
        from lib.bridge_auth import process_agent_token
        return {'X-Bridge-Secret': process_agent_token()}

    def test_poll_body_streams_reach_bridge(self, flask_client, clean_streams):
        with db.command_queue_lock:
            db.command_queue['c9'] = {
                'id': 'c9', 'type': 'project_run_command', 'params': {},
                'created_at': time.time(), 'event': threading.Event(),
                'result': None, 'error': None, 'user_id': '1',
                'target_agent_id': 'stream-agent',
                'claimed_agent_id': 'stream-agent',
            }
        r = flask_client.post('/api/desktop/poll', json={
            'results': [],
            'agent': {'agent_id': 'stream-agent'},
            'streams': [{'cmd_id': 'c9', 'seq': 1, 'stream': 'stdout',
                         'data': 'hello', 'done': False}],
        }, headers=self._auth())
        assert r.status_code == 200
        assert db.get_command_stream('c9')['stdout'] == 'hello'

    def test_poll_without_streams_still_ok(self, flask_client, clean_streams):
        r = flask_client.post('/api/desktop/poll', json={
            'results': [], 'agent': {'agent_id': 'stream-agent'},
        },
                              headers=self._auth())
        assert r.status_code == 200


@pytest.mark.parametrize('timeout', [None, 300])
def test_streamed_process_cancellation_joins_readers_before_terminal(tmp_path, timeout):
    cancel, entered, done = threading.Event(), threading.Event(), threading.Event()
    result, chunks = [], []
    command = 'python3 -u -c "import time; print(\'ready\'); time.sleep(300)"'
    def on_chunk(stream, data):
        chunks.append((stream, data))
        entered.set()
    ex.start_streamed_command(command, str(tmp_path), timeout, on_chunk,
                              lambda outcome: (result.append(outcome), done.set()), cancel=cancel)
    assert entered.wait(3)
    cancel.set()
    assert done.wait(5)
    assert len(result) == 1 and result[0]['cancelled'] is True
    assert result[0]['timed_out'] is False and result[0]['exit_code'] != 0
    previous = list(chunks)
    time.sleep(.05)
    assert chunks == previous


def test_stream_worker_start_failure_reaps_spawned_process(tmp_path, monkeypatch):
    processes, results = [], []
    original = ex.subprocess.Popen
    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    def fail_start(thread):
        raise RuntimeError('injected worker creation failure')
    with monkeypatch.context() as mutation:
        mutation.setattr(ex.subprocess, 'Popen', spawn)
        mutation.setattr(ex.threading.Thread, 'start', fail_start)
        ex.start_streamed_command('sleep 300', str(tmp_path), 300, lambda *_: None, results.append)
    assert len(processes) == len(results) == 1
    assert processes[0].poll() is not None
    assert 'worker creation failure' in results[0]['error']


@pytest.mark.parametrize('timeout', [None, 5000])
def test_project_execution_keeps_real_process_after_old_deadline(proj, monkeypatch, timeout):
    import shlex
    import sys
    from types import SimpleNamespace
    clock = [100.0]
    monkeypatch.setattr(ex, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    release = proj['root'] / 'release-command'
    code = ("import pathlib,time; print('ready',flush=True); p=pathlib.Path('release-command'); "
            "exec('while not p.exists(): time.sleep(.01)'); print('complete',flush=True)")
    command = shlex.join([sys.executable, '-u', '-c', code])
    entered, done, cancelled = threading.Event(), threading.Event(), threading.Event()
    outcomes = []
    params = {'root': 'app', 'command': command}
    if timeout is not None:
        params['timeout'] = timeout
    try:
        assert pj.start_project_run('long', params,
            lambda stream, data: entered.set() if 'ready' in data else None,
            lambda outcome: (outcomes.append(outcome), done.set()), cancel=cancelled) is None
        assert entered.wait(3)
        clock[0] += 4000
        assert not done.wait(.35), outcomes
        release.touch()
        assert done.wait(5)
        assert len(outcomes) == 1
        assert outcomes[0]['exit_code'] == 0
        assert outcomes[0]['timed_out'] is False and outcomes[0]['cancelled'] is False
        assert 'complete' in outcomes[0]['stdout']
    finally:
        cancelled.set()
        release.touch()
        assert done.wait(5)


@pytest.mark.parametrize('timeout', [-1, float('inf'), float('nan'), True, 'bad'])
def test_invalid_execution_timeout_reports_once_before_spawn(monkeypatch, timeout):
    outcomes = []
    def forbidden(*args, **kwargs):
        pytest.fail('invalid wait policy spawned a command')
    monkeypatch.setattr(ex.subprocess, 'Popen', forbidden)
    ex.start_streamed_command('echo work', None, timeout, lambda *_: None, outcomes.append)
    assert len(outcomes) == 1 and 'timeout' in outcomes[0]['error']
    assert 'timeout' in ex.cmd_run_local({'command': 'echo work', 'timeout': timeout})['error']


@pytest.mark.parametrize('timeout', [None, 0, .01, 5000])
def test_sync_execution_passes_exact_optional_timeout(monkeypatch, timeout):
    from types import SimpleNamespace
    captured = {}
    def run(command, cwd, timeout, on_chunk, on_exit, **kwargs):
        captured['timeout'] = timeout
        on_exit({'stdout': 'done', 'stderr': '', 'exit_code': 0})
    monkeypatch.setattr(ex, 'start_streamed_command', run)
    params = {'command': 'echo done'}
    if timeout is not None:
        params['timeout'] = timeout
    assert ex.cmd_run_local(params)['exit_code'] == 0
    assert captured['timeout'] == timeout


@pytest.mark.parametrize('allow_exec', [False, True])
def test_generic_device_execution_observes_permission_and_owner_stop(tmp_path, allow_exec):
    import shlex
    import sys
    from lib.desktop_agent import _run
    stop, done = threading.Event(), threading.Event()
    class Results(list):
        def append(self, item):
            super().append(item)
            done.set()
    results = Results()
    results.stop = stop
    chunks, lock = [], threading.RLock()
    command = shlex.join([sys.executable, '-u', '-c',
        "import time;print('ready',flush=True);time.sleep(300)"])
    _run._start_project_run_streamed('generic', {'command': command, 'cwd': str(tmp_path)},
        {'allow_exec': allow_exec}, results, chunks, lock, cmd_type='desktop_run_command')
    if not allow_exec:
        assert done.wait(1) and 'allow-exec' in results[0]['error']
        assert chunks == []
        return
    deadline = time.monotonic() + 3
    try:
        while not any('ready' in item['data'] for item in chunks) and time.monotonic() < deadline:
            done.wait(.01)
        assert any('ready' in item['data'] for item in chunks)
        assert not done.is_set()
    finally:
        stop.set()
        assert done.wait(5)
    assert len(results) == 1
    outcome = results[0]['result']
    assert outcome['cancelled'] is True and outcome['timed_out'] is False
    assert outcome['exit_code'] != 0


@pytest.mark.skipif(__import__('sys').platform != 'linux', reason='Linux subreaper/parent-death ownership')
def test_abrupt_owner_death_reaps_escaped_command_tree_and_private_outputs(tmp_path):
    from pathlib import Path
    import json
    import shlex
    import subprocess
    import sys
    import psutil
    child_info = tmp_path / 'child.json'
    command_info = tmp_path / 'command.json'
    owner_info = tmp_path / 'owner.json'
    child_code = ("import json,os,subprocess,sys,time;from pathlib import Path;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']);"
        f"Path({str(child_info)!r}).write_text(json.dumps([os.getpid(),p.pid]));time.sleep(300)")
    command_code = ("import json,os,subprocess,sys,time;from pathlib import Path;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],start_new_session=True);"
        f"q=Path({str(child_info)!r});exec('while not q.exists(): time.sleep(.01)');"
        f"Path({str(command_info)!r}).write_text(json.dumps([os.getpid(),*json.loads(q.read_text())]));"
        "time.sleep(300)")
    owner_code = """
import json,sys,time
from pathlib import Path
from lib.desktop_agent._exec import _StreamedProcess
p=_StreamedProcess(sys.argv[1],sys.argv[2],None,lambda *_:None,lambda *_:None)
p.start()
Path(sys.argv[3]).write_text(json.dumps({'supervisor':p._proc.pid,'directory':p._temporary.name}))
while True:time.sleep(.1)
"""
    owner = subprocess.Popen([sys.executable, '-c', owner_code,
        shlex.join([sys.executable, '-u', '-c', command_code]), str(tmp_path), str(owner_info)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ids = []
    try:
        deadline = time.monotonic() + 10
        while not (owner_info.exists() and command_info.exists()) and time.monotonic() < deadline:
            assert owner.poll() is None
            time.sleep(.02)
        command_ids = json.loads(command_info.read_text())
        info = json.loads(owner_info.read_text())
        ids = [info['supervisor'], *command_ids]
        assert os.getsid(command_ids[1]) != os.getsid(info['supervisor'])
        owned = [psutil.Process(pid) for pid in ids]
        owner.kill();owner.wait(timeout=5)
        gone, alive = psutil.wait_procs(owned, timeout=5)
        # The supervisor may await the host init; every command descendant is reaped.
        assert all(child.pid == info['supervisor'] and child.status() == psutil.STATUS_ZOMBIE
                   for child in alive)
        deadline = time.monotonic() + 3
        while Path(info['directory']).exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert not Path(info['directory']).exists()
    finally:
        if owner.poll() is None:owner.kill()
        owner.wait(timeout=5)
        for pid in ids:
            try:psutil.Process(pid).kill()
            except psutil.NoSuchProcess:pass


def test_worker_refuses_retired_owner_before_command_side_effect(tmp_path):
    import subprocess
    import sys
    from lib.paper.builds.process_worker import main
    directory = tmp_path / 'tofu-desktop-command-owner-guard'
    directory.mkdir()
    ready = directory / 'ready'
    ready.write_bytes(b'1')
    output, errors, status = [directory / name for name in ('stdout', 'stderr', 'status.json')]
    output.touch();errors.touch()
    effect = tmp_path / 'must-not-exist'
    args = ['--desktop-streams', str(errors), str(status), '1', '0', '0', '0',
        str(os.getppid()+1000000), str(ready), str(output), str(tmp_path), sys.executable,
        '-c', f'from pathlib import Path;Path({str(effect)!r}).touch()']
    assert main(args) == 125
    assert not effect.exists() and not directory.exists()


def test_supervised_stdout_stderr_and_negative_exit_remain_separate(tmp_path):
    import shlex
    import sys
    command = shlex.join([sys.executable, '-u', '-c',
        "import os,signal,sys;print('out',flush=True);print('err',file=sys.stderr,flush=True);os.kill(os.getpid(),signal.SIGTERM)"])
    chunks, outcome = _run_streamed(command, str(tmp_path), None)
    assert outcome['stdout'] == 'out\n' and outcome['stderr'] == 'err\n'
    assert outcome['exit_code'] == -15
    assert outcome['timed_out'] is False and outcome['cancelled'] is False


@pytest.mark.parametrize('birth,status,expected', [(1,'running',True),(2,'running',False),(1,'zombie',False)])
def test_windows_pre_admission_owner_identity_checks_birth_and_liveness(monkeypatch, birth, status, expected):
    import psutil
    from types import SimpleNamespace
    from lib.paper.builds import process_worker
    owner = SimpleNamespace(is_running=lambda: True, status=lambda: status, create_time=lambda: birth)
    monkeypatch.setattr(process_worker, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(psutil, 'Process', lambda pid: owner)
    assert process_worker._owner_alive(123, 1) is expected


def test_cleanup_fault_still_delivers_one_terminal_outcome(tmp_path, monkeypatch):
    import tempfile
    original = tempfile.TemporaryDirectory.cleanup
    def cleanup(directory):
        original(directory)
        raise OSError('injected cleanup failure')
    monkeypatch.setattr(tempfile.TemporaryDirectory, 'cleanup', cleanup)
    chunks, outcome = _run_streamed('echo ready', str(tmp_path), None)
    assert outcome['exit_code'] == 0
    assert 'injected cleanup failure' in outcome['error']


@pytest.mark.parametrize('record', ['[]', '{"returncode":true,"error":null,"stopped":false}',
    '{"returncode":0,"error":[],"stopped":false}', '{"returncode":0,"stopped":false}'])
def test_private_completion_record_refuses_malformed_status(tmp_path, record):
    from lib.paper.builds.process_worker import load_desktop_status
    status = tmp_path / 'status.json'
    status.write_text(record)
    with pytest.raises(ValueError):load_desktop_status(status)


def test_stop_before_worker_admission_has_no_command_side_effect(tmp_path):
    import shlex
    import sys
    effect = tmp_path / 'must-not-execute'
    command = shlex.join([sys.executable, '-c',
        f'from pathlib import Path;Path({str(effect)!r}).touch()'])
    chunks, outcome = _run_streamed(command, str(tmp_path), 0)
    assert outcome['timed_out'] is True
    assert not effect.exists()


def test_completion_record_failure_still_retires_and_delivers_error(tmp_path, monkeypatch):
    from lib.paper.builds import process_worker
    def fail(path):raise ValueError('injected invalid record')
    monkeypatch.setattr(process_worker, 'load_desktop_status', fail)
    chunks, outcome = _run_streamed('echo ready', str(tmp_path), None)
    assert outcome['exit_code'] == 0 and 'supervisor' in outcome['error']
