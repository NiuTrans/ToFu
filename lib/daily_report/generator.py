"""Async report-generation worker.

Spawned by ``routes.daily_report.start_generation`` as a daemon thread.
Updates the shared ``_active_jobs`` registry with progress stages
(``extracting`` → ``analyzing`` → ``saving`` → ``done`` / ``error``)
which the status-poll endpoint surfaces to the frontend.
"""

from lib.log import get_logger
from lib.identity import require_user_id

from .conversations import _analyse_conversations, _extract_convs_for_date
from .storage import _save_generated_report, _update_job

logger = get_logger(__name__)


def _generate_in_background(date_str, force, *, owner_user_id):
    """Background thread: extract convs → LLM analysis → save report.

    Updates ``_active_jobs[date_str]`` with progress stages:
    starting → extracting → analyzing → saving → done (or error).
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report background generation')
    try:
        logger.info('[DailyReport] Background generation started for %s (force=%s)',
                    date_str, force)

        # Phase 1: Extract conversations from DB
        _update_job(date_str, 'generating', owner_user_id=owner_id, progress={
            'stage': 'extracting',
            'message': '正在扫描对话…',
            'current': 0, 'total': 0,
        })

        def _extraction_progress(current, total):
            _update_job(
                date_str, 'generating', owner_user_id=owner_id, progress={
                'stage': 'extracting',
                'message': f'扫描对话 {current}/{total}',
                'current': current, 'total': total,
            })

        convs = _extract_convs_for_date(
            date_str, progress_cb=_extraction_progress,
            owner_user_id=owner_id)

        if not convs:
            # Delegate to the empty-convs path of _analyse_conversations so
            # carryover + manual-state preservation (_merge_manual_state) are
            # applied consistently instead of a bare tasks-only merge here.
            result = _analyse_conversations(
                [], date_str, owner_user_id=owner_id,
                preserve_manual=False)
            if (result.get('streams') or result.get('tomorrow')
                    or result.get('tasks')):
                _save_generated_report(
                    date_str, result, owner_user_id=owner_id)
            _update_job(date_str, 'done', owner_user_id=owner_id)
            logger.info('[DailyReport] Background generation %s: no convs found', date_str)
            return

        # Phase 2: LLM Analysis
        _update_job(date_str, 'generating', owner_user_id=owner_id, progress={
            'stage': 'analyzing',
            'message': f'LLM 分析 {len(convs)} 个对话…',
            'current': 0, 'total': len(convs),
        })

        # Manual-state preservation (status overrides, TODO check-offs,
        # manual TODOs, legacy _todo tasks) is centralized in
        # _analyse_conversations → _merge_manual_state.
        result = _analyse_conversations(
            convs, date_str, owner_user_id=owner_id,
            preserve_manual=False)

        # Phase 3: Save
        _update_job(date_str, 'generating', owner_user_id=owner_id, progress={
            'stage': 'saving', 'message': '保存报告…',
        })

        if (result.get('streams') or result.get('tomorrow')) and not result.get('error'):
            _save_generated_report(
                date_str, result, owner_user_id=owner_id)
        elif result.get('error'):
            logger.warning('[DailyReport] Background generation %s: not saving error result: %s',
                           date_str, result['error'])

        _update_job(date_str, 'done', owner_user_id=owner_id)

        stream_count = len(result.get('streams', []))
        done_count = sum(1 for s in result.get('streams', []) if s.get('status') == 'done')
        logger.info('[DailyReport] Background generation %s completed: %d streams (%d done)',
                    date_str, stream_count, done_count)

    except Exception as e:
        logger.error('[DailyReport] Background generation %s failed: %s',
                     date_str, e, exc_info=True)
        _update_job(
            date_str, 'error', owner_user_id=owner_id, error=str(e))
