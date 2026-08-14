"""Daemon scheduler that auto-backfills yesterday's report.

Started once at app boot via ``register_all`` → ``start_report_scheduler``.
Idempotent: a second call is a no-op.
"""

import datetime as _dt
import threading

from lib.log import get_logger

from .conversations import _analyse_conversations, _extract_convs_for_date
from .storage import _load_report, _save_generated_report

logger = get_logger(__name__)

_scheduler_thread = None
_scheduler_stop = threading.Event()
_scheduler_lock = threading.Lock()


def _backfill_yesterday_if_missing():
    """Check if yesterday's report exists; if not, generate from DB."""
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    if _load_report(yesterday) is not None:
        logger.debug('[DailyReport] Yesterday %s already has a report', yesterday)
        return

    logger.info('[DailyReport] Auto-backfill for yesterday %s', yesterday)
    try:
        convs = _extract_convs_for_date(yesterday)
        if not convs:
            logger.info('[DailyReport] No conversations found for %s, skipping', yesterday)
            return

        result = _analyse_conversations(
            convs, yesterday, preserve_manual=False)
        if result.get('streams') and not result.get('error'):
            _save_generated_report(yesterday, result)
            logger.info('[DailyReport] Auto-backfill %s: %d streams saved', yesterday,
                        len(result['streams']))
        else:
            logger.warning('[DailyReport] Auto-backfill %s: analysis failed: %s',
                           yesterday, result.get('error', 'unknown'))
    except Exception as e:
        logger.error('[DailyReport] Auto-backfill %s failed: %s',
                     yesterday, e, exc_info=True)


def _scheduler_loop():
    """Background loop: run backfill check at startup and every 6 hours."""
    # Initial delay to let server fully start
    if _scheduler_stop.wait(60):
        return
    logger.info('[DailyReport] Scheduler started — checking yesterday')

    while not _scheduler_stop.is_set():
        try:
            _backfill_yesterday_if_missing()
        except Exception as e:
            logger.error('[DailyReport] Scheduler cycle error: %s', e, exc_info=True)
        finally:
            # The extraction helpers predate pooled leases and may acquire a
            # thread-local connection.  This daemon then sleeps for six hours;
            # return that connection before sleeping rather than reserving a
            # PG slot for the entire process lifetime.
            try:
                from lib.database import close_thread_db
                close_thread_db()
            except Exception as e:
                logger.debug('[DailyReport] connection release failed: %s', e)
        # Sleep 6 hours between checks, interruptibly for clean shutdown.
        if _scheduler_stop.wait(6 * 3600):
            break


def start_report_scheduler() -> bool:
    """Start the background scheduler daemon thread.

    Called once from server.py or from blueprint registration.
    Safe to call multiple times — only starts one thread.
    """
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop, daemon=True,
            name='daily-report-scheduler')
        _scheduler_thread.start()
    logger.info('[DailyReport] Background scheduler thread launched')
    return True


def stop_report_scheduler(timeout: float = 2.0) -> bool:
    """Signal and bounded-join the daily report scheduler."""
    global _scheduler_thread
    _scheduler_stop.set()
    with _scheduler_lock:
        thread = _scheduler_thread
    if thread is None:
        return True
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[DailyReport] invalid stop timeout; using 2.0: %s', exc)
        wait_seconds = 2.0
    if thread is not threading.current_thread():
        thread.join(timeout=wait_seconds)
    if thread.is_alive():
        return False
    with _scheduler_lock:
        if _scheduler_thread is thread:
            _scheduler_thread = None
    return True
