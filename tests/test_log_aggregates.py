"""tests/test_log_aggregates.py — lib/log_aggregates 指纹聚合层测试。

分层(epic pt_71eaaa8d5b8243e9;owner 验收口径 2026-08-06):

  1. 指纹归一化:数字/hex/URL/路径占位化 → 同一模板;不同消息不同指纹。
  2. 异常签名:traceback 按「异常类型 + 栈顶帧」分组——同帧同类不同变量
     → 同指纹;不同帧/不同类型 → 不同指纹(NEUTER:挖掉签名则坍缩,证明
     签名是承重墙)。
  3. 续行归属:多行 traceback 在 replay 里是**一条**记录,且与 live
     handler 路径产出同一指纹(同步/listener/replay 三路径同一实现)。
  4. 落库:批量 upsert 计数累加、fail-open 重排队、TTL 只扫旧行。
  5. 端点:信封形状 + 参数校验。
  6. 真实验收:回放项目 logs/error.log(缺失则响亮 skip),验证 69% 级
     噪音确实坍缩、且没有一条记录以 'Traceback' 开头(续行永不成独立记录)。

Run:  pytest tests/test_log_aggregates.py -m unit
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.log_aggregates as la

pytestmark = pytest.mark.unit


# ═══ helpers ═══

def _raise_key_error():
    raise KeyError(404)


def _exc_info():
    try:
        _raise_key_error()
    except KeyError:
        return sys.exc_info()
    raise AssertionError('unreachable')


def _record(text, *, level=logging.ERROR, name='lib.foo', exc_info=None):
    return logging.LogRecord(name, level, __file__, 10, text, (), exc_info)


# ═══ 1. 指纹归一化 ═══

class TestFingerprintNormalization:
    def test_volatile_parts_collapse_to_one_fingerprint(self):
        variants = [
            '[SyncDrift] STALLED conv=abc123def456 kind=rev client=3 server=9 age=42s',
            '[SyncDrift] STALLED conv=000000ffffff kind=rev client=1 server=2 age=99s',
            '[SyncDrift] STALLED conv=beefbeef00 kind=rev client=7 server=8 age=1s',
        ]
        fps = {la.fingerprint_text('WARNING', 'routes.api_v1.conversations', v)[0]
               for v in variants}
        assert len(fps) == 1, fps

    def test_url_path_digits_hex_are_placeholderized(self):
        _, t1 = la.fingerprint_text(
            'ERROR', 'lib.x',
            'fetch https://a.b/c?d=1 via /mnt/data/x/y failed 3 times id=deadbeef')
        _, t2 = la.fingerprint_text(
            'ERROR', 'lib.x',
            'fetch https://z.z/q via /opt/other/path failed 9 times id=123456')
        assert t1 == t2
        assert 'https://' not in t1 and 'deadbeef' not in t1

    def test_distinct_messages_distinct_fingerprints(self):
        fp1, _ = la.fingerprint_text('ERROR', 'lib.x', 'alpha broke')
        fp2, _ = la.fingerprint_text('ERROR', 'lib.x', 'beta broke')
        assert fp1 != fp2

    def test_level_and_logger_are_part_of_the_fingerprint(self):
        base = la.fingerprint_text('ERROR', 'lib.x', 'same msg')[0]
        assert la.fingerprint_text('WARNING', 'lib.x', 'same msg')[0] != base
        assert la.fingerprint_text('ERROR', 'lib.y', 'same msg')[0] != base


# ═══ 2. 异常签名(traceback 分组)═══

class TestExcSignature:
    def test_same_frame_and_type_groups(self):
        """同一抛出点同一异常类型,变量不同 → 同一指纹(刷屏坍缩)。"""
        t1 = ('fetch failed\nTraceback (most recent call last):\n'
              '  File "/app/lib/fetch.py", line 42, in go\n'
              '    r.raise_for_status()\n'
              'KeyError: 404')
        t2 = ('fetch failed\nTraceback (most recent call last):\n'
              '  File "/app/lib/fetch.py", line 42, in go\n'
              '    r.raise_for_status()\n'
              'KeyError: 500')
        # 消息首行相同、帧相同、异常类相同 → 同指纹
        assert la.fingerprint_text('ERROR', 'lib.x', t1)[0] == \
            la.fingerprint_text('ERROR', 'lib.x', t2)[0]

    def test_different_frame_different_fingerprint(self):
        """同名异常从不同函数抛出 → 不同指纹(栈顶帧是分组键)。"""
        t1 = ('boom\nTraceback (most recent call last):\n'
              '  File "/app/lib/a.py", line 1, in alpha\nKeyError: 1')
        t2 = ('boom\nTraceback (most recent call last):\n'
              '  File "/app/lib/a.py", line 2, in beta\nKeyError: 1')
        assert la.fingerprint_text('ERROR', 'lib.x', t1)[0] != \
            la.fingerprint_text('ERROR', 'lib.x', t2)[0]

    def test_different_exc_type_different_fingerprint(self):
        t1 = ('boom\nTraceback (most recent call last):\n'
              '  File "/app/lib/a.py", line 1, in alpha\nKeyError: 1')
        t2 = ('boom\nTraceback (most recent call last):\n'
              '  File "/app/lib/a.py", line 1, in alpha\nValueError: 1')
        assert la.fingerprint_text('ERROR', 'lib.x', t1)[0] != \
            la.fingerprint_text('ERROR', 'lib.x', t2)[0]

    def test_NEUTER_without_signature_frames_collapse(self, monkeypatch):
        """NEUTER 负对照:挖掉异常签名后,上面 test_different_frame 的两条
        traceback 坍缩成同一指纹——证明签名是承重墙,不是摆设。"""
        monkeypatch.setattr(la, '_exc_signature', lambda text: '')
        t1 = ('boom\nTraceback (most recent call last):\n'
              '  File "/app/lib/a.py", line 1, in alpha\nKeyError: 1')
        t2 = ('boom\nTraceback (most recent call last):\n'
              '  File "/app/lib/a.py", line 2, in beta\nValueError: 2')
        assert la.fingerprint_text('ERROR', 'lib.x', t1)[0] == \
            la.fingerprint_text('ERROR', 'lib.x', t2)[0], \
            'neutered signature should collapse distinct tracebacks'

    def test_no_traceback_no_signature(self):
        _, template = la.fingerprint_text('ERROR', 'lib.x', 'plain message')
        assert '[' not in template  # 无签名,展示模板不带尾巴


# ═══ 3. 续行归属 + 三路径同一指纹 ═══

class TestContinuationAttribution:
    SYNTHETIC = (
        '2026-08-06 10:00:00 [ERROR] lib.foo [Thread-1]: first line boom\n'
        'Traceback (most recent call last):\n'
        '  File "/x/y/z.py", line 10, in run\n'
        '    do()\n'
        'KeyError: 404\n'
        '2026-08-06 10:00:01 [WARNING] lib.bar [MainThread]: next record\n'
    )

    def test_traceback_block_is_one_record(self):
        records = list(la.replay_log_lines(self.SYNTHETIC.splitlines(True)))
        assert len(records) == 2
        level, name, text, ts_ms = records[0]
        assert (level, name) == ('ERROR', 'lib.foo')
        assert 'Traceback' in text and text.endswith('KeyError: 404')
        assert ts_ms > 0

    def test_continuation_never_starts_a_record(self):
        """owner 验收口径 #1:回放产物里没有任何记录以 'Traceback' 开头。"""
        records = list(la.replay_log_lines(self.SYNTHETIC.splitlines(True)))
        assert not any(t.startswith('Traceback') for _, _, t, _ in records)

    def test_leading_orphan_continuation_is_skipped(self):
        text = '  File "/x.py", line 1, in <module>\n' + self.SYNTHETIC
        records = list(la.replay_log_lines(text.splitlines(True)))
        assert len(records) == 2  # 无头续行被丢弃,不产生幽灵记录

    def test_handler_raw_and_prerendered_paths_agree(self):
        """同步路径(exc_info 原始)与 listener 路径(msg 已内联渲染)必须
        产出同一指纹——这是「指纹语义只有一个实现」的实证。"""
        exc_info = _exc_info()
        store_raw, store_pre = la.AggregateStore(), la.AggregateStore()
        la.FingerprintHandler(store_raw).emit(
            logging.LogRecord('lib.foo', logging.ERROR, __file__, 10,
                              'boom %s', ('x',), exc_info))
        prerendered = ('boom x\n'
                       + logging.Formatter().formatException(exc_info))
        la.FingerprintHandler(store_pre).emit(
            logging.LogRecord('lib.foo', logging.ERROR, __file__, 10,
                              prerendered, (), None))
        fp_raw = store_raw.snapshot()[0]['fingerprint']
        fp_pre = store_pre.snapshot()[0]['fingerprint']
        assert fp_raw == fp_pre

    def test_handler_counts_and_never_raises(self):
        store = la.AggregateStore()
        h = la.FingerprintHandler(store)
        for _ in range(5):
            h.emit(_record('same thing happened'))
        # msg 带两个 %s 但只给一个 arg:getMessage 会炸 → handleError,不抛。
        h.emit(logging.LogRecord('lib.foo', logging.ERROR, __file__, 10,
                                 'args mismatch %s %s', ('x',), None))
        rows = store.snapshot()
        assert len(rows) == 1 and rows[0]['count'] == 5
        assert rows[0]['level'] == 'ERROR' and rows[0]['logger'] == 'lib.foo'

    def test_store_cap_overflows_into_single_bucket(self):
        store = la.AggregateStore(cap=2)
        h = la.FingerprintHandler(store)
        h.emit(_record('msg one'))
        h.emit(_record('msg two'))
        h.emit(_record('msg three'))
        h.emit(_record('msg four'))
        rows = {r['fingerprint']: r for r in store.snapshot()}
        assert len(rows) == 3  # 2 真指纹 + overflow 桶,内存有界
        overflow = rows[la.AggregateStore.OVERFLOW_FP]
        assert overflow['count'] == 2
        assert overflow['level'] == '*'


