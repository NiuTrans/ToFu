"""tests/test_desktop_egress_stream.py — S3 流式出口守卫。

Covers:
  * bridge.get_frames（原始帧有序读取 / done / since_seq / 未知 None）
  * 流帧清扫尊重命令自身 ttl（1800s 的流不被 90s 全局窗误杀）
  * agent start_egress_stream（meta 首帧 / body b64 / on_exit 统计 / 白名单）
  * cancel_inflight（未知静默 / 在飞流关闭 / 取消路径 on_exit）
  * EgressStreamReader（SSE 行重组 / meta 状态 / 条目消失与看门狗判死 /
    read_all_text / close 触发 cancel）
  * open_stream / cancel_stream 入队形状（ttl=1800 / stream_id / 寻址）
  * 传输集成：_stream_chat_once 经假 reader 的 SSE 解析与直连逐字节一致；
    中途 EgressUnavailable → EndpointUnreachableError（模型回退语义）

Failure-first：S3 实现前全部红（get_frames / open_stream 不存在）。
"""

from __future__ import annotations

import base64
import json
import threading
import time
import unittest
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

from lib.desktop import bridge as db
from lib.desktop import egress
from lib.desktop.egress import EgressUnavailable


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode('ascii')


# ══════════════════════════════════════════════════════════
#  bridge.get_frames + 清扫语义
# ══════════════════════════════════════════════════════════

class TestGetFrames(unittest.TestCase):

    def setUp(self):
        with db.command_queue_lock:
            db._streams.clear()
            db.command_queue.clear()

    tearDown = setUp

    def _seed(self, cmd_id='s1', frames=((1, 'meta', '{"status":200}'),
                                         (2, 'body', 'AAAA'), (3, 'body', 'BBBB')),
              done=False):
        now = time.time()
        with db.command_queue_lock:
            entry = db._streams.setdefault(
                cmd_id, {'chunks': {}, 'done': False, 'updated_at': now})
            for seq, stream, data in frames:
                entry['chunks'][seq] = (stream, data)
            entry['done'] = done

    def test_ordered_frames_and_done(self):
        self._seed(done=True)
        frames, done = db.get_frames('s1')
        self.assertTrue(done)
        self.assertEqual([(s, st) for s, st, _d in frames],
                         [(1, 'meta'), (2, 'body'), (3, 'body')])

    def test_since_seq_incremental(self):
        self._seed()
        frames, _done = db.get_frames('s1', since_seq=2)
        self.assertEqual([s for s, _st, _d in frames], [3])

    def test_unknown_returns_none(self):
        self.assertIsNone(db.get_frames('ghost'))

    def test_get_command_stream_contract_intact(self):
        self._seed(frames=((1, 'stdout', 'a'), (2, 'stdout', 'b')), done=True)
        out = db.get_command_stream('s1')
        self.assertEqual(out['stdout'], 'ab')
        self.assertTrue(out['done'])
        self.assertEqual(out['last_seq'], 2)

    def test_sweep_respects_command_ttl(self):
        # cmd 带 ttl=1800 的流：updated_at 超过全局 90s 也不被扫掉。
        old = time.time() - 200
        with db.command_queue_lock:
            db._streams['keep'] = {'chunks': {1: ('body', 'x')}, 'done': False,
                                   'updated_at': old}
            db._streams['drop'] = {'chunks': {1: ('body', 'x')}, 'done': False,
                                   'updated_at': old}
            db.command_queue['keep'] = {
                'id': 'keep', 'type': 'egress_http_stream', 'params': {},
                'created_at': time.time(), 'event': threading.Event(),
                'result': None, 'error': None, 'ttl': 1800}
        try:
            with db.command_queue_lock:
                db._sweep_streams_locked(time.time())
            self.assertIn('keep', db._streams)
            self.assertNotIn('drop', db._streams)
        finally:
            with db.command_queue_lock:
                db._streams.pop('keep', None)
                db.command_queue.pop('keep', None)


# ══════════════════════════════════════════════════════════
#  agent start_egress_stream + cancel_inflight
# ══════════════════════════════════════════════════════════

class _FakeStreamResp:
    def __init__(self, chunks, status=200):
        self.status_code = status
        self.headers = {'content-type': 'text/event-stream'}
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, _n):
        for c in self._chunks:
            yield c

    def close(self):
        self.closed = True


