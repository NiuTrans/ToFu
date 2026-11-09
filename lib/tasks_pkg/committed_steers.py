"""Steers committed to a live Turn reach its executor in any server process.

The personal deployment runs an API process (browser commands and the Global
Router replies they start) beside an execution process (channel ingestion,
relays, queue drains and project continuations); a conversation's Turn runs in
whichever process started it. ``lib.agent_inbox`` is process-local, so a steer
accepted by one process for an attempt executing in the other used to wait in
an inbox nobody drained (docs/PROJECT_COORDINATION.md, "Cross-process steer
delivery"). The record ``turn.steer.commit`` stores on the live Turn before any
wakeup (``_userSteerInjects``, ``deliveryState: pending``) is the delivery
authority across processes; the local inbox item stays the same-process fast
path.

Entry points: ``executes_attempt_here`` (acceptance publishes a local wakeup
only for a local executor), ``committed_steers`` (steers committed to this
executor's live Turn that it has not taken: await_project_workers and the task
finalizer), ``take_steers`` (round boundary: commit order, each block once),
``requeue_committed_steer`` (a steer that missed the last boundary becomes the
next turn).
Dependencies: turn_lifecycle (owned Turn reads, the executor's last written
view), tasks_pkg.manager (local registry, task owner), tasks_pkg.
conversation_delivery, scheduler.conversation_dispatch (requeue),
turn_initiation.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import time
from typing import Any

from lib.log import get_logger
from lib.turn_initiation import STEER_BLOCK_PREFIX

logger = get_logger(__name__)

# Both storage authorities store a steer record's preview as ``text[:1200]``
# (SQL ``_turn_steer_commit``; native ``turn/recovery_index.rs``
# ``pending_steer_record``), the bound of the inbox delivery chips. A shorter
# preview is the complete steered text.
INJECTION_PREVIEW_CHARS = 1200
# Marks an inbox-shaped item built from a committed record rather than drained
# from this process's inbox; it carries no accepted config of its own.
COMMITTED_STEER = '_committedSteer'
_VIEW_KEY = '_committedSteerView'
_PARTIAL_NOTE = ('\n[Steered from another server process: only the stored preview of this '
                 'nonhuman text reached you, so it may be cut off.]')


def executes_attempt_here(attempt_id: str, *, user_id: Any) -> bool:
    """Whether this process's task registry holds the executor of ``attempt_id``."""
    from lib.identity import require_user_id
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    owner = require_user_id(user_id, context='steer executor lookup')
    return any(task.get('_attemptId') == attempt_id
               for task in chat_task_runtime.snapshot_owned(user_id=owner))


