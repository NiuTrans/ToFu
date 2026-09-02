"""Logging runtime construction (extracted from server.py).

Owns the filter/handler/queue classes plus the one-shot ``build_logging_runtime``
builder. ``server.py`` keeps the *control* functions (start/stop aggregate +
listener lifecycle) and the module globals those functions read, fed by the
builder's return tuple.

This module must NOT import ``server`` (server.py imports it).
"""

import logging
import os
import queue as _queue_mod
import re
import sys
import threading
import time

from logging.handlers import (
    QueueHandler,
    QueueListener,
    RotatingFileHandler,
    TimedRotatingFileHandler,
)

from lib.incident_journal import IncidentJournalHandler
from lib.log import LogContextFilter
from lib.log_aggregates import FingerprintHandler
from lib.log_policy import (
    LOG_FILE_MODE,
    STREAM_POLICIES,
    stream_backup_count,
    stream_family_budget_bytes,
    stream_max_bytes,
)
from lib.log_rate_limit import DuplicateCoalescingFilter
from lib.log_redaction import RedactingFormatter
from lib.log_signals import LogBudgetFilter

_LOG_FMT = ('%(asctime)s [%(levelname)s] %(name)s [%(threadName)s]: '
            '%(tofu_correlation_prefix)s%(tofu_coalesce_note)s%(message)s')
_LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'

# 'tofu_search' is the extracted search/fetch library (sibling package). Its
# loggers carry first-class business diagnostics — the per-engine result
# counts, the streaming-fetch race-to-N decisions, the LLM content-filter
# reductions, and the step-by-step pipeline timing breakdown that explains WHY
# a search took N seconds. Treat it as business (→ app.log INFO, error.log
# WARNING+), NOT vendor: routing it to vendor.log at WARNING-only (the old
# behaviour) discarded all the INFO pipeline detail an operator needs to
# diagnose a slow/failed search.
_BIZ_PREFIXES = ('lib.', 'routes.', 'server', 'tofu_search')


class _BizOnly(logging.Filter):
    def filter(self, record):
        return record.name.startswith(_BIZ_PREFIXES)


class _VendorOnly(logging.Filter):
    def filter(self, record):
        return (not record.name.startswith(_BIZ_PREFIXES)
                and not record.name.startswith('frontend')
                and record.name != 'werkzeug'
                and record.name != 'hypercorn'
                and not record.name.startswith('hypercorn.'))


class _FrontendOnly(logging.Filter):
    """The browser-console relay (/api/v1/logs/client → logger 'frontend').
    Owns its own file: the full client stream (INFO included) is far too
    chatty for app.log, and client warnings must not cry wolf in error.log."""
    def filter(self, record):
        return (record.name == 'frontend'
                or record.name.startswith('frontend.'))


class _BizAndServerOnly(logging.Filter):
    def filter(self, record):
        return (record.name.startswith(_BIZ_PREFIXES)
                or record.name == 'hypercorn'
                or record.name.startswith('hypercorn.'))


class _AccessOnly(logging.Filter):
    def filter(self, record):
        return (record.name == 'hypercorn.access'
                or record.name == 'werkzeug')


