"""tests/test_desktop_exec_streaming.py — RWA P2:run_command 平价.

docs/REMOTE_WORKTREE_DESIGN.md §3.4 + §5 P2:
  * **流式分片**:agent 执行不再阻塞 poll 循环,stdout/stderr 分片
    (``{cmd_id, seq, stream, data, done}``)随每次 poll 批量上行;
    服务器 ``resolve_streams`` 按 seq 去重拼帧(断线重发不双计);
  * **进程树 kill**:超时先杀子进程树(psutil.children(recursive=True)),
    不留孤儿;
  * **``rm -rf ~`` 类拦下**:删除命令的绝对/~/env 目标必须 realpath
    落在 share root 内(agent 侧锁根,不靠服务器 is_restricted);
  * **长超时不误杀**:timeout 参数透传,默认 300s。

Run:  pytest tests/test_desktop_exec_streaming.py -m unit -v
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

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
        见 tests/test_run_command_rm_rf_scoped.py)。该 regex 已全库移除,
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
                if polls['n'] >= 25:
                    stop.set()
                return {'commands': []}

        def fake_post(url, json=None, headers=None, timeout=None, proxies=None):
            bodies.append(json)
            return _Resp()

        monkeypatch.setattr(ar.requests, 'post', fake_post)
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
    yield
    with db.command_queue_lock:
        db._streams.clear()


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
        db.resolve_streams(self._frames())
        stream = db.get_command_stream('c1')
        assert stream['stdout'] == 'AB'
        assert stream['stderr'] == 'E'
        assert stream['done'] is True

    def test_resend_dedupes_by_seq(self, clean_streams):
        # 断线重发同一批帧(agent outbox 前缀重传)不许双计
        db.resolve_streams(self._frames())
        db.resolve_streams(self._frames())
        assert db.get_command_stream('c1')['stdout'] == 'AB'

    def test_done_persists_across_batches(self, clean_streams):
        db.resolve_streams(self._frames()[:2])
        assert db.get_command_stream('c1')['done'] is False
        db.resolve_streams(self._frames()[2:])
        assert db.get_command_stream('c1')['done'] is True

    def test_ttl_sweep_drops_stale(self, clean_streams, monkeypatch):
        db.resolve_streams(self._frames())
        with db.command_queue_lock:
            db._streams['c1']['updated_at'] = time.time() - 7200
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
        loopback agent token like TestPollRouteV2 does."""
        from routes.api_v1.auth import loopback_agent_token
        return {'X-Bridge-Secret': loopback_agent_token()}

    def test_poll_body_streams_reach_bridge(self, flask_client, clean_streams):
        r = flask_client.post('/api/desktop/poll', json={
            'results': [],
            'streams': [{'cmd_id': 'c9', 'seq': 1, 'stream': 'stdout',
                         'data': 'hello', 'done': False}],
        }, headers=self._auth())
        assert r.status_code == 200
        assert db.get_command_stream('c9')['stdout'] == 'hello'

    def test_poll_without_streams_still_ok(self, flask_client, clean_streams):
        r = flask_client.post('/api/desktop/poll', json={'results': []},
                              headers=self._auth())
        assert r.status_code == 200