# ═══ 4. 落库(真 SQLite)═══

class _DbBase:
    @pytest.fixture()
    def fresh_db(self, tmp_path, monkeypatch):
        from lib.storage import StorageSupervisor

        supervisor = StorageSupervisor(
            project_root=tmp_path / 'sidecar', backend='sqlite',
            startup_timeout=60)
        supervisor.start()
        monkeypatch.setattr(
            la, '_storage', lambda **_kwargs: supervisor.client)
        try:
            yield supervisor
        finally:
            supervisor.stop()

    @staticmethod
    def _rows():
        return la.query_aggregates(limit=500)['items']


class TestFlush(_DbBase):
    def _store_with(self, text='boom happened', n=3, ts_sec=None):
        """ts_sec 为 record.created 同语义(epoch 秒 float);默认近实时,
        使 TTL 清扫对本测试永远无感(与 _last_sweep_at 模块态解耦)。"""
        if ts_sec is None:
            ts_sec = time.time() - 10
        store = la.AggregateStore()
        h = la.FingerprintHandler(store)
        for i in range(n):
            rec = logging.LogRecord('lib.foo', logging.ERROR, __file__, 10,
                                    text, (), None)
            rec.created = ts_sec + i
            h.emit(rec)
        return store

    def test_table_created_by_bootstrap(self, fresh_db):
        """新表必须由 always-on bootstrap 创建(selfheal 探针的覆盖对象)。"""
        assert la.query_aggregates()['items'] == []

    def test_upsert_accumulates_across_flushes(self, fresh_db):
        base = time.time() - 10
        la.flush_once(store=self._store_with(n=3, ts_sec=base))
        la.flush_once(store=self._store_with(n=4, ts_sec=base + 100))
        rows = self._rows()
        assert len(rows) == 1
        assert rows[0]['count'] == 7
        assert rows[0]['first_seen'] == int(base * 1000)
        assert rows[0]['last_seen'] >= int((base + 100) * 1000)

    def test_flush_fail_open_requeues(self, fresh_db, monkeypatch):
        store = self._store_with(n=5)
        monkeypatch.setattr(
            la, '_storage',
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError('db down')))
        result = la.flush_once(store=store)
        assert result['ok'] is False
        monkeypatch.setattr(
            la, '_storage', lambda **_kwargs: fresh_db.client)
        # 计数被并回,下个窗口(换了好的 DB)一次补齐,不丢。
        result = la.flush_once(store=store)
        assert result['ok'] is True
        rows = self._rows()
        assert len(rows) == 1 and rows[0]['count'] == 5

    def test_count_flush_and_ttl_sweep_use_separate_priority_transactions(
            self, monkeypatch):
        calls = []

        class Client:
            def command(self, operation, payload, _command_id, *, priority):
                calls.append((operation, payload, priority))
                return {
                    'flushed': len(payload['rows']),
                    'swept': 0,
                }

        store = self._store_with(n=2)
        monkeypatch.setattr(la, '_storage', lambda **_kwargs: Client())
        monkeypatch.setattr(
            la, '_last_sweep_at',
            time.monotonic() - 2 * la._TTL_SWEEP_INTERVAL_SEC)

        result = la.flush_once(store=store)

        assert result == {'ok': True, 'flushed': 1, 'swept': 0}
        assert len(calls) == 2
        assert calls[0][0] == 'log_aggregate.flush'
        assert len(calls[0][1]['rows']) == 1
        assert calls[0][2] == 'event'
        assert calls[1][1]['rows'] == []
        assert 'cutoff_ms' in calls[1][1]
        assert calls[1][2] == 'maintenance'

    def test_ttl_sweep_deletes_only_stale(self, fresh_db, monkeypatch):
        new_ms = 9_000_000_000_000
        # 结构性解耦模块态:setup 两次 flush 若撞上 _last_sweep_at=0
        # (隔离运行/新 worker)会当场把 stale 行扫掉,assert 2 变 1——
        # 本测试的语义只在「强制那一扫」,setup 期必须钉住不扫。
        monkeypatch.setattr(la, '_last_sweep_at', time.monotonic())
        la.flush_once(store=self._store_with('stale thing', n=1, ts_sec=1000.0))
        la.flush_once(store=self._store_with('fresh thing', n=1,
                                             ts_sec=new_ms / 1000))
        assert len(self._rows()) == 2
        # 强制本窗清扫:不能写 0.0——_last_sweep_at 比的是
        # time.monotonic()(开机时长),新 CI VM 开机不足 1h 时
        # monotonic()-0.0 < 3600,强制静默失效(stale 存活,2==1 方向)。
        monkeypatch.setattr(la, '_last_sweep_at',
                            time.monotonic() - 2 * la._TTL_SWEEP_INTERVAL_SEC)
        la.flush_once(store=la.AggregateStore(), now_ms=new_ms)
        rows = self._rows()
        assert len(rows) == 1 and rows[0]['template'].startswith('fresh thing')

    def test_query_aggregates_sort_filter_limit(self, fresh_db):
        la.flush_once(store=self._store_with('alpha spam', n=9))
        la.flush_once(store=self._store_with('beta rare', n=1))
        top = la.query_aggregates(sort='count')
        assert top['total_rows'] == 2 and top['total_events'] == 10
        assert top['items'][0]['count'] == 9
        assert top['items'][0]['last_seen_iso']
        filtered = la.query_aggregates(q='beta')
        assert filtered['total_rows'] == 1
        leveled = la.query_aggregates(level='WARNING')
        assert leveled['total_rows'] == 0
        limited = la.query_aggregates(limit=1)
        assert len(limited['items']) == 1