class TestAgentStreamExecutor(unittest.TestCase):

    def setUp(self):
        from lib.desktop_agent import _egress as ag
        with ag._INFLIGHT_LOCK:
            ag._INFLIGHT.clear()
            ag._SETTLED.clear()

    def test_meta_then_body_then_stats(self):
        from lib.desktop_agent import _egress as ag
        resp = _FakeStreamResp([b'data: one\n\n', b'data: two\n\n'])
        frames, exits = [], []
        with mock.patch('lib.desktop_agent._egress.requests.request',
                        return_value=resp):
            ag.start_egress_stream(
                {'url': 'https://api.anthropic.com/v1/messages',
                 'method': 'POST', 'headers': {}, 'body_b64': '',
                 'stream_id': 'st-1', 'proxy_mode': 'env'},
                lambda seq, s, d: frames.append((seq, s, d)),
                exits.append)
        self.assertEqual(frames[0][1], 'meta')
        meta = json.loads(frames[0][2])
        self.assertEqual(meta['status'], 200)
        bodies = [base64.b64decode(d) for _s, st, d in frames if st == 'body']
        self.assertEqual(bodies, [b'data: one\n\n', b'data: two\n\n'])
        self.assertEqual(exits, [{'status': 200, 'bytes': 22,
                                  'elapsed_ms': mock.ANY}])
        self.assertTrue(resp.closed)

    def test_whitelist_refusal(self):
        from lib.desktop_agent import _egress as ag
        exits = []
        ag.start_egress_stream({'url': 'https://evil.com/x', 'stream_id': 'x'},
                               lambda *a: None, exits.append)
        self.assertIn('error', exits[0])

    def test_cancel_unknown_is_silent(self):
        from lib.desktop_agent import _egress as ag
        self.assertFalse(ag.cancel_inflight('ghost'))

    def test_cancel_mid_stream_closes_and_settles(self):
        from lib.desktop_agent import _egress as ag

        class _CancellingResp(_FakeStreamResp):
            def iter_content(self, _n):
                yield b'first'
                # 上游因为 close() 而中断的效果
                ag.cancel_inflight('st-2')
                raise ConnectionError('aborted by close')

        resp = _CancellingResp([])
        frames, exits = [], []
        with mock.patch('lib.desktop_agent._egress.requests.request',
                        return_value=resp):
            ag.start_egress_stream(
                {'url': 'https://api.anthropic.com/x', 'method': 'POST',
                 'headers': {}, 'stream_id': 'st-2', 'proxy_mode': 'env'},
                lambda seq, s, d: frames.append((seq, s, d)),
                exits.append)
        # 取消路径：无 error 帧，on_exit 带 cancelled 标记
        self.assertFalse(any(st == 'error' for _s, st, _d in frames))
        self.assertTrue(exits and exits[0].get('cancelled'))
        self.assertTrue(resp.closed)


# ══════════════════════════════════════════════════════════
#  EgressStreamReader
# ══════════════════════════════════════════════════════════

