"""lib/log_signals.py — 面向 LLM 消费的日志差分信号层 + 模块行数预算。

三层日志架构(2026-08-21;文本日志永远是唯一权威源):

  ① 证据层  logs/app.log + error.log —— 全量原文,轮转保留,按需 grep。
  ② 聚合层  lib/log_aggregates.py —— WARNING+ 指纹频率榜单(已有),
             回答「哪些问题在刷屏」。
  ③ 本层    差分打标 —— 回答「今天该修什么」:
               NEW        窗口内首次出现的指纹(新 bug)
               ESCALATING 近期速率 > escalate_factor × 终生速率(在恶化)
               RECURRING  老问题仍在烧(存量债,按量排序即可)
               RESOLVED   resolve_hours 未再现(可关闭)
             入口是 ``GET /api/v1/logs/digest``(routes/api_v1/logs.py),
             LLM 每轮只读这一份有界摘要,需要细节时拿条目里的 ``rid`` /
             ``grep_hint`` 回查证据层。

差分需要「上次看到多少」的对照点:``maybe_refresh_baseline`` 把当前计数
快照进 ``data/config/log_digest_baseline.json``(json_store 原子写,
默认每小时最多刷新一次,``TOFU_LOG_DIGEST_SNAPSHOT_SEC`` 可调)。首次
运行没有基线时 ESCALATING 永不误报——只出 NEW/RECURRING,这是诚实的
冷启动(测试里有 NEUTER 负对照钉住这一点)。

``LogBudgetFilter`` 是另一件事:挂在 app.log handler 上的防洪保险。
单一模块 INFO/DEBUG 行数超过 ``TOFU_LOG_BUDGET_PER_MIN``(默认 300 行/
分钟)即丢弃本窗剩余常规行,并发一条 WARNING 信号(进 error.log +
聚合层,自然成为指纹)。WARNING+ 永远放行——信号通道绝不被预算掐断。
``TOFU_LOG_BUDGET=0`` 全关。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from lib.json_store import read_json, write_json_atomic
from lib.log import LOG_DIR, get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  模块行数预算(app.log 防洪保险)
# ═══════════════════════════════════════════════════════════════════════

def budget_enabled() -> bool:
    """``TOFU_LOG_BUDGET=0`` 时过滤器全放行(默认开)。"""
    return os.environ.get('TOFU_LOG_BUDGET', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def _budget_per_minute() -> int:
    try:
        value = int(os.environ.get('TOFU_LOG_BUDGET_PER_MIN', '') or '300')
    except (TypeError, ValueError) as e:
        logger.debug('[LogBudget] bad TOFU_LOG_BUDGET_PER_MIN, default 300: %s', e)
        value = 300
    return max(10, min(100_000, value))


class LogBudgetFilter(logging.Filter):
    """Per-logger INFO/DEBUG 行数预算(固定窗口)。WARNING+ 永远放行。

    只挂 app.log handler:error.log 本来就只有 WARNING+,access.log 是
    HTTP 流水自己的家。超限模块每窗只发一条 ``server.logging`` WARNING
    (预算信号),它自身是 WARNING 级,不可能被本过滤器递归掐掉。
    """

    _BUCKET_CAP = 10000

    def __init__(self, per_minute: int = None, window_s: float = 60.0):
        super().__init__()
        self._per_minute = per_minute
        # 显式参数是可信的(测试用小窗口);只防非正数让窗口永不复位。
        self._window_s = max(0.001, float(window_s))
        self._lock = threading.Lock()
        # name -> [window_start_mono, count, notified]
        self._buckets = {}

    def _limit(self) -> int:
        return self._per_minute if self._per_minute is not None \
            else _budget_per_minute()

    def filter(self, record: logging.LogRecord) -> bool:
        if not budget_enabled():
            return True
        if record.levelno >= logging.WARNING:
            return True
        now = time.monotonic()
        name = record.name
        over = False
        with self._lock:
            bucket = self._buckets.get(name)
            if bucket is None or now - bucket[0] >= self._window_s:
                if bucket is None and len(self._buckets) >= self._BUCKET_CAP:
                    self._buckets.clear()  # 有界:宁可重置窗口不涨内存
                self._buckets[name] = [now, 1, False]
                return True
            bucket[1] += 1
            if bucket[1] <= self._limit():
                return True
            if not bucket[2]:
                bucket[2] = True
                over = True
        if over:
            # 锁外发信号:这条 WARNING 走 root → 各 handler,级高直过预算。
            logging.getLogger('server.logging').warning(
                '[LogBudget] %s exceeded %d INFO/DEBUG lines per %ds — '
                'dropping routine lines from this module until the window '
                'resets (flood insurance; TOFU_LOG_BUDGET=0 disables)',
                name, self._limit(), int(self._window_s),
                extra={
                    'tofu_event_name': 'logging.module_budget_exceeded',
                    'tofu_event_fields': {
                        'logger': name,
                        'limit': self._limit(),
                        'window_seconds': self._window_s,
                    },
                })
        return False


# ═══════════════════════════════════════════════════════════════════════
#  基线快照(差分对照点)
# ═══════════════════════════════════════════════════════════════════════

_BASELINE_CAP = 5000


def _baseline_path() -> str:
    # LOG_DIR = <writable_base>/logs;数据与日志同住一个可写根。
    return os.path.join(os.path.dirname(LOG_DIR),
                        'data', 'config', 'log_digest_baseline.json')


def _snapshot_interval_sec() -> float:
    try:
        return max(60.0, float(os.environ.get(
            'TOFU_LOG_DIGEST_SNAPSHOT_SEC', '3600')))
    except ValueError as e:
        logger.debug('[LogDigest] bad TOFU_LOG_DIGEST_SNAPSHOT_SEC, '
                     'default 3600: %s', e)
        return 3600.0


def load_baseline(path: str = None) -> dict:
    """读基线文档;缺失/损坏 → {}(冷启动,不误报 ESCALATING)。"""
    doc = read_json(path or _baseline_path(), default={})
    return doc if isinstance(doc, dict) else {}


def maybe_refresh_baseline(items: list, *, min_interval_s: float = None,
                           now_ms: int = None, path: str = None) -> bool:
    """距上次快照超过间隔就把当前聚合计数写成新基线。返回是否真刷新。

    只保留当前表里的指纹(自然淘汰 TTL 清掉的旧条目),并按计数截断到
    ``_BASELINE_CAP``——基线文件永远有界。写失败只丢一次快照(fail-open),
    下个窗口重试。
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    interval = min_interval_s if min_interval_s is not None \
        else _snapshot_interval_sec()
    path = path or _baseline_path()
    doc = load_baseline(path)
    meta = doc.get('_meta') if isinstance(doc.get('_meta'), dict) else {}
    last_ts = int(meta.get('ts') or 0)
    if last_ts and (now_ms - last_ts) < interval * 1000:
        return False
    top = sorted(items, key=lambda r: -(int(r.get('count') or 0)))
    new_doc = {'_meta': {'ts': now_ms}}
    for row in top[:_BASELINE_CAP]:
        fp = row.get('fingerprint')
        if not fp:
            continue
        new_doc[fp] = {'count': max(0, int(row.get('count') or 0)),
                       'ts': now_ms}
    try:
        write_json_atomic(path, new_doc, indent=None, sort_keys=True)
    except OSError as e:
        logger.warning('[LogDigest] baseline snapshot write failed '
                       '(differential view degraded, text logs unaffected): %s', e)
        return False
    logger.info('[LogDigest] baseline refreshed: %d fingerprints', len(new_doc) - 1)
    return True