# ═══ 5. 端点 ═══

class TestAggregatesEndpoint(_DbBase):
    def _app(self):
        from quart import g

        from lib.app_factory import create_base_app
        from lib.api_keys import local_admin_context
        from routes.api_v1.logs import api_v1_logs_bp

        app = create_base_app(__name__, {'TESTING': True})

        @app.before_request
        async def _grant():
            g.auth_ctx = local_admin_context()
            g.rate_decision = None

        app.register_blueprint(api_v1_logs_bp)
        return app

    @staticmethod
    def _get(app, qs):
        async def go():
            r = await app.test_client().get('/api/v1/logs/aggregates' + qs)
            return r.status_code, await r.get_json()
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(go())
        finally:
            loop.close()

    def test_envelope_and_items(self, fresh_db):
        store = la.AggregateStore()
        la.FingerprintHandler(store).emit(_record('endpoint boom'))
        la.flush_once(store=store)
        code, body = self._get(self._app(), '')
        assert code == 200, body
        assert body['ok'] is True
        assert body['total_rows'] == 1 and body['total_events'] == 1
        item = body['items'][0]
        assert item['template'].startswith('endpoint boom')
        assert item['fingerprint'] and item['last_seen_iso']

    def test_invalid_params_are_clean_400(self, fresh_db):
        code, _ = self._get(self._app(), '?level=NOPE')
        assert code == 400
        code, _ = self._get(self._app(), '?sort=DROP%20TABLE')
        assert code == 400
        code, _ = self._get(self._app(), '?limit=abc')
        assert code == 400

    def test_limit_clamped(self, fresh_db):
        code, body = self._get(self._app(), '?limit=99999')
        assert code == 200 and body['ok'] is True


