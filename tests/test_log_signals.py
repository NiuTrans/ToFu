"""tests/test_log_signals.py — lib/log_signals 差分信号层 + 预算过滤器测试。

分层(2026-08-21,owner 口径「日志服务 LLM,只给差分信号」):

  1. 差分打标(build_digest 纯函数):NEW / ESCALATING / RECURRING /
     RESOLVED 四态边界;NEUTER 负对照——没有基线时 ESCALATING 永不出
     (证明基线是承重墙,冷启动不误报)。
  2. 速率护栏:escalate_floor_per_hour 拦住「1 次 → 3 次」的伪恶化。
  3. 基线快照:写盘/间隔幂等/强制刷新/有界截断/写失败 fail-open。
  4. 预算过滤器:INFO 超预算丢弃、WARNING 永放行、每窗一条信号、
     窗口复位恢复、TOFU_LOG_BUDGET=0 全关。
  5. 端点:信封形状 + 参数校验 + compute 故障 fail-open。

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_log_signals.py -m unit
"""

from __future__ import annotations

import logging
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.log_signals as ls

pytestmark = pytest.mark.unit

NOW_MS = 1_800_000_000_000  # 固定参考点,测试与真实时钟解耦


def _item(fp, count, first_h_ago, last_h_ago, template=None, sample=''):
    return {
        'fingerprint': fp,
        'level': 'ERROR',
        'logger': 'lib.x',
        'template': template or ('tmpl %s' % fp),
        'sample': sample or ('%s boom' % fp),
        'count': count,
        'first_seen': NOW_MS - int(first_h_ago * 3_600_000),
        'last_seen': NOW_MS - int(last_h_ago * 3_600_000),
    }


def _build(items, **kw):
    kw.setdefault('now_ms', NOW_MS)
    return ls.build_digest(items, **kw)


# ═══ 1. 差分打标 ═══

class TestClassification:
    def test_new_within_window(self):
        d = _build([_item('fp1', 5, first_h_ago=2, last_h_ago=0)])
        assert [e['fingerprint'] for e in d['new']] == ['fp1']
        assert d['summary']['new'] == 1
        assert not d['escalating'] and not d['recurring_top']

    def test_new_respects_window_boundary(self):
        d = _build([_item('fp1', 5, first_h_ago=30, last_h_ago=20)],
                   window_hours=24)
        # 30h 前首见、20h 前还在烧:不是 NEW,是 RECURRING。
        assert d['summary']['new'] == 0
        assert d['summary']['recurring'] == 1

    def test_escalating_with_baseline(self):
        """终生 1/h 的老指纹,最近 2h 烧了 180 次(90/h)→ ESCALATING。"""
        item = _item('fp1', 240, first_h_ago=240, last_h_ago=0)
        baseline = {'fp1': {'count': 60, 'ts': NOW_MS - 7_200_000}}
        d = _build([item], baseline=baseline,
                   baseline_ts_ms=NOW_MS - 7_200_000)
        assert [e['fingerprint'] for e in d['escalating']] == ['fp1']
        entry = d['escalating'][0]
        assert entry['count_since_baseline'] == 180
        assert entry['recent_rate_per_hour'] == 90.0
        assert entry['lifetime_rate_per_hour'] == 1.0

    def test_recurring_steady_old_problem(self):
        """老问题按原速率继续烧:是存量债,不是恶化。"""
        item = _item('fp1', 240, first_h_ago=240, last_h_ago=0)
        baseline = {'fp1': {'count': 238, 'ts': NOW_MS - 7_200_000}}
        d = _build([item], baseline=baseline,
                   baseline_ts_ms=NOW_MS - 7_200_000)
        assert d['summary']['recurring'] == 1
        assert not d['escalating']

    def test_resolved_after_silence(self):
        d = _build([_item('fp1', 9, first_h_ago=100, last_h_ago=48)])
        assert d['summary']['resolved'] == 1
        assert d['summary']['recurring'] == 0

    def test_NEUTER_without_baseline_escalating_never_fires(self):
        """NEUTER 负对照:挖掉基线后,上面的恶化候选只能落 RECURRING——
        证明「recent_rate 来自基线对照」是承重墙,冷启动绝不猜恶化。"""
        item = _item('fp1', 240, first_h_ago=240, last_h_ago=0)
        d = _build([item], baseline=None, baseline_ts_ms=0)
        assert d['escalating'] == []
        assert d['summary']['recurring'] == 1
        assert d['baseline_age_s'] is None

    def test_escalate_floor_blocks_tiny_noise(self):
        """1 → 3 次是 ×3 但绝对量太小,floor(默认 6/h)拦下伪恶化。"""
        item = _item('fp1', 4, first_h_ago=200, last_h_ago=0)
        baseline = {'fp1': {'count': 1, 'ts': NOW_MS - 3_600_000}}
        d = _build([item], baseline=baseline,
                   baseline_ts_ms=NOW_MS - 3_600_000)
        assert d['escalating'] == []
        assert d['summary']['recurring'] == 1

    def test_sections_bounded_by_max_items(self):
        items = [_item('fp%d' % i, 100 - i, 2, 0) for i in range(5)]
        d = _build(items, max_items=2)
        assert len(d['new']) == 2
        assert d['summary']['new'] == 5  # 摘要是全量计数,截断只截列表
        assert [e['count'] for e in d['new']] == [100, 99]  # 按量排序

    def test_resolved_sorted_by_recency(self):
        items = [_item('old', 1, 500, 100), _item('fresh', 1, 500, 30)]
        d = _build(items)
        assert [e['fingerprint'] for e in d['resolved']] == ['fresh', 'old']