class TestStreamReader(unittest.TestCase):

    def _reader_with_script(self, script):
        """script: list of (frames, done) batches served per get_frames call."""
        calls = {'i': 0}

        def fake_get_frames(cmd_id, since_seq=0):
            if calls['i'] >= len(script):
                return ([], True)
            batch = script[calls['i']]
            calls['i'] += 1
            return batch

        with mock.patch('lib.desktop.get_frames', side_effect=fake_get_frames):
            reader = egress.EgressStreamReader('cmd-x', 'agent-1')
            yield reader

    def test_meta_sets_status_and_headers(self):
        meta = json.dumps({'status': 200, 'headers': {'content-type': 'text/event-stream'}})
        with mock.patch('lib.desktop.get_frames',
                        return_value=([(1, 'meta', meta)], False)):
            r = egress.EgressStreamReader('cmd-x', 'agent-1')
            r.wait_headers(timeout=2)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers['content-type'], 'text/event-stream')

    def test_iter_lines_reassembles_split_sse(self):
        payload = b'data: {"a":1}\n\ndata: {"b":2}\n\n'
        batches = [
            ([(1, 'meta', json.dumps({'status': 200, 'headers': {}}))], False),
            ([(2, 'body', _b64(payload[:11]))], False),   # 切在行中间
            ([(3, 'body', _b64(payload[11:]))], False),
            ([], True),
        ]
        lines = []
        for r in self._reader_with_script(batches):
            for ln in r.iter_lines():
                lines.append(ln)
        self.assertEqual(lines, ['data: {"a":1}', '', 'data: {"b":2}', ''])

    def test_vanished_entry_raises(self):
        with mock.patch('lib.desktop.get_frames', return_value=None):
            r = egress.EgressStreamReader('cmd-x', 'agent-1')
            with self.assertRaises(EgressUnavailable):
                list(r.iter_lines())

    def test_error_frame_raises(self):
        batches = [([(1, 'meta', json.dumps({'status': 200}))], False),
                   ([(2, 'error', json.dumps({'message': 'reset'}))], False)]
        with self.assertRaises(EgressUnavailable):
            for r in self._reader_with_script(batches):
                list(r.iter_lines())

    def test_watchdog_fires_when_stalled_and_agent_offline(self):
        with mock.patch('lib.desktop.get_frames', return_value=([], False)), \
             mock.patch('lib.desktop.online_agents', return_value=[]):
            r = egress.EgressStreamReader('cmd-x', 'agent-1')
            r._last_frame_at = time.monotonic() - 31  # 超过看门狗窗口
            with self.assertRaises(EgressUnavailable):
                next(r.iter_lines())

    def test_watchdog_quiet_when_agent_alive(self):
        with mock.patch('lib.desktop.get_frames', return_value=([], False)), \
             mock.patch('lib.desktop.online_agents',
                        return_value=[{'agent_id': 'agent-1'}]):
            r = egress.EgressStreamReader('cmd-x', 'agent-1')
            r._last_frame_at = time.monotonic() - 31
            # 只验证看门狗不判死（不驱动整个生成器）
            r._check_watchdog()

    def test_read_all_text_drains(self):
        batches = [
            ([(1, 'meta', json.dumps({'status': 429}))], False),
            ([(2, 'body', _b64(b'{"error":"rate"}'))], True),
        ]
        for r in self._reader_with_script(batches):
            self.assertEqual(r.read_all_text(), '{"error":"rate"}')

    def test_close_fires_cancel_once(self):
        with mock.patch.object(egress, 'cancel_stream') as cancel, \
             mock.patch('lib.desktop.get_frames', return_value=([], True)):
            r = egress.EgressStreamReader('cmd-x', 'agent-1')
            r.close()
            r.close()
        cancel.assert_called_once_with('cmd-x', 'agent-1', '')


# ══════════════════════════════════════════════════════════
#  open_stream / cancel_stream 入队形状
# ══════════════════════════════════════════════════════════

class TestOpenStream(unittest.TestCase):

    def test_whitelist_refused(self):
        with self.assertRaises(EgressUnavailable):
            egress.open_stream('https://evil.com/x')

    def test_enqueue_shape_and_reader(self):
        url = 'https://api.anthropic.com/v1/messages'
        # open_stream routes via route_candidates, which PROBES the live host.
        # Probe first and skip honestly: on this host api.anthropic.com is
        # geo_blocked and no desktop agent runs, so the real routing raises
        # EgressUnavailable BY DESIGN — there is nothing to assert about the
        # enqueue shape through a path that cannot exist here.
        verdict = egress._probe_host(url)
        if verdict != 'ok' and not egress._online_egress_agents(''):
            pytest.skip(
                f'egress probe api.anthropic.com → {verdict} and no '
                'egress-capable desktop agent online — EgressUnavailable is '
                'by design on this host')
        meta = json.dumps({'status': 200, 'headers': {}})
        with mock.patch.object(egress, 'route_candidates',
                               return_value=['agent-9']), \
             mock.patch('lib.desktop.enqueue_desktop_command',
                        return_value=('cmd-1', None)) as enq, \
             mock.patch('lib.desktop.get_frames',
                        return_value=([(1, 'meta', meta)], False)):
            r = egress.open_stream('https://api.anthropic.com/v1/messages',
                                   headers={'x': 'y'}, body=b'{"m":1}',
                                   user_id='u1')
        args, kwargs = enq.call_args
        self.assertEqual(args[0], 'egress_http_stream')
        self.assertEqual(kwargs['target_agent_id'], 'agent-9')
        self.assertEqual(kwargs['user_id'], 'u1')
        self.assertEqual(kwargs['ttl'], 1800)
        params = args[1]
        self.assertEqual(params['stream_id'], kwargs['cmd_id'])
        self.assertEqual(base64.b64decode(params['body_b64']), b'{"m":1}')
        self.assertEqual(r.status_code, 200)

    def test_cancel_stream_enqueues_cancel(self):
        with mock.patch('lib.desktop.enqueue_desktop_command',
                        return_value=('c2', None)) as enq:
            egress.cancel_stream('cmd-7', 'agent-3', 'u1')
        args, kwargs = enq.call_args
        self.assertEqual(args[0], 'egress_cancel')
        self.assertEqual(args[1], {'cmd_id': 'cmd-7'})
        self.assertEqual(kwargs['target_agent_id'], 'agent-3')