class _QuietPollFilter(logging.Filter):
    # Successful liveness/poll traffic is an operational metric, not durable
    # line-by-line evidence. Repeated browser-poll 4xx responses are likewise
    # attacker/client-controlled; retain first + power-of-two + five-minute
    # checkpoints while request metrics retain the exact aggregate count.
    _NOISY_PATH_PREFIXES = (
        '/api/health', '/api/v1/health', '/api/browser/commands', '/api/browser/poll',
        '/api/v1/push/poll', '/api/v1/conversations/sync',
        '/api/v3/conversations/',
        '/api/v1/project/brain/summary', '/api/v1/tasks/events',
    )
    _REQUEST_STATUS_RE = re.compile(
        r'"(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+'
        r'([^\s?"]+)[^"]*"\s+(\d{3})\b')

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._browser_failure_counts: dict[str, int] = {}
        self._browser_failure_last_emit: dict[str, float] = {}

    def _allow_browser_failure_checkpoint(self, status: str) -> bool:
        now = time.monotonic()
        with self._lock:
            count = self._browser_failure_counts.get(status, 0) + 1
            self._browser_failure_counts[status] = count
            last = self._browser_failure_last_emit.get(status, 0.0)
            allowed = (
                count <= 2
                or (count & (count - 1)) == 0
                or now - last >= 300.0
            )
            if allowed:
                self._browser_failure_last_emit[status] = now
            return allowed

    def filter(self, record):
        msg = record.getMessage()
        match = self._REQUEST_STATUS_RE.search(msg)
        if match:
            method, path, status = match.groups()
            if (status.startswith(('2', '3'))
                    and any(path.startswith(prefix)
                            for prefix in self._NOISY_PATH_PREFIXES)
                    and (not path.startswith('/api/v3/conversations/')
                         or method in ('GET', 'HEAD'))):
                return False
            if (method == 'POST' and path == '/api/browser/poll'
                    and status.startswith('4')):
                return self._allow_browser_failure_checkpoint(status)
        return True


class _PrivateLogFileMixin:
    """Force every active handler inode to the policy's owner-only mode."""

    def _open(self):
        stream = super()._open()
        try:
            os.fchmod(stream.fileno(), LOG_FILE_MODE)
        except (AttributeError, OSError):
            try:
                os.chmod(self.baseFilename, LOG_FILE_MODE)
            except OSError:
                pass
        return stream


class _PrivateRotatingFileHandler(_PrivateLogFileMixin, RotatingFileHandler):
    pass


class _SizeAndTimeRotatingFileHandler(
        _PrivateLogFileMixin, TimedRotatingFileHandler):
    """Daily rotation with a per-file ceiling and a whole-family budget.

    ``TimedRotatingFileHandler`` alone lets one runaway day create an
    arbitrarily large file (9.1 GiB happened in production).  Multiple size
    rotations within a day receive numeric suffixes, while the usual date
    names and new-user behaviour remain intact.
    """

    def __init__(self, filename, *, max_bytes, total_budget_bytes, **kwargs):
        self.maxBytes = max(0, int(max_bytes))
        self.totalBudgetBytes = max(0, int(total_budget_bytes))
        super().__init__(filename, **kwargs)
        self._prune_total_budget()

    def shouldRollover(self, record):
        if super().shouldRollover(record):
            return 1
        if self.maxBytes <= 0:
            return 0
        if self.stream is None:
            self.stream = self._open()
        self.stream.seek(0, os.SEEK_END)
        rendered = '%s\n' % self.format(record)
        encoding = self.encoding or 'utf-8'
        try:
            incoming = len(rendered.encode(encoding, errors='replace'))
        except LookupError:
            incoming = len(rendered.encode('utf-8', errors='replace'))
        return 1 if self.stream.tell() + incoming >= self.maxBytes else 0

    def rotation_filename(self, default_name):
        candidate = super().rotation_filename(default_name)
        if not os.path.exists(candidate):
            return candidate
        # A time handler normally owns one file per date. Size rotation can
        # happen repeatedly on that date, so choose a lexically ordered unique
        # suffix instead of deleting/replacing the earlier chunk.
        sequence = 1
        while sequence <= 99999:
            numbered = f'{candidate}.{sequence:05d}'
            if not os.path.exists(numbered):
                return numbered
            sequence += 1
        return f'{candidate}.{time.time_ns()}'

    def doRollover(self):
        super().doRollover()
        self._prune_total_budget()

    def _prune_total_budget(self):
        """Delete oldest *rotated* chunks until the family fits its budget."""
        if self.totalBudgetBytes <= 0:
            return
        directory = os.path.dirname(self.baseFilename) or '.'
        prefix = os.path.basename(self.baseFilename) + '.'
        rotated = []
        total = 0
        try:
            total += os.path.getsize(self.baseFilename)
        except OSError:
            pass
        try:
            names = os.listdir(directory)
        except OSError:
            return
        for name in names:
            if not name.startswith(prefix):
                continue
            path = os.path.join(directory, name)
            try:
                if os.path.islink(path):
                    continue
                stat = os.lstat(path)
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            total += stat.st_size
            rotated.append((stat.st_mtime_ns, name, path, stat.st_size))
        for _mtime, _name, path, size in sorted(rotated):
            if total <= self.totalBudgetBytes:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                continue


