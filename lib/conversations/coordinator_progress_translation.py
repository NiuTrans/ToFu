"""Display translation of the Coordinator task board.

Responsibility: while the human reads a Coordinator conversation translated
(its auto-translate setting), show the task board pinned above it in that
language too, just like the replies. The Coordinator keeps writing the board
in its working language; translations are reconstructible display data
attached to the same head by ``CoordinatorTaskProgress``, never evidence.

Entry points: ``progress_translation_target(task, scope)`` resolves the
reading language of the publishing turn's Coordinator (None without
auto-translate); ``schedule_progress_translation(owner, project, scope,
target)`` queues one optional job on the shared translation lane after a
board publish.

Dependencies: ``lib.conv_config`` (the auto-translate owners), the
conversation repository (stored settings), ``lib.project_dispatch.admission``
(continuation identity), ``lib.translate.execution`` and
``lib.translate.display_texts`` (lane and engine), and
``coordinator_progress`` (the head). Nothing here is durable: a dropped job
(restart, refused admission, translator failure) leaves the board as written
until the next publish schedules another.
"""
from __future__ import annotations

import uuid

from lib.log import get_logger

logger = get_logger(__name__)


def progress_translation_target(task, scope):
    """The language name the human reads ``scope``'s Coordinator in, or None.

    The turn config wins, then the conversation's stored settings. A
    project-dispatch continuation's config pins ``autoTranslate`` off for its
    synthetic input and carries no ``uiLang`` (``TrustedDispatch.capture``), so
    it says nothing about the human's choice and only the settings decide.
    """
    from lib.conv_config import resolve_auto_translate, resolve_translate_target
    from lib.project_dispatch.admission import is_dispatch_task

    config = {} if is_dispatch_task(task) else task.get('config') or {}
    settings = _stored_settings(scope)
    if not resolve_auto_translate(config, settings):
        return None
    return resolve_translate_target(config, settings)


def _stored_settings(scope):
    from lib.conversations.repository import ConversationRepository

    try:
        snapshot = ConversationRepository().get(
            scope['coordinatorId'], user_id=scope['owner'], include_messages=False)
    except Exception:
        # The board is the tool's effect; its translation is optional.
        logger.warning('Coordinator settings unavailable for board translation owner=%s project=%s coordinator=%s',
                       scope['owner'], scope['projectPath'], scope['coordinatorId'], exc_info=True)
        return {}
    return (snapshot.metadata.get('settings') if snapshot else None) or {}


def schedule_progress_translation(owner, project, scope, target):
    """Queue one translation of ``scope``'s board into ``target``; False when not admitted.

    Every call gets its own lane job id: the lane refuses an id that is still
    queued or running, and that job may already have read an older board, so a
    reused id would silently drop the newest texts.
    """
    from lib.translate.execution import submit_reconstructible_translation

    job_id = f"coordinator-progress:{scope['coordinatorId']}:{scope['bindingRevision']}:{uuid.uuid4().hex}"
    try:
        submit_reconstructible_translation(
            job_id, owner_user_id=owner,
            function=lambda: _translate_board(owner, project, scope, target))
    except Exception:
        logger.warning('Coordinator task board translation not admitted owner=%s project=%s coordinator=%s',
                       owner, project, scope['coordinatorId'], exc_info=True)
        return False
    return True


def _translate_board(owner, project, scope, target):
    try:
        from lib.conversations.coordinator_progress import CoordinatorTaskProgress
        from lib.translate.display_texts import translate_display_texts

        progress = CoordinatorTaskProgress(owner, project)
        missing = progress.untranslated_texts(scope, target)
        if not missing:
            return
        translated = translate_display_texts(
            missing, target, log_tag='coordinator-progress:' + scope['coordinatorId'][:8])
        progress.attach_translation(scope, target, translated)
    except Exception:
        logger.warning('Coordinator task board translation failed owner=%s project=%s coordinator=%s target=%s',
                       owner, project, scope['coordinatorId'], target, exc_info=True)


__all__ = ['progress_translation_target', 'schedule_progress_translation']