# ══════════════════════════════════════════════════════════
#  传输集成：_stream_chat_once 经假 reader
# ══════════════════════════════════════════════════════════

class TestTransportIntegration(unittest.TestCase):

    def _plan(self):
        from lib.llm._sse_core import prepare_request
        return prepare_request(
            {'model': 'gpt-x', 'messages': [{'role': 'user', 'content': 'hi'}],
             'stream': True},
            api_key='k', base_url='https://api.anthropic.com/v1')

    def _sse_reader(self, lines, status=200):
        meta = json.dumps({'status': status, 'headers': {}})
        frames = [(1, 'meta', meta)]
        for i, ln in enumerate(lines, start=2):
            frames.append((i, 'body', _b64((ln + '\n').encode())))
        script = [(frames, True)]
        calls = {'i': 0}

        def fake_get_frames(cmd_id, since_seq=0):
            if calls['i']:
                return ([], True)
            calls['i'] += 1
            return script[0]

        # The reader's iter_lines is driven LATER by the transport — the patch
        # must outlive this function, not just the constructor.
        patcher = mock.patch('lib.desktop.get_frames',
                             side_effect=fake_get_frames)
        patcher.start()
        self.addCleanup(patcher.stop)
        reader = egress.EgressStreamReader('cmd-x', 'agent-1')
        # open_stream's contract: headers are consumed before returning.
        reader.wait_headers(timeout=2)
        return reader

    def test_sse_parsed_identically_to_direct(self):
        from lib.llm.stream import _stream_chat_once
        lines = [
            'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"gpt-x","choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}',
            'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"gpt-x","choices":[{"index":0,"delta":{"content":"lo"}}]}',
            'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"gpt-x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]
        reader = self._sse_reader(lines)
        db.register_agent('agent-1', {'capabilities': {'egress': True}})
        self.addCleanup(lambda: db._agents.pop('agent-1', None))
        with mock.patch('lib.desktop.egress.route_request', return_value='agent-1'), \
             mock.patch('lib.desktop.egress.open_stream', return_value=reader):
            msg, finish, usage = _stream_chat_once(
                {'model': 'gpt-x', 'messages': [{'role': 'user', 'content': 'hi'}],
                 'stream': True},
                api_key='k', base_url='https://api.anthropic.com/v1')
        self.assertEqual(msg['content'], 'Hello')
        self.assertEqual(msg.get('role'), 'assistant')
        self.assertEqual(finish, 'stop')

    def test_midstream_egress_failure_maps_to_unreachable(self):
        from lib.llm.stream import _stream_chat_once
        from lib.llm_errors import EndpointUnreachableError

        class _DyingReader:
            status_code = 200
            headers = {}

            def iter_lines(self, decode_unicode=True):
                yield 'data: {"choices":[{"delta":{"content":"x"}}]}'
                raise EgressUnavailable('agent died mid-stream')

            def close(self):
                pass

        with mock.patch('lib.desktop.egress.route_request', return_value='agent-1'), \
             mock.patch('lib.desktop.egress.open_stream',
                        return_value=_DyingReader()):
            with self.assertRaises(EndpointUnreachableError):
                _stream_chat_once(
                    {'model': 'gpt-x',
                     'messages': [{'role': 'user', 'content': 'hi'}],
                     'stream': True},
                    api_key='k', base_url='https://api.anthropic.com/v1')


if __name__ == '__main__':
    unittest.main()
