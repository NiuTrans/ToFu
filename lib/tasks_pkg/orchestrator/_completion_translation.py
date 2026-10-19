"""Shared final prose submission before the terminal translation handoff."""
from lib.log import get_logger

logger=get_logger(__name__)


def submit_final_translation(task,round_num,content):
    try:
        from lib.translate import submit_round_segment
        if not callable(task.get('_invocationEventSink')):
            submit_round_segment(task,round_num,content)
    except Exception as error:
        logger.debug('[%s] incremental translate submit (final) failed: %s',
                     (task.get('id') or '')[:8],error)
