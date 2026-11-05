"""Publish a prepared report under the shared task-completion claim.

Entry point: ``publish_report`` returns the confirmed result and terminal
presentation fields. The worker supplies repository, title, runtime and event
ports; TaskRuntime owns cancellation, execution settlement and terminal status.
No model/tool work or independent task lifecycle belongs in this phase.
"""

import uuid

from lib.agent_core.task_runtime import TaskCompletion
from lib.log import get_logger
from lib.paper.artifact_repository import PaperReport


logger = get_logger(__name__)


def publish_report(task, report: PaperReport, *, prepared_body, images,
                   card_title, repository_factory, backfill_title,
                   runtime, append_event) -> TaskCompletion:
    """Confirm durable publication before title repair or visible completion."""
    user_id = int(task['_userId'])
    request_policy = report.meta.get('requestPolicyV1') or {}
    cache_isolated = request_policy.get('cacheMode') == 'request_local'
    if not cache_isolated:
        saved = repository_factory(user_id).put_report(
            report, command_id=f'paper.report.upsert:{uuid.uuid4().hex}')
        if not saved:
            raise RuntimeError('Paper report repository did not confirm persistence')
        logger.info(
            '[Paper:Report] Persisted — hash=%s lang=%s %d chars (%d imgs) '
            'model=%s cost=%s', report.paper_hash, report.lang, len(report.report),
            len(images), report.model, report.meta.get('costCny'))
    else:
        logger.info(
            '[Paper:Report] Request-local policy — canonical cache write '
            'suppressed hash=%s policy=%s', report.paper_hash,
            request_policy.get('executionFingerprint', '')[:12])

    # Repair only placeholder titles after the canonical write is confirmed;
    # request-local reports must not mutate the owner's canonical library.
    resolved_title = ''
    if card_title and not cache_isolated:
        try:
            resolved_title = backfill_title(
                report.paper_hash, card_title, user_id=user_id)
        except Exception as exc:
            logger.warning('[Paper:Report] Title backfill failed for hash=%s: %s',
                           report.paper_hash, exc)
    runtime.update_fields(
        task['task_id'], fields={'resolved_title': resolved_title},
        only_if_status='running')

    enriched = task['enriched_text']
    if enriched and enriched != prepared_body:
        append_event(task, {
            'type': 'enriched', 'text': enriched, 'paperHash': report.paper_hash,
        })
    return TaskCompletion(
        report.report,
        {'type': 'done', 'report': report.report, 'paperHash': report.paper_hash,
         'meta': report.meta, 'resolvedTitle': resolved_title},
    )