# ═══ 5b. flusher 生命周期(server.py 生产分支调的正是这两个函数)═══

class TestFlusherLifecycle:
    def test_failed_flushes_back_off_and_success_resets(self, monkeypatch):
        monkeypatch.setenv('TOFU_LOG_AGG_FLUSH_SEC', '10')

        assert la._next_flush_delay(10.0, {'ok': False}) == 20.0
        assert la._next_flush_delay(20.0, {'ok': False}) == 40.0
        assert la._next_flush_delay(200.0, None) == 300.0
        assert la._next_flush_delay(300.0, {'ok': True}) == 10.0

    def test_start_is_idempotent_and_loop_flushes(self, monkeypatch):
        calls = []
        monkeypatch.setattr(la, 'flush_once',
                            lambda **kw: calls.append(kw) or {'ok': True})
        monkeypatch.setenv('TOFU_LOG_AGG_FLUSH_SEC', '1')
        monkeypatch.setattr(la, '_last_sweep_at', 0.0)
        try:
            assert la.start_flusher() is True
            first = la._flusher_thread
            assert first.is_alive()
            assert la.start_flusher() is False  # 幂等:不叠线程
            assert la._flusher_thread is first
            deadline = time.time() + 3
            while not calls and time.time() < deadline:
                time.sleep(0.05)
            assert calls, 'flusher loop never called flush_once within 3s'
        finally:
            la.stop_flusher(final_flush=False)
        assert la._flusher_thread is None

    def test_idle_worker_wakes_hourly_instead_of_every_flush_window(
            self, monkeypatch):
        monkeypatch.setenv('TOFU_LOG_AGG_FLUSH_SEC', '15')
        monkeypatch.setattr(la, '_last_sweep_at', 100.0)

        assert la._idle_flush_delay(now=100.0) == 3600.0
        assert la._idle_flush_delay(now=3700.0) == 15.0
        assert 3600 / la._idle_flush_delay(now=100.0) == 1
        assert 3600 / 15 == 240

    def test_default_store_rows_wake_an_hourly_idle_worker(self, monkeypatch):
        store = la.AggregateStore()
        flushed = threading.Event()
        rows_seen = []
        monkeypatch.setattr(la, '_default_store', store)
        monkeypatch.setattr(la, '_last_sweep_at', time.monotonic())
        monkeypatch.setenv('TOFU_LOG_AGG_FLUSH_SEC', '1')

        def flush_once():
            rows_seen.extend(store.snapshot())
            flushed.set()
            return {'ok': True, 'flushed': len(rows_seen), 'swept': 0}

        monkeypatch.setattr(la, 'flush_once', flush_once)
        try:
            assert la.start_flusher() is True
            assert flushed.wait(0.2) is False
            la.FingerprintHandler(store).emit(_record('new aggregate work'))
            assert flushed.wait(2.5) is True
        finally:
            la.stop_flusher(final_flush=False)
        assert len(rows_seen) == 1

    def test_default_store_wakes_only_on_empty_to_pending_transition(
            self, monkeypatch):
        store = la.AggregateStore()
        wakes = []
        monkeypatch.setattr(la, '_default_store', store)
        monkeypatch.setattr(la, '_wake_flusher', lambda: wakes.append('wake'))

        handler = la.FingerprintHandler(store)
        handler.emit(_record('first pending aggregate'))
        handler.emit(_record('second pending aggregate'))

        assert wakes == ['wake']
        assert len(store) == 2

    def test_stop_timeout_retains_live_thread_and_skips_final_flush(
            self, monkeypatch):
        calls = []

        class StuckThread:
            def is_alive(self):
                return True

            def join(self, timeout):
                calls.append(('join', timeout))

        thread = StuckThread()
        monkeypatch.setattr(la, '_flusher_thread', thread)
        monkeypatch.setattr(
            la, 'flush_once', lambda: calls.append(('flush', None)))
        la._flusher_stop.clear()

        assert la.stop_flusher(timeout=0.125) is False
        assert la._flusher_stop.is_set()
        assert la._flusher_thread is thread
        assert calls == [('join', 0.125)]

    def test_kill_switch_disables(self, monkeypatch):
        monkeypatch.setenv('TOFU_LOG_AGGREGATES', '0')
        assert la.enabled() is False
        monkeypatch.setenv('TOFU_LOG_AGGREGATES', '1')
        assert la.enabled() is True