# ═══ 2. 回查指针(rid / grep_hint)═══

class TestPointers:
    def test_rid_extracted_from_sample(self):
        item = _item('fp1', 1, 2, 0,
                     sample='[rid:abc123def] something failed')
        assert _build([item])['new'][0]['rid'] == 'abc123def'

    def test_task_id_fallback(self):
        item = _item('fp1', 1, 2, 0, sample='[Task bf9d7f8b][R20] broke')
        assert _build([item])['new'][0]['rid'] == 'bf9d7f8b'

    def test_grep_hint_stops_at_placeholder(self):
        item = _item('fp1', 1, 2, 0,
                     template='[SyncDrift] STALLED conv=<hex> kind=rev')
        hint = _build([item])['new'][0]['grep_hint']
        assert hint == '[SyncDrift] STALLED conv='
        assert '<hex>' not in hint

    def test_excerpt_truncated(self):
        item = _item('fp1', 1, 2, 0, sample='x' * 1000)
        assert len(_build([item])['new'][0]['excerpt']) == ls._SAMPLE_EXCERPT


# ═══ 3. 基线快照 ═══

class TestBaselineRefresh:
    def test_first_refresh_writes_and_roundtrips(self, tmp_path):
        path = str(tmp_path / 'baseline.json')
        items = [_item('fp1', 42, 100, 0)]
        assert ls.maybe_refresh_baseline(items, now_ms=NOW_MS, path=path)
        doc = ls.load_baseline(path)
        assert doc['_meta']['ts'] == NOW_MS
        assert doc['fp1'] == {'count': 42, 'ts': NOW_MS}

    def test_second_refresh_within_interval_is_noop(self, tmp_path):
        path = str(tmp_path / 'baseline.json')
        items = [_item('fp1', 1, 100, 0)]
        assert ls.maybe_refresh_baseline(items, now_ms=NOW_MS, path=path)
        assert not ls.maybe_refresh_baseline(
            items, now_ms=NOW_MS + 60_000, path=path)

    def test_forced_interval_zero_refreshes(self, tmp_path):
        path = str(tmp_path / 'baseline.json')
        items = [_item('fp1', 1, 100, 0)]
        ls.maybe_refresh_baseline(items, now_ms=NOW_MS, path=path)
        assert ls.maybe_refresh_baseline(
            items, min_interval_s=0, now_ms=NOW_MS + 1, path=path)

    def test_baseline_capped_by_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ls, '_BASELINE_CAP', 2)
        path = str(tmp_path / 'baseline.json')
        items = [_item('big', 100, 100, 0), _item('mid', 50, 100, 0),
                 _item('small', 1, 100, 0)]
        ls.maybe_refresh_baseline(items, now_ms=NOW_MS, path=path)
        doc = ls.load_baseline(path)
        assert set(doc) == {'_meta', 'big', 'mid'}

    def test_write_failure_is_fail_open(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ls, 'write_json_atomic',
                            lambda *a, **k: (_ for _ in ()).throw(
                                OSError('fuse hung')))
        assert not ls.maybe_refresh_baseline(
            [_item('fp1', 1, 100, 0)], now_ms=NOW_MS,
            path=str(tmp_path / 'baseline.json'))

    def test_corrupt_baseline_loads_empty(self, tmp_path):
        path = tmp_path / 'baseline.json'
        path.write_text('{not json', encoding='utf-8')
        assert ls.load_baseline(str(path)) == {}


