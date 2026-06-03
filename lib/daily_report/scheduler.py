"""Daemon scheduler that auto-backfills yesterday's report.

Started once at app boot via ``register_all`` → ``start_report_scheduler``.
Idempotent: a second call is a no-op.
"""

import datetime as _dt
import threading
import time

from lib.log import get_logger

from .conversations import _analyse_conversations, _extract_convs_for_date
from .storage import _load_report, _save_report

logger = get_logger(__name__)

_scheduler_started = False


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

        result = _analyse_conversations(convs, yesterday)
        if result.get('streams') and not result.get('error'):
            _save_report(yesterday, result)
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
    time.sleep(60)
    logger.info('[DailyReport] Scheduler started — checking yesterday')

    while True:
        try:
            _backfill_yesterday_if_missing()
        except Exception as e:
            logger.error('[DailyReport] Scheduler cycle error: %s', e, exc_info=True)
        # Sleep 6 hours between checks
        time.sleep(6 * 3600)


def start_report_scheduler():
    """Start the background scheduler daemon thread.

    Called once from server.py or from blueprint registration.
    Safe to call multiple times — only starts one thread.
    """
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    t = threading.Thread(target=_scheduler_loop, daemon=True,
                         name='daily-report-scheduler')
    t.start()
    logger.info('[DailyReport] Background scheduler thread launched')