def _log_queue_capacity() -> int:
    """Bound pending LogRecord memory without requiring install-time tuning."""
    try:
        value = int(os.environ.get('TOFU_LOG_QUEUE_MAX', '') or '20000')
    except (TypeError, ValueError):
        value = 20000
    return max(1000, min(500000, value))


class _BoundedQueueHandler(QueueHandler):
    """Shed a full async queue and summarize drops after it recovers.

    The stdlib handler routes ``queue.Full`` through ``handleError``. During a
    storm that emits a traceback per dropped record and creates another storm,
    so Full is an expected overload signal here rather than an exception.
    """

    def __init__(self, log_queue):
        super().__init__(log_queue)
        self._drop_lock = threading.Lock()
        self._dropped_pending = 0
        self._dropped_total = 0
        self._dropped_occurrences_pending = 0
        self._dropped_occurrences_total = 0

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except _queue_mod.Full:
            try:
                occurrences = max(
                    1, int(getattr(record, 'tofu_occurrence_delta', 1) or 1))
            except (TypeError, ValueError, OverflowError):
                occurrences = 1
            with self._drop_lock:
                self._dropped_pending += 1
                self._dropped_total += 1
                self._dropped_occurrences_pending += occurrences
                self._dropped_occurrences_total += occurrences
            return

        with self._drop_lock:
            dropped = self._dropped_pending
            dropped_total = self._dropped_total
            dropped_occurrences = self._dropped_occurrences_pending
            dropped_occurrences_total = self._dropped_occurrences_total
            if not dropped:
                return
            # Claim this pending batch while holding the lock. Without the
            # claim, two simultaneous recovery writers can both snapshot and
            # publish the same shed count, making incident/aggregate totals lie.
            self._dropped_pending = 0
            self._dropped_occurrences_pending = 0
        notice = logging.LogRecord(
            name='server.logging', level=logging.WARNING,
            pathname=__file__, lineno=0,
            msg=('Async log queue recovered; shed %d physical record(s) / %d '
                 'occurrence(s) while full (capacity=%d, total_records=%d, '
                 'total_occurrences=%d).'),
            args=(dropped, dropped_occurrences, self.queue.maxsize,
                  dropped_total, dropped_occurrences_total), exc_info=None,
        )
        notice.tofu_occurrence_delta = max(1, dropped_occurrences)
        notice.tofu_window_count = max(1, dropped_occurrences_total)
        notice.tofu_no_coalesce = True
        notice.tofu_event_name = 'logging.queue_shed'
        notice.tofu_event_fields = {
            'dropped_records': dropped,
            'dropped_occurrences': dropped_occurrences,
            'capacity': self.queue.maxsize,
            'total_dropped_records': dropped_total,
            'total_dropped_occurrences': dropped_occurrences_total,
        }
        try:
            self.queue.put_nowait(self.prepare(notice))
        except _queue_mod.Full:
            # Another producer consumed the last slot. Restore only the
            # unreported batch; drops accumulated after our claim remain and
            # combine with it for the next successful recovery checkpoint.
            with self._drop_lock:
                self._dropped_pending += dropped
                self._dropped_occurrences_pending += dropped_occurrences
            return


