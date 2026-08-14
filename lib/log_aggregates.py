"""lib/log_aggregates.py — error.log 指纹聚合层(epic pt_71eaaa8d5b8243e9,2026-08-06).

三层架构(文本日志永远是唯一权威源,本层只是其上的"频率榜单"):

  ① 文本日志(现状)     — 全量原文,轮转保留;本层任何故障都不影响它。
  ② 本模块             — ``FingerprintHandler`` 挂在 QueueListener 线程上
    (server.py ``_real_log_handlers``,与 error.log 同一个
    ``_BizAndServerOnly`` 过滤器),把每条 WARNING+ 记录归一成
    ``(level, logger, 消息模板, 异常签名)`` 指纹,内存计数;后台 daemon
    flusher 每 ``TOFU_LOG_AGG_FLUSH_SEC`` 秒(默认 15)批量 upsert 进
    ``log_aggregates`` 表。DB 写失败只丢聚合、fail-open——绝不反噬业务
    路径。TTL(``TOFU_LOG_AGG_TTL_DAYS``,默认 30 天,对齐 app.log 保留
    期)由同一个 flusher 每小时清扫。``TOFU_LOG_AGGREGATES=0`` 全关。
  ③ 只读视图           — ``GET /api/v1/logs/aggregates``(routes/api_v1/logs.py)
    调 ``query_aggregates``。

多行 traceback 的归属(本层最容易做砸的地方):

  挂在 QueueListener 之后的 handler 看到的 ``record.msg`` 已被
  ``QueueHandler.prepare()`` 渲染成「消息 + 内联 traceback」的整段文本
  (``exc_info`` 已清空),所以文件里的续行在 handler 层面**天然就是一条
  记录**;pytest 同步模式下记录带原始 ``exc_info``,``emit`` 渲染后得到
  同一形状。离线回放(``replay_log_lines``)从文本文件重建同一形状:
  头行开新记录、续行挂到上一条。两条路径共用 ``fingerprint_text`` ——
  指纹语义只有一个实现。

  指纹的异常签名 = 异常类型 + 栈顶帧(最深层 ``File "…", line N, in func``
  的文件基名 + 函数名;不带行号,容忍小编辑)。同一处 bug 刷屏一万次只
  是一行;不同调用点的同名异常是不同指纹。

CLI(离线回放 / 验收 / 手动回填):

    python -m lib.log_aggregates logs/error.log --top 15
    python -m lib.log_aggregates logs/error.log --apply   # 真写 DB
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  配置(env 可调,默认即合理)
# ═══════════════════════════════════════════════════════════════════════

def enabled() -> bool:
    """总开关。``TOFU_LOG_AGGREGATES=0`` 时 server.py 不挂 handler 也不起
    flusher——文本日志路径零改动。"""
    return os.environ.get('TOFU_LOG_AGGREGATES', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def _flush_interval_sec() -> float:
    try:
        return max(1.0, float(os.environ.get('TOFU_LOG_AGG_FLUSH_SEC', '15')))
    except ValueError as e:
        logger.debug('[LogAgg] bad TOFU_LOG_AGG_FLUSH_SEC, defaulting 15s: %s', e)
        return 15.0


def _ttl_days() -> int:
    try:
        return max(1, int(os.environ.get('TOFU_LOG_AGG_TTL_DAYS', '30')))
    except ValueError as e:
        logger.debug('[LogAgg] bad TOFU_LOG_AGG_TTL_DAYS, defaulting 30: %s', e)
        return 30


# 内存指纹表上限:超过后新指纹并入单一 overflow 桶,聚合层自身永不成内存源。
_STORE_CAP = 20000
_TEMPLATE_MAX = 200
_SAMPLE_MAX = 2000
_TTL_SWEEP_INTERVAL_SEC = 3600

# ═══════════════════════════════════════════════════════════════════════
#  指纹归一化
# ═══════════════════════════════════════════════════════════════════════

_URL_RE = re.compile(r'https?://\S+')
_PATH_RE = re.compile(r'(?:/[\w.\-]+){2,}')
# rid 有两种形态:纯 hex(254e0bf0)和 hex-计数(8ea40f-111)——后缀计数
# 同属易变部分,一并吃掉,否则同一模板被 rid 形态拆成两行(实测 306/314 分裂)。
_HEX_RE = re.compile(r'\b[0-9a-fA-F]{6,}(?:-\d+)*\b')
_NUM_RE = re.compile(r'\d+')
_WS_RE = re.compile(r'\s+')

# 日志头行(server.py ``_LOG_FMT``):
#   2026-08-06 13:37:15 [ERROR] tofu_search.fetch.core [Thread-3]: message
_HEADER_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\]'
    r' ([\w.\-]+) \[[^\]]*\]: (.*)$')

# traceback 栈帧 + 异常行
_FRAME_RE = re.compile(r'File "([^"]+)", line \d+, in (\S+)')
_EXC_TOKEN_RE = re.compile(r'^([A-Za-z_][\w.]*)')
_EXC_SUFFIXES = ('Error', 'Exception', 'Warning', 'Exit', 'Interrupt',
                 'Timeout', 'Fault', 'Abort', 'Killed')


def _normalize_template(first_line: str) -> str:
    """把一条消息的首行归一成模板:URL/绝对路径/hex id/数字全部占位化。"""
    t = _URL_RE.sub('<url>', first_line)
    t = _PATH_RE.sub('<path>', t)
    t = _HEX_RE.sub('<hex>', t)
    t = _NUM_RE.sub('<n>', t)
    t = _WS_RE.sub(' ', t).strip()
    return t[:_TEMPLATE_MAX]


def _exc_signature(text: str) -> str:
    """从整段文本里提取异常签名 ``ExcType@file:func``;无 traceback 返回 ''。

    帧取**最深层**(最后一个 ``File ... in func`` 匹配)——它指向抛出点,
    是最稳定的分组键;行号刻意丢弃(小编码改动不该改指纹)。异常类型从
    traceback 末行取,要求带已知后缀,防止把普通末行误当异常类。
    """
    if 'Traceback (most recent call last)' not in text:
        return ''
    frames = _FRAME_RE.findall(text)
    frame_sig = ''
    if frames:
        path, func = frames[-1]
        base = path.replace('\\', '/').rsplit('/', 1)[-1]
        frame_sig = '%s:%s' % (base, func)
    exc_type = ''
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        m = _EXC_TOKEN_RE.match(line)
        if m and any(sfx in m.group(1) for sfx in _EXC_SUFFIXES):
            exc_type = m.group(1)
        break
    if not exc_type and not frame_sig:
        return ''
    return '%s@%s' % (exc_type or '?', frame_sig or '?')


def fingerprint_text(level: str, logger_name: str, full_text: str):
    """(level, logger, 整段文本) → (fingerprint, display_template)。

    ``full_text`` 是「消息首行 + 可选内联 traceback」的整段——listener
    路径、同步路径、离线回放三条路径都先归一到这个形状再进本函数。
    """
    first_line = full_text.split('\n', 1)[0]
    template = _normalize_template(first_line)
    sig = _exc_signature(full_text)
    raw = '%s|%s|%s|%s' % (level, logger_name, template, sig)
    fp = hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()[:16]
    display = template if not sig else '%s [%s]' % (template, sig)
    return fp, display


# ═══════════════════════════════════════════════════════════════════════
#  内存聚合存储
# ═══════════════════════════════════════════════════════════════════════

class AggregateStore:
    """指纹 → 计数 的线程安全内存表(有界)。

    ``snapshot`` 换出整张表给 flusher;flush 失败时 ``requeue`` 把计数并回,
    下个窗口重试——聚合数据宁可晚到不可丢计数(在窗口语义内)。
    """

    OVERFLOW_FP = 'overflow'

    def __init__(self, cap: int = _STORE_CAP):
        self._cap = cap
        self._lock = threading.Lock()
        self._rows = {}

    def add(self, fp: str, *, level: str, logger_name: str,
            template: str, sample: str, ts_ms: int) -> None:
        with self._lock:
            row = self._rows.get(fp)
            if row is None and len(self._rows) >= self._cap:
                fp = self.OVERFLOW_FP
                row = self._rows.get(fp)
                level, logger_name = '*', '*'
                template = '<overflow: too many distinct fingerprints>'
            if row is None:
                self._rows[fp] = {
                    'fingerprint': fp,
                    'level': level,
                    'logger': logger_name,
                    'template': template,
                    'sample': sample[:_SAMPLE_MAX],
                    'count': 1,
                    'first_seen': ts_ms,
                    'last_seen': ts_ms,
                }
            else:
                row['count'] += 1
                row['last_seen'] = max(row['last_seen'], ts_ms)
                row['first_seen'] = min(row['first_seen'], ts_ms)
                # 样例跟最新——排障时最近的上下文最有用。
                row['sample'] = sample[:_SAMPLE_MAX]

    def snapshot(self) -> list:
        """换出当前所有行(返回 list,存储重置为空)。"""
        with self._lock:
            rows = list(self._rows.values())
            self._rows = {}
            return rows

    def requeue(self, rows: list) -> None:
        """flush 失败后把行并回存储(计数相加,不丢)。"""
        with self._lock:
            for r in rows:
                cur = self._rows.get(r['fingerprint'])
                if cur is None:
                    if len(self._rows) >= self._cap:
                        continue  # 上限时宁可丢聚合也不涨内存
                    self._rows[r['fingerprint']] = dict(r)
                else:
                    cur['count'] += r['count']
                    cur['last_seen'] = max(cur['last_seen'], r['last_seen'])
                    cur['first_seen'] = min(cur['first_seen'], r['first_seen'])

    def __len__(self):
        with self._lock:
            return len(self._rows)


_default_store = AggregateStore()


def get_default_store() -> AggregateStore:
    return _default_store


# ═══════════════════════════════════════════════════════════════════════
#  logging.Handler — 挂在 QueueListener 线程上(server.py)
# ═══════════════════════════════════════════════════════════════════════

class FingerprintHandler(logging.Handler):
    """把每条记录归一成指纹写进内存表。

    两种记录形状都正确处理(产出同一指纹):
      - 同步路径(pytest):msg=格式串,args 未合并,exc_info 原始;
      - listener 路径(生产):msg 已渲染(含内联 traceback),args=None,
        exc_info=None。
    emit 绝不抛异常——handler 跑在 listener 线程上,抛出会炸整条日志链。
    """

    def __init__(self, store: AggregateStore):
        super().__init__(level=logging.WARNING)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                # 只有同步路径会走到这:listener 路径 traceback 已内联进
                # msg 且 exc_info 已清空,再渲染会重复。
                text = '%s\n%s' % (
                    text, logging.Formatter().formatException(record.exc_info))
            fp, template = fingerprint_text(
                record.levelname, record.name, text)
            self.store.add(
                fp, level=record.levelname, logger_name=record.name,
                template=template, sample=text,
                ts_ms=int(record.created * 1000))
        except Exception:
            self.handleError(record)


# ═══════════════════════════════════════════════════════════════════════
#  落库 — 批量 upsert + TTL 清扫(全部 fail-open)
# ═══════════════════════════════════════════════════════════════════════

_last_sweep_at = 0.0
_last_fail_log_at = 0.0


def _storage(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


def flush_once(store: AggregateStore = None, now_ms: int = None) -> dict:
    """把 ``store`` 的当前快照批量 upsert 进 log_aggregates;顺手做每小时
    一次的 TTL 清扫。

    fail-open:DB 任何异常只丢这一次 flush(行并回存储下窗重试),并以
    10 分钟节流的 WARNING 留痕(§2.2:可恢复意外 → warning;节流防止
    DB 长故障时聚合层自己变成噪音源,debug 级每次都记)。
    """
    global _last_sweep_at, _last_fail_log_at
    # 必须 is None:AggregateStore 定义了 __len__,空表在 or 语义下是
    # falsy,会把调用方显式传入的空 store 静默换成默认 store。
    store = store if store is not None else _default_store
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    rows = store.snapshot()
    swept = 0
    flushed = 0
    now_mono = time.monotonic()
    did_sweep = now_mono - _last_sweep_at >= _TTL_SWEEP_INTERVAL_SEC
    try:
        chunks = [rows[index:index + 500] for index in range(0, len(rows), 500)]
        if not chunks and did_sweep:
            chunks = [[]]
        for index, chunk in enumerate(chunks):
            payload = {'rows': chunk}
            if did_sweep and index == len(chunks) - 1:
                payload['cutoff_ms'] = now_ms - _ttl_days() * 86400_000
            result = _storage(write=True).command(
                'log_aggregate.flush', payload, None, priority='event')
            flushed += int(result.get('flushed') or 0)
            swept += int(result.get('swept') or 0)
        if did_sweep:
            _last_sweep_at = now_mono
        return {'ok': True, 'flushed': flushed, 'swept': swept}
    except Exception as e:
        remaining = rows[flushed:]
        if remaining:
            store.requeue(remaining)
        logger.debug(
            '[LogAgg] Sidecar flush failed (fail-open, rows requeued): %s',
            type(e).__name__)
        if time.monotonic() - _last_fail_log_at >= 600:
            _last_fail_log_at = time.monotonic()
            logger.warning(
                '[LogAgg] aggregate flush failing (counts retained in '
                'memory, text logs unaffected): %s', type(e).__name__)
        return {'ok': False, 'flushed': flushed, 'swept': 0}


# ═══════════════════════════════════════════════════════════════════════
#  后台 flusher(生产;pytest 下不自动启动,测试直调 flush_once)
# ═══════════════════════════════════════════════════════════════════════

_flusher_thread = None
_flusher_stop = threading.Event()
_flusher_lock = threading.Lock()


def _flusher_loop() -> None:
    while not _flusher_stop.wait(_flush_interval_sec()):
        flush_once()


def start_flusher() -> bool:
    """启动(幂等)周期 flush daemon。返回是否真正新启动。"""
    global _flusher_thread
    with _flusher_lock:
        if _flusher_thread is not None and _flusher_thread.is_alive():
            return False
        _flusher_stop.clear()
        t = threading.Thread(target=_flusher_loop, name='tofu-log-agg',
                             daemon=True)
        t.start()
        _flusher_thread = t
    logger.info('[LogAgg] flusher started (interval=%.0fs, ttl=%dd)',
                _flush_interval_sec(), _ttl_days())
    return True


def stop_flusher(final_flush: bool = True, timeout: float = 5.0) -> bool:
    """停 flusher;默认做最后一次 best-effort flush(PG 停止中则跳过,
    退出不被挂死)。返回线程是否已停止。"""
    global _flusher_thread
    with _flusher_lock:
        t = _flusher_thread
    if t is None:
        return True
    _flusher_stop.set()
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[LogAgg] invalid stop timeout; using 5.0: %s', exc)
        wait_seconds = 5.0
    if t is not threading.current_thread():
        t.join(timeout=wait_seconds)
    if t.is_alive():
        return False
    with _flusher_lock:
        if _flusher_thread is t:
            _flusher_thread = None
    if not final_flush:
        return True
    try:
        flush_once()
    except Exception as e:  # flush_once 内部已 fail-open,这里是双保险
        logger.debug('[LogAgg] final flush failed: %s', e)
    return True


# ═══════════════════════════════════════════════════════════════════════
#  只读查询(GET /api/v1/logs/aggregates 的底层)
# ═══════════════════════════════════════════════════════════════════════

_SORTS = {
    'count': 'count DESC, last_seen DESC',
    'last_seen': 'last_seen DESC',
    'level': 'level ASC, count DESC',
}


def query_aggregates(*, level: str = '', sort: str = 'count',
                     limit: int = 100, q: str = '') -> dict:
    """按频率/时间读聚合表。返回 {items, total_rows, total_events}。"""
    result = _storage().query('log_aggregate.query', {
        'level': level, 'sort': sort if sort in _SORTS else 'count',
        'limit': max(1, min(500, int(limit))), 'q': q,
    })
    rows = result.get('items') or []
    from datetime import datetime, timezone

    def _iso(ms):
        try:
            return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError) as e:
            logger.debug('[LogAgg] bad epoch ms %r: %s', ms, e)
            return ''

    items = [{
        'fingerprint': r['fingerprint'],
        'level': r['level'],
        'logger': r['logger'],
        'template': r['template'],
        'sample': r['sample'],
        'count': r['count'],
        'first_seen': r['first_seen'],
        'last_seen': r['last_seen'],
        'first_seen_iso': _iso(r['first_seen']),
        'last_seen_iso': _iso(r['last_seen']),
    } for r in rows]
    return {
        'items': items,
        'total_rows': int(result.get('total_rows') or 0),
        'total_events': int(result.get('total_events') or 0),
    }


# ═══════════════════════════════════════════════════════════════════════
#  离线回放 — 文本日志 → 指纹统计(验收 harness + 手动回填 CLI)
# ═══════════════════════════════════════════════════════════════════════

def replay_log_lines(lines):
    """把日志文件的物理行重建为记录(头行开新记录,续行挂上一条)。

    yield (level, logger_name, full_text, ts_ms)。文件开头的无头续行
    (轮转切开的前半段 traceback)没有父记录可挂,直接跳过。
    """
    from datetime import datetime
    cur = None  # [level, name, parts, ts_ms]
    for raw in lines:
        line = raw.rstrip('\n')
        m = _HEADER_RE.match(line)
        if m:
            if cur is not None:
                yield (cur[0], cur[1], '\n'.join(cur[3]), cur[2])
            ts_str, level, name, msg = m.groups()
            try:
                ts_ms = int(datetime.strptime(
                    ts_str, '%Y-%m-%d %H:%M:%S').timestamp() * 1000)
            except ValueError as e:
                logger.debug('[LogAgg] unparsable log timestamp %r, ts=0: %s',
                             ts_str, e)
                ts_ms = 0
            cur = [level, name, ts_ms, [msg]]
        elif cur is not None:
            cur[3].append(line)
        # else: 无头续行(轮转切片开头),跳过
    if cur is not None:
        yield (cur[0], cur[1], '\n'.join(cur[3]), cur[2])


def replay_log_file(path: str, store: AggregateStore = None) -> dict:
    """回放整个文件进 store(默认新独立 store),返回统计摘要。"""
    store = store if store is not None else AggregateStore()  # __len__ falsy 陷阱
    total_lines = 0
    total_records = 0
    with open(path, encoding='utf-8', errors='replace') as f:
        def _gen():
            nonlocal total_lines
            for line in f:
                total_lines += 1
                yield line
        for level, name, text, ts_ms in replay_log_lines(_gen()):
            fp, template = fingerprint_text(level, name, text)
            store.add(fp, level=level, logger_name=name, template=template,
                      sample=text, ts_ms=ts_ms)
            total_records += 1
    return {
        'path': path,
        'total_lines': total_lines,
        'total_records': total_records,
        'unique_fingerprints': len(store),
        'store': store,
    }


def replay_log_files(paths, store: AggregateStore = None) -> dict:
    """跨多个文件(轮转族)回放进同一个 store——指纹按内容计,跨文件
    去重天然成立。返回合计摘要。"""
    store = store if store is not None else AggregateStore()  # __len__ falsy 陷阱
    total = {'paths': [], 'total_lines': 0, 'total_records': 0,
             'store': store}
    for p in paths:
        s = replay_log_file(p, store=store)
        total['paths'].append(p)
        total['total_lines'] += s['total_lines']
        total['total_records'] += s['total_records']
    total['unique_fingerprints'] = len(store)
    return total


def _main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog='python -m lib.log_aggregates',
        description='Replay text log file(s) through the fingerprint '
                    'aggregator. Pass the whole rotation family to span '
                    'rotations: logs/error.log logs/error.log.1 …')
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--top', type=int, default=15)
    ap.add_argument('--apply', action='store_true',
                    help='write the aggregated rows into the DB')
    args = ap.parse_args(argv)

    summary = replay_log_files(args.paths)
    store = summary['store']
    rows = sorted(store.snapshot(), key=lambda r: -r['count'])
    print('files=%s' % ','.join(summary['paths']))
    print('physical lines : %d' % summary['total_lines'])
    print('records        : %d (continuation lines attached)' % summary['total_records'])
    print('fingerprints   : %d' % summary['unique_fingerprints'])
    if summary['total_records']:
        top_sum = sum(r['count'] for r in rows[:args.top])
        print('top-%d coverage: %d/%d = %.1f%%'
              % (args.top, top_sum, summary['total_records'],
                 100.0 * top_sum / summary['total_records']))
    print()
    for r in rows[:args.top]:
        print('%6d  %s | %s | %s'
              % (r['count'], r['level'], r['logger'], r['template'][:150]))
    if args.apply:
        result = flush_once(store=store)
        print('\nflush: %s' % result)
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