# ═══ 6. 真实日志验收回放(owner 口径 #4)═══

class TestRealErrorLogReplay:
    def test_real_error_log_collapses(self):
        """回放整个轮转族(error.log + error.log.N):单卷可能刚轮转只有
        几十 KB(实测 2026-08-06 轮转把 4.9MB 主卷换成了 65KB 新卷),聚合
        语义本来就跨轮转——指纹按内容计,与文件边界无关。"""
        import glob as _glob
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = sorted(_glob.glob(os.path.join(root, 'logs', 'error.log*')))
        total_bytes = sum(os.path.getsize(p) for p in paths)
        if not paths or total_bytes < 200_000:
            pytest.skip(
                'LOUD-SKIP: real log replay needs a populated logs/error.log* '
                'family (present only on a live install)')
        summary = la.replay_log_files(paths)
        records = summary['total_records']
        unique = summary['unique_fingerprints']
        assert records >= 1000, f'logs too small to be meaningful: {records}'
        # 去重确实发生:指纹数远小于记录数。
        assert unique * 2 <= records, (records, unique)
        # owner 验收口径 #1:没有任何记录以 'Traceback' 开头——续行永远
        # 挂进父记录,不会各成一个模板。
        store = summary['store']
        rows = sorted(store.snapshot(), key=lambda r: -r['count'])
        for r in rows:
            assert not r['sample'].startswith('Traceback'), r['template'][:120]
        # top-20 覆盖率显著(本机实测 top-15 ≈ 69%,阈值取保守下界)。
        top20 = sum(r['count'] for r in rows[:20])
        assert top20 / records >= 0.40, \
            f'top-20 coverage {top20}/{records} = {top20/records:.1%} < 40%'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-x', '-q', '-m', 'unit']))