class _BoundedQueueListener(QueueListener):
    """QueueListener whose shutdown is bounded even if a sink is wedged."""

    def stop(self, timeout=5.0):
        thread = self._thread
        if thread is None:
            return True
        try:
            wait_s = max(0.0, float(timeout))
        except (TypeError, ValueError):
            wait_s = 5.0
        try:
            self.queue.put(self._sentinel, timeout=min(1.0, wait_s))
        except _queue_mod.Full:
            # At shutdown an undrainable queue is already lossy. Free exactly
            # one slot so the daemon listener can observe the sentinel.
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except _queue_mod.Empty:
                pass
            try:
                self.queue.put_nowait(self._sentinel)
            except _queue_mod.Full:
                return False
        thread.join(wait_s)
        if thread.is_alive():
            return False
        self._thread = None
        return True


def build_logging_runtime(*, log_dir, under_pytest, log_agg_enabled,
                          log_agg_store):
    """Build the production/pytest logging wiring and return its runtime parts.

    Returns ``(formatter, real_log_handlers, log_queue, queue_handler,
    coalescing_filter, listener)``. Under pytest the queue/listener are
    ``None`` and logging stays synchronous; in production a bounded
    QueueHandler + QueueListener decouple request-thread logging from slow
    FUSE/NFS file I/O.
    """
    formatter = RedactingFormatter(_LOG_FMT, datefmt=_LOG_DATEFMT)

    app_handler = _SizeAndTimeRotatingFileHandler(
        os.path.join(log_dir, 'app.log'),
        when='midnight', backupCount=stream_backup_count('app'), encoding='utf-8',
        max_bytes=stream_max_bytes('app'),
        total_budget_bytes=stream_family_budget_bytes('app'))
    app_handler.setFormatter(formatter)
    app_handler.setLevel(logging.INFO)
    app_handler.addFilter(_BizOnly())

    access_handler = _SizeAndTimeRotatingFileHandler(
        os.path.join(log_dir, 'access.log'),
        when='midnight', backupCount=stream_backup_count('access'), encoding='utf-8',
        max_bytes=stream_max_bytes('access'),
        total_budget_bytes=stream_family_budget_bytes('access'))
    access_handler.setFormatter(formatter)
    access_handler.setLevel(logging.INFO)
    access_handler.addFilter(_AccessOnly())

    error_handler = _PrivateRotatingFileHandler(
        os.path.join(log_dir, 'error.log'),
        maxBytes=stream_max_bytes('error'),
        backupCount=stream_backup_count('error'), encoding='utf-8')
    error_handler.setFormatter(formatter)
    error_handler.setLevel(logging.WARNING)
    error_handler.addFilter(_BizAndServerOnly())

    vendor_handler = _PrivateRotatingFileHandler(
        os.path.join(log_dir, 'vendor.log'),
        maxBytes=stream_max_bytes('vendor'),
        backupCount=stream_backup_count('vendor'), encoding='utf-8')
    vendor_handler.setFormatter(formatter)
    vendor_handler.setLevel(logging.WARNING)
    vendor_handler.addFilter(_VendorOnly())

    frontend_handler = _SizeAndTimeRotatingFileHandler(
        os.path.join(log_dir, 'frontend.log'),
        when='midnight', backupCount=stream_backup_count('frontend'), encoding='utf-8',
        max_bytes=stream_max_bytes('frontend'),
        total_budget_bytes=stream_family_budget_bytes('frontend'))
    frontend_handler.setFormatter(formatter)
    frontend_handler.setLevel(logging.INFO)
    frontend_handler.addFilter(_FrontendOnly())

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    # A managed worker already has durable file sinks; its stderr is redirected
    # to server-console.log. Mirroring every warning there duplicated hundreds
    # of MB without adding evidence. Keep stderr available for direct
    # writes/fatal hooks, but do not duplicate normal logging records in
    # managed mode.
    console_handler.setLevel(
        logging.CRITICAL + 1 if os.environ.get('TOFU_SERVER_WORKER') == '1'
        else logging.WARNING)
    console_handler.addFilter(_BizAndServerOnly())

    incident_handler = IncidentJournalHandler(
        os.path.join(log_dir, STREAM_POLICIES['incident'].filename))

    # ── Log fingerprint aggregation ──
    # error.log 的加速层:文本文件保留带 occurrence delta 的人类证据,本 handler 只把每条
    # WARNING+ 记录归一成 (level, logger, 消息模板, 异常签名) 指纹在内存计数,
    # 后台 daemon 在有积压时每 ~15s 批量 upsert,空闲时仅按小时做 TTL——DB
    # 失败只丢聚合、fail-open。与 error.log 共用 _BizAndServerOnly 过滤器,聚合覆盖面
    # 恒等于 error.log。挂在 QueueListener 线程上(_real_log_handlers),归一化
    # CPU 不占请求线程。TOFU_LOG_AGGREGATES=0 全关。
    log_agg_handler = FingerprintHandler(log_agg_store())
    log_agg_handler.setLevel(logging.WARNING)
    log_agg_handler.addFilter(_BizAndServerOnly())

    # ── 模块行数预算(LLM 信号层的防洪保险,lib/log_signals.py) ──
    app_handler.addFilter(LogBudgetFilter())

    real_log_handlers = [app_handler, access_handler, error_handler,
                         vendor_handler, frontend_handler, console_handler]

    if log_agg_enabled() and not under_pytest:
        # pytest 同步模式下不挂:测试进程里聚合属噪音(测试会自建实例);
        # 生产下它跑在 QueueListener 的 drain 线程上,emit 只做内存计数。
        real_log_handlers.append(log_agg_handler)

    if not under_pytest:
        # Storage-independent WARNING+ index. It remains queryable when the DB
        # or storage sidecar is the very component being debugged.
        real_log_handlers.append(incident_handler)

    log_queue = None
    queue_handler = None
    coalescing_filter = None
    listener = None

    if under_pytest:
        # No producer-side QueueHandler exists in synchronous test mode, so
        # stamp context at each actual sink. The matching route filter runs
        # first and avoids work for records the sink does not own.
        for handler in real_log_handlers:
            handler.addFilter(LogContextFilter())
        logging.basicConfig(
            level=logging.INFO,
            handlers=list(real_log_handlers),
        )
    else:
        # SINGLE QueueHandler on the root logger. Its emit() is just a
        # non-blocking bounded-queue put — it never touches the disk or the
        # per-handler locks. A dedicated background thread (QueueListener)
        # drains the queue and performs the actual file/stderr I/O.
        log_queue = _queue_mod.Queue(maxsize=_log_queue_capacity())
        queue_handler = _BoundedQueueHandler(log_queue)
        queue_handler.addFilter(LogContextFilter())
        coalescing_filter = DuplicateCoalescingFilter()
        queue_handler.addFilter(coalescing_filter)
        # CRITICAL: give the QueueHandler an explicit ``%(message)s`` formatter
        # so basicConfig() does NOT attach its default BASIC_FORMAT
        # (``LEVEL:name:message``) to it. QueueHandler.prepare() renders its
        # formatter into record.msg before enqueueing; if that were
        # BASIC_FORMAT, each real file handler would then format the
        # ALREADY-formatted string a SECOND time → doubled
        # ``[ERROR] name: ERROR:name:msg`` lines. With ``%(message)s`` the
        # enqueued text is just the rendered message (+ any exc traceback,
        # which Formatter appends and prepare() then clears from exc_info so
        # it isn't duplicated), and the real handlers apply the full
        # timestamp/level/name/thread layout exactly once.
        queue_handler.setFormatter(RedactingFormatter('%(message)s'))
        logging.basicConfig(
            level=logging.INFO,
            handlers=[queue_handler],
        )
        # respect_handler_level=True so each real handler still applies its own
        # setLevel()/filters on the listener thread exactly as before.
        listener = _BoundedQueueListener(
            log_queue, *real_log_handlers, respect_handler_level=True)

    return (formatter, real_log_handlers, log_queue, queue_handler,
            coalescing_filter, listener)
