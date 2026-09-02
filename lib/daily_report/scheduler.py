"""Owner-scoped, idempotent backfill service for the durable scheduler.

The recurring cadence is owned by ``lib.scheduler.manager`` and its Sidecar
claim. Legacy ``start/stop_report_scheduler`` imports remain compatible but do
not retain a second six-hour sleeper thread.
"""

import datetime as _dt

from lib.identity import PrincipalContext
from lib.log import get_logger

from .conversations import _analyse_conversations, _extract_convs_for_date
from .storage import _load_report, _save_generated_report

logger = get_logger(__name__)

_MAINTENANCE_SCOPE = 'reports:maintain'


def _scheduler_owner(principal: PrincipalContext) -> int:
    if not isinstance(principal, PrincipalContext):
        raise TypeError('daily report scheduler requires PrincipalContext')
    principal.require_scope(_MAINTENANCE_SCOPE)
    return principal.require_owner(context='daily report scheduler')


def _backfill_yesterday_if_missing(*, principal: PrincipalContext):
    """Generate a missing report and return a scheduler-safe result summary."""
    owner_user_id = _scheduler_owner(principal)
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    existing = _load_report(yesterday, owner_user_id=owner_user_id)
    if existing is not None:
        logger.debug('[DailyReport] Yesterday %s already has a report', yesterday)
        return {
            'ok': True,
            'status': 'already_exists',
            'date': yesterday,
            'streams': len(existing.get('streams') or []),
        }

    logger.info('[DailyReport] Auto-backfill for yesterday %s', yesterday)
    try:
        convs = _extract_convs_for_date(
            yesterday, owner_user_id=owner_user_id)
        if not convs:
            logger.info('[DailyReport] No conversations found for %s, skipping', yesterday)
            return {
                'ok': True,
                'status': 'no_conversations',
                'date': yesterday,
                'streams': 0,
            }

        result = _analyse_conversations(
            convs, yesterday, owner_user_id=owner_user_id,
            preserve_manual=False)
        if result.get('streams') and not result.get('error'):
            _save_generated_report(
                yesterday, result, owner_user_id=owner_user_id)
            logger.info('[DailyReport] Auto-backfill %s: %d streams saved', yesterday,
                        len(result['streams']))
            return {
                'ok': True,
                'status': 'saved',
                'date': yesterday,
                'streams': len(result['streams']),
            }
        else:
            error = str(result.get('error') or 'analysis returned no streams')
            logger.warning('[DailyReport] Auto-backfill %s: analysis failed: %s',
                           yesterday, error)
            return {
                'ok': False,
                'status': 'analysis_failed',
                'date': yesterday,
                'streams': 0,
                'error': error[:300],
            }
    except Exception as e:
        logger.error('[DailyReport] Auto-backfill %s failed: %s',
                     yesterday, e, exc_info=True)
        return {
            'ok': False,
            'status': 'exception',
            'date': yesterday,
            'streams': 0,
            'error': str(e)[:300],
        }


def start_report_scheduler(*, principal: PrincipalContext) -> bool:
    """Compatibility facade that reconciles the durable built-in task."""
    _scheduler_owner(principal)
    from lib.scheduler.manager import ensure_daily_report_schedule
    return ensure_daily_report_schedule(principal=principal)


def stop_report_scheduler(timeout: float = 2.0) -> bool:
    """Thread-free compatibility facade; the main scheduler owns teardown."""
    del timeout
    return True