# ═══════════════════════════════════════════════════════════════════════
#  差分打标(纯函数,测试主战场)
# ═══════════════════════════════════════════════════════════════════════

_SAMPLE_EXCERPT = 240
# 速率地板:首见即爆发时 age≈0,没有地板 lifetime_rate 会爆 ∞。
_MIN_SPAN_HOURS = 1.0 / 60.0

_RID_PATTERNS = (
    re.compile(r'\[rid:([0-9A-Za-z._-]{1,64})\]'),
    re.compile(r'\[Task ([0-9a-f]{6,12})\]'),
    re.compile(r'\[([0-9a-f]{8}(?:-\d+)?)\]'),
)


def _sample_rid(sample: str) -> str:
    """从样例行里提取一个可回查的关联 id(rid 优先,任务 id 次之)。"""
    for pattern in _RID_PATTERNS:
        m = pattern.search(sample or '')
        if m:
            return m.group(1)
    return ''


def _grep_hint(template: str) -> str:
    """给 LLM 一个可直接 ``grep -F`` 证据层的字面前缀(占位符前截断)。"""
    prefix = (template or '').split('<', 1)[0].strip()
    return prefix[:60] if len(prefix) >= 8 else (template or '')[:60]


def _iso(ms: int) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError) as e:
        logger.debug('[LogDigest] bad epoch ms %r: %s', ms, e)
        return ''