# ═══ 4. 预算过滤器 ═══

def _rec(name, level, msg='line'):
    return logging.LogRecord(name, level, __file__, 10, msg, (), None)


class TestLogBudgetFilter:
    def test_under_budget_passes(self):
        f = ls.LogBudgetFilter(per_minute=3)
        assert all(f.filter(_rec('lib.x', logging.INFO)) for _ in range(3))

    def test_over_budget_drops_info_but_never_warning(self):
        f = ls.LogBudgetFilter(per_minute=2)
        assert f.filter(_rec('lib.x', logging.INFO))
        assert f.filter(_rec('lib.x', logging.INFO))
        assert not f.filter(_rec('lib.x', logging.INFO))
        assert not f.filter(_rec('lib.x', logging.DEBUG))
        # 信号通道绝不被预算掐断:
        assert f.filter(_rec('lib.x', logging.WARNING))
        assert f.filter(_rec('lib.x', logging.CRITICAL))

    def test_notice_emitted_once_per_window(self):
        f = ls.LogBudgetFilter(per_minute=1)
        notices = []

        class _Grab(logging.Handler):
            def emit(self, record):
                notices.append(record.getMessage())

        lg = logging.getLogger('server.logging')
        h = _Grab()
        lg.addHandler(h)
        old_prop, old_level = lg.propagate, lg.level
        lg.propagate, lg.level = False, logging.WARNING
        try:
            f.filter(_rec('lib.noisy', logging.INFO))
            for _ in range(10):
                f.filter(_rec('lib.noisy', logging.INFO))
        finally:
            lg.removeHandler(h)
            lg.propagate, lg.level = old_prop, old_level
        assert len(notices) == 1
        assert 'lib.noisy' in notices[0]

    def test_window_reset_restores_flow(self):
        f = ls.LogBudgetFilter(per_minute=1, window_s=0.05)
        assert f.filter(_rec('lib.x', logging.INFO))
        assert not f.filter(_rec('lib.x', logging.INFO))
        time.sleep(0.06)
        assert f.filter(_rec('lib.x', logging.INFO))

    def test_kill_switch_disabled(self, monkeypatch):
        monkeypatch.setenv('TOFU_LOG_BUDGET', '0')
        f = ls.LogBudgetFilter(per_minute=1)
        f.filter(_rec('lib.x', logging.INFO))
        assert f.filter(_rec('lib.x', logging.INFO))

    def test_separate_loggers_separate_budgets(self):
        f = ls.LogBudgetFilter(per_minute=1)
        assert f.filter(_rec('lib.a', logging.INFO))
        assert f.filter(_rec('lib.b', logging.INFO))
        assert not f.filter(_rec('lib.a', logging.INFO))


# ═══ 5. 端点 ═══

class TestDigestEndpoint:
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
            r = await app.test_client().get('/api/v1/logs/digest' + qs)
            return r.status_code, await r.get_json()
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(go())
        finally:
            loop.close()

    def test_envelope(self, monkeypatch):
        canned = {'summary': {'new': 1}, 'new': [{'fingerprint': 'fp1'}],
                  'escalating': [], 'recurring_top': [], 'resolved': []}
        monkeypatch.setattr(ls, 'compute_digest', lambda **kw: canned)
        code, body = self._get(self._app(), '')
        assert code == 200, body
        assert body['ok'] is True
        assert body['summary']['new'] == 1
        assert body['new'][0]['fingerprint'] == 'fp1'

    def test_invalid_params_are_clean_400(self):
        assert self._get(self._app(), '?window_hours=0')[0] == 400
        assert self._get(self._app(), '?window_hours=999')[0] == 400
        assert self._get(self._app(), '?max_items=0')[0] == 400
        assert self._get(self._app(), '?max_items=abc')[0] == 400
        assert self._get(self._app(), '?escalate_factor=1.0')[0] == 400

    def test_compute_failure_is_fail_open(self, monkeypatch):
        def _boom(**kw):
            raise RuntimeError('sidecar down')
        monkeypatch.setattr(ls, 'compute_digest', _boom)
        code, body = self._get(self._app(), '')
        assert code == 200
        assert body['ok'] is True and body['unavailable'] is True
        assert body['new'] == []


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-x', '-q', '-m', 'unit']))