def _live_turn_records(task: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Committed steer records of the Turn this task executes, or None.

    One metadata read per call. The projection is read only when a writer other
    than this executor changed the Turn since the last view; the executor's own
    writes leave its written view current.
    """
    from lib.tasks_pkg.conversation_delivery import completion_conversation_id
    conversation_id = completion_conversation_id(task)
    turn_id, attempt_id = task.get('_turnId'), task.get('_attemptId')
    if not conversation_id or not turn_id or not attempt_id:
        return None
    from lib.tasks_pkg.manager import task_user_id
    from lib.turn_lifecycle import executor_written_steers, get_turn
    owner = task_user_id(task)
    head = get_turn(conversation_id, turn_id, user_id=owner, include_projection=False)
    if not isinstance(head, Mapping) or head.get('currentAttemptId') != attempt_id:
        return None
    revision = head.get('projectionRevision')
    view = task.get(_VIEW_KEY)
    if isinstance(view, tuple) and view[0] == revision:
        return view[1]
    written = executor_written_steers(task)
    if written is not None and written[0] == revision:
        records = written[1]
    else:
        turn = get_turn(conversation_id, turn_id, user_id=owner)
        if not isinstance(turn, Mapping) or turn.get('currentAttemptId') != attempt_id:
            return None
        revision = turn.get('projectionRevision')
        records = (turn.get('projection') or {}).get('_userSteerInjects')
    view = (revision, [dict(record) for record in records if isinstance(record, Mapping)]
            if isinstance(records, list) else [])
    task[_VIEW_KEY] = view
    return view[1]


def _taken(task: Mapping[str, Any], records: Iterable[Mapping[str, Any]]) -> set[str]:
    """Blocks this executor already took or settled; a replay never delivers twice."""
    taken = {item.get('blockId') for item in task.get('_steer_inject_pending') or ()
             if isinstance(item, Mapping)}
    taken.update(record.get('blockId') for record in task.get('_userSteerInjects') or ()
                 if isinstance(record, Mapping))
    taken.update(record.get('blockId') for record in records
                 if record.get('deliveryState') in ('delivered', 'queued'))
    taken.discard(None)
    return taken


def _committed_item(record: Mapping[str, Any]) -> dict[str, Any]:
    """The inbox item ``_deliver_while_running`` would have published for ``record``.

    A complete stored preview is the exact steered text (reply quotes and any
    input translation included). Past the preview bound an owner's message
    falls back to its committed authored text; nonhuman text has no other copy
    and says it may be cut off.
    """
    block_id = record['blockId']
    previews = record.get('previews')
    preview = previews[0] if isinstance(previews, list) and previews and isinstance(
        previews[0], Mapping) else {}
    text = preview.get('text') if isinstance(preview.get('text'), str) else ''
    author = record.get('author') if isinstance(record.get('author'), Mapping) else None
    if len(text) >= INJECTION_PREVIEW_CHARS:
        authored = author.get('text') if author else None
        text = authored if isinstance(authored, str) and authored else text + _PARTIAL_NOTE
    message: dict[str, Any] = {'role': 'user', 'content': text}
    images = preview.get('images')
    if isinstance(images, list) and images:
        message['images'] = [dict(image) for image in images if isinstance(image, Mapping)]
    item = {'value': text, 'priority': 'next', 'mode': 'user-steer', 'agent_id': '',
            'enqueued': time.time(),
            'injectionId': str(record.get('commandId') or block_id[len(STEER_BLOCK_PREFIX):]),
            'blockId': block_id, '_user_msg': message, COMMITTED_STEER: True}
    if author is not None:
        item['steerAuthor'] = dict(author)
    return item


def _pending(records: Iterable[Mapping[str, Any]], taken: set[str]) -> list[dict[str, Any]]:
    return [_committed_item(record) for record in records
            if record.get('deliveryState') == 'pending'
            and isinstance(record.get('blockId'), str)
            and record['blockId'].startswith(STEER_BLOCK_PREFIX)
            and record['blockId'] not in taken]


def committed_steers(task: dict[str, Any], *, taken_elsewhere: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Steers committed to this executor's live Turn that it has not taken, in commit order.

    Observation only: the round boundary (``take_steers``) or the finalizer
    takes them. ``taken_elsewhere`` names blocks the caller already holds.
    """
    records = _live_turn_records(task)
    if not records:
        return []
    return _pending(records, _taken(task, records) | set(taken_elsewhere))


def take_steers(task: dict[str, Any], local_items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The steers this round boundary injects: commit order, each block once.

    ``local_items`` were just drained from this process's inbox. Steers another
    process committed to this executor's live Turn join them, and a block this
    executor already took (a replayed steer command) is dropped. When the Turn
    cannot be read the drained items are delivered as before.
    """
    local_items = list(local_items)
    try:
        records = _live_turn_records(task) or []
    except Exception as error:
        logger.warning('[Steer] committed steers unreadable task=%s; delivering local steers only: %s',
                       str(task.get('id') or ''), error)
        records = []
    taken = _taken(task, records)
    chosen, blocks, dropped = [], set(), []
    for item in local_items + _pending(records, taken):
        block_id = item.get('blockId')
        if isinstance(block_id, str) and block_id:
            if block_id in taken or block_id in blocks:
                dropped.append(block_id)
                continue
            blocks.add(block_id)
        chosen.append(item)
    order = {record.get('blockId'): index for index, record in enumerate(records)}
    chosen.sort(key=lambda item: order.get(item.get('blockId'), len(order)))
    adopted = [item['blockId'] for item in chosen if item.get(COMMITTED_STEER)]
    if adopted or dropped:
        logger.info('[Steer] conv=%s attempt=%s delivering blocks=%s (adopted from another process=%s, '
                    'already taken=%s)', task.get('convId'), task.get('_attemptId'),
                    [item.get('blockId') for item in chosen], adopted, dropped)
    return chosen


def requeue_committed_steer(task: dict[str, Any], item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Queue a committed steer that missed this executor's last boundary as the next turn.

    The owner's words keep their committed author (actor, text, origin), so a
    forwarded request is verified again when it authorizes; a steer without an
    author was nonhuman and stays nonhuman. The steer's images come along as
    the next Turn's images. Returns the ``queued`` record for the Turn's
    injection lane, or None when nothing was accepted.
    """
    from lib.agent_inbox._steer_preview import steer_model_images
    from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn
    from lib.tasks_pkg.manager import task_user_id
    from lib.turn_initiation import INITIATOR_PEER, stamp_initiator
    author = item.get('steerAuthor')
    if isinstance(author, Mapping):
        message = {'role': 'user', 'content': author['text']}
        if isinstance(author.get('origin'), Mapping):
            message['origin'] = dict(author['origin'])
        actor = str(author.get('actor') or 'human')
    else:
        message = stamp_initiator({'role': 'user', 'content': item['value']}, INITIATOR_PEER)
        actor = 'human'
    images = steer_model_images(item)
    if images:
        message['images'] = images
    outcome = dispatch_scheduled_turn(
        task['convId'], message, {}, user_id=task_user_id(task),
        command_id='steer-requeue:' + item['blockId'], log_prefix='[Steer]',
        input_actor=actor, inject_mode='queue')
    if not outcome.accepted:
        logger.warning('[Steer] conv=%s block=%s missed its Turn and was not queued (%s)',
                       task.get('convId'), item['blockId'], outcome.disposition)
        return None
    logger.info('[Steer] conv=%s block=%s missed its Turn; %s as the next turn',
                task.get('convId'), item['blockId'], outcome.disposition)
    return {'blockId': item['blockId'], 'deliveryState': 'queued',
            **({'queueId': outcome.queue_id} if outcome.queue_id else {})}


__all__ = ['COMMITTED_STEER', 'INJECTION_PREVIEW_CHARS', 'committed_steers',
           'executes_attempt_here', 'requeue_committed_steer', 'take_steers']