def build_digest(items: list, *, now_ms: int, window_hours: float = 24,
                 max_items: int = 20, escalate_factor: float = 3.0,
                 escalate_floor_per_hour: float = 6.0,
                 resolve_hours: float = 24, baseline: dict = None,
                 baseline_ts_ms: int = 0) -> dict:
    """把聚合行打成差分摘要。纯函数:items/baseline/now 全部注入。

    ``baseline``: {fingerprint: {'count': N, 'ts': ms}} —— 上次快照。
    ``recent_rate`` 只在有对照点时计算;没有对照点的指纹诚实落
    NEW/RECURRING,绝不猜 ESCALATING。
    """
    window_ms = window_hours * 3_600_000
    resolve_ms = resolve_hours * 3_600_000
    baseline = baseline or {}
    sections = {'new': [], 'escalating': [], 'recurring': [], 'resolved': []}

    for row in items:
        fp = str(row.get('fingerprint') or '')
        count = max(0, int(row.get('count') or 0))
        first_seen = int(row.get('first_seen') or 0)
        last_seen = int(row.get('last_seen') or 0)
        age_hours = max((now_ms - first_seen) / 3_600_000, _MIN_SPAN_HOURS) \
            if first_seen else _MIN_SPAN_HOURS
        lifetime_rate = count / age_hours

        recent_rate = None
        count_delta = None
        prev = baseline.get(fp)
        if isinstance(prev, dict) and baseline_ts_ms and now_ms > baseline_ts_ms:
            delta = count - int(prev.get('count') or 0)
            hours = (now_ms - baseline_ts_ms) / 3_600_000
            if delta > 0 and hours > 0:
                count_delta = delta
                recent_rate = delta / hours

        sample = str(row.get('sample') or '')
        entry = {
            'fingerprint': fp,
            'level': str(row.get('level') or ''),
            'logger': str(row.get('logger') or ''),
            'template': str(row.get('template') or ''),
            'count': count,
            'count_since_baseline': count_delta,
            'lifetime_rate_per_hour': round(lifetime_rate, 2),
            'recent_rate_per_hour': (round(recent_rate, 2)
                                     if recent_rate is not None else None),
            'first_seen': _iso(first_seen),
            'last_seen': _iso(last_seen),
            'rid': _sample_rid(sample),
            'grep_hint': _grep_hint(str(row.get('template') or '')),
            'excerpt': sample[:_SAMPLE_EXCERPT],
        }

        if last_seen and last_seen < now_ms - resolve_ms:
            sections['resolved'].append(entry)
        elif first_seen and first_seen >= now_ms - window_ms:
            sections['new'].append(entry)
        elif (recent_rate is not None
              and recent_rate >= escalate_floor_per_hour
              and recent_rate > escalate_factor * lifetime_rate):
            sections['escalating'].append(entry)
        else:
            sections['recurring'].append(entry)

    sections['new'].sort(key=lambda e: -e['count'])
    sections['escalating'].sort(
        key=lambda e: -(e['recent_rate_per_hour'] or 0))
    sections['recurring'].sort(key=lambda e: -e['count'])
    sections['resolved'].sort(key=lambda e: e['last_seen'], reverse=True)

    summary = {name: len(entries) for name, entries in sections.items()}
    summary['unique_fingerprints'] = len(items)
    summary['total_events'] = sum(max(0, int(r.get('count') or 0))
                                  for r in items)
    return {
        'generated_at': _iso(now_ms),
        'window_hours': window_hours,
        'summary': summary,
        'baseline_age_s': (round((now_ms - baseline_ts_ms) / 1000)
                           if baseline_ts_ms else None),
        'new': sections['new'][:max_items],
        'escalating': sections['escalating'][:max_items],
        'recurring_top': sections['recurring'][:max_items],
        'resolved': sections['resolved'][:max_items],
    }


def compute_digest(*, window_hours: float = 24, max_items: int = 20,
                   escalate_factor: float = 3.0,
                   escalate_floor_per_hour: float = 6.0,
                   now_ms: int = None) -> dict:
    """生产包装:查聚合表 + 读基线 → build_digest → 顺手刷新到期基线。"""
    from lib.log_aggregates import query_aggregates

    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    result = query_aggregates(sort='count', limit=500)
    items = result.get('items') or []
    doc = load_baseline()
    meta = doc.get('_meta') if isinstance(doc.get('_meta'), dict) else {}
    baseline = {k: v for k, v in doc.items()
                if not k.startswith('_') and isinstance(v, dict)}
    digest = build_digest(
        items, now_ms=now_ms, window_hours=window_hours,
        max_items=max_items, escalate_factor=escalate_factor,
        escalate_floor_per_hour=escalate_floor_per_hour,
        baseline=baseline, baseline_ts_ms=int(meta.get('ts') or 0))
    digest['total_rows'] = int(result.get('total_rows') or 0)
    digest['total_events'] = int(result.get('total_events') or 0)
    digest['baseline_refreshed'] = maybe_refresh_baseline(
        items, now_ms=now_ms)
    return digest


__all__ = [
    'LogBudgetFilter',
    'budget_enabled',
    'build_digest',
    'compute_digest',
    'load_baseline',
    'maybe_refresh_baseline',
]
