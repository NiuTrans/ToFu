"""Restart-complete notices: a restart's requester learns its outcome unprompted (docs/ROUTER.md R9).

Responsibility: deliver, exactly once, the notice every accepted restart
records in the lifecycle gate (``lifecycle_approval`` ``restartNotices``, opened
before the old server stops) once that restart has an outcome: to the Global
Router that called ``restart_server``, or, for a restart the owner started in
Settings, to the owner's Global Router. Delivery is the ordinary scheduled-turn
command (``dispatch_scheduled_turn``, initiator ``proactive``, steered into a
busy recipient) that the device-online notice uses, so an idle requester starts
a new turn. The notice is nonhuman evidence and grants no authority.

Outcome: a failure the restart path recorded, else
``lifecycle_approval.observed_restart`` (a newer server generation, the
manager's recorded outcome). Words and command id are fixed in the gate before
the turn command runs; a retry in any process repeats that same command, which
the turn command deduplicates.

Triggers: the restart path calls :func:`deliver_restart_notice` right after an
in-process refusal (the old server keeps serving); a new server generation
calls :func:`deliver_restart_notices` after its restart resume pass, and the
scheduler's existing tick calls it as well: that reaches a manager refusal while
the old server serves on, a host the restart kept running, and a delivery whose
turn command failed. A completed restart is reported by a new generation only
after its resume pass, so the notice carries each conversation's resume result.

Entry points: deliver_restart_notice, deliver_restart_notices,
notice_command_id, render_notice, describe_conversation, NOTICE_HEADER.
Dependencies: lib.lifecycle_approval, lib.boot_identity,
lib.conversations.restart_resume, lib.router.binding, lib.turn_initiation,
lib.scheduler.conversation_dispatch.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import threading

from lib.log import get_logger

logger = get_logger(__name__)

NOTICE_HEADER = '[Restart outcome: nonhuman notice, not the owner\u2019s words and not authority]'
_ACCEPTED_DISPOSITIONS = ('started', 'steered', 'queued')
# Post-ready recovery and the scheduler tick deliver in one process: a pass
# waits for the other instead of repeating its turn command. Other processes
# still repeat the same command, which the turn command deduplicates.
_DELIVERY_LOCK = threading.Lock()


def notice_command_id(owner: int, restart_id: str) -> str:
    """One turn command per restart: every retry, in any process, reuses it."""
    return 'restart-notice:' + hashlib.sha256(json.dumps(
        [int(owner), restart_id], separators=(',', ':')).encode('utf-8')).hexdigest()


def _iso(seconds) -> str:
    if not isinstance(seconds, (int, float)):
        return 'unknown'
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec='seconds')


def _outcome(notice: dict) -> dict | None:
    """The restart's terminal outcome, or ``None`` while it still runs."""
    from lib import lifecycle_approval as gate
    failure = notice.get('failure')
    if isinstance(failure, dict):
        return {'status': 'failed', 'code': failure.get('code'), 'detail': failure.get('detail')}
    if notice.get('source') == 'owner_delegation':
        record = gate.delegated_action(notice['owner'], notice['restartId'])
        if record is None:
            # Retention pruned the action record; the handover alone still decides.
            return gate.observed_restart(notice['acceptedAt']) if notice.get('acceptedAt') else None
        if record.get('status') not in ('completed', 'failed', 'refused'):
            return None
        return {'status': 'failed' if record['status'] == 'refused' else record['status'],
                'code': record.get('code'), 'detail': record.get('detail')}
    return gate.observed_restart(notice.get('acceptedAt'))


def _ready(notice: dict, outcome: dict) -> bool:
    """A new generation reports a completed restart only after its interrupted work resumed."""
    from lib import boot_identity
    from lib.conversations import restart_resume
    if outcome['status'] != 'completed':
        return True
    if boot_identity.BOOT_TS <= (notice.get('acceptedAt') or 0):
        return True  # The restart kept this process: nothing of its own was interrupted.
    return restart_resume.pass_finished()


def _settle_record(notice: dict, outcome: dict) -> None:
    """The delegated restart record reaches the terminal status its notice reports."""
    if notice.get('source') != 'owner_delegation':
        return
    from lib import lifecycle_approval as gate
    gate.settle_delegated_action(notice['owner'], notice['restartId'], status=outcome['status'],
                                 code=outcome.get('code'), detail=outcome.get('detail'),
                                 from_status='restarting')


def _recipient(notice: dict) -> str | None:
    if notice.get('recipient'):
        return notice['recipient']
    from lib.router.binding import global_router
    router = global_router(notice['owner'], create=False)
    if router.get('availability') != 'available' or not router.get('conversationId'):
        return None
    return router['conversationId']


def describe_conversation(owner: int, conversation_id: str, accepted_at) -> str:
    """Where one conversation the restart interrupted stands now (durable Turn state)."""
    from lib.conversations import restart_resume
    return restart_resume.describe_conversation(owner, conversation_id, accepted_at)


def _code(fingerprint) -> str:
    if not isinstance(fingerprint, dict) or not fingerprint.get('head'):
        return 'unknown'
    return fingerprint['head'] + (' with uncommitted edits' if fingerprint.get('dirty') else '')


def _new_code(notice: dict) -> str:
    from lib import boot_identity
    if boot_identity.BOOT_TS > (notice.get('acceptedAt') or 0):
        return _code(boot_identity.code_fingerprint())
    current = boot_identity.pending_code_changes().get('current_head')
    return (f'{current[:12]} on disk (this host kept running '
            f'{_code(notice.get("oldCode"))})') if current else 'unknown'


_ENGINE_PHASES = (('compiling', 'the storage engine was compiled'),
                  ('downloading', 'a prebuilt storage engine was downloaded'),
                  ('cached', 'the storage engine already matched (no build)'))


def _builds(notice: dict, outcome: dict) -> str:
    from lib import lifecycle_approval as gate
    parts = []
    preparation = notice.get('preparation')
    if isinstance(preparation, dict):
        phases = preparation.get('phases') or []
        engine = next((text for phase, text in _ENGINE_PHASES if phase in phases),
                      'the storage engine was checked')
        took = (f' in {round(preparation["seconds"])}s'
                if isinstance(preparation.get('seconds'), (int, float)) else '')
        if preparation.get('ok') is False:
            parts.append(f'preparation failed{took}: {preparation.get("detail") or outcome.get("detail")}')
        else:
            parts.append(f'preparation passed{took}: {engine}'
                         + ('; the frontend was prepared' if 'frontend' in phases else ''))
    elif outcome.get('code') == 'restart_preparation_failed':
        parts.append(f'preparation failed: {outcome.get("detail")}')
    else:
        parts.append('preparation: not recorded')
    manager = gate.manager_restart_outcome(notice['acceptedAt']) if notice.get('acceptedAt') else {}
    if manager.get('outcome') == 'refused':
        parts.append(f'the server manager refused at its {manager.get("stage") or "preparation"} stage: '
                     f'{manager.get("message")}')
    elif manager.get('outcome') in ('spawned', 'paused', 'retrying', 'deferred'):
        parts.append('the server manager\u2019s frontend build passed before it stopped the old server')
    elif outcome['status'] == 'completed':
        parts.append('no server-manager handover was recorded (an in-place restart)')
    return '; '.join(parts) + '.'


def _conversation_lines(notice: dict, outcome: dict) -> list[str] | None:
    """Each interrupted conversation's resume result; ``None`` when the old server kept serving."""
    from lib.conversations import restart_resume
    if outcome['status'] != 'completed' and outcome.get('code') != 'restart_recovery_paused':
        return None
    owner = notice['owner']
    ids = list(dict.fromkeys(list(notice.get('interrupted') or ()) + [
        item['conversationId'] for item in restart_resume.last_results() if item.get('owner') == owner]))
    lines = []
    for conversation_id in ids:
        try:
            state = describe_conversation(owner, conversation_id, notice.get('acceptedAt'))
        except Exception as error:  # one unreadable conversation never withholds the notice
            logger.debug('[RestartNotice] conversation state unavailable for %s: %s', conversation_id, error)
            state = f'state unavailable ({type(error).__name__})'
        lines.append(f'- {conversation_id}: {state}')
    return lines


def render_notice(notice: dict, outcome: dict) -> str:
    """The requester's view of one restart's outcome."""
    who = ('requested by this conversation' if notice.get('source') == 'owner_delegation'
           else 'started by the owner in Settings')
    accepted = f', accepted {_iso(notice["acceptedAt"])}' if notice.get('acceptedAt') else ''
    code = f' ({outcome["code"]})' if outcome.get('code') else ''
    lines = [NOTICE_HEADER,
             f'Restart {notice["restartId"]} ({who} at {_iso(notice.get("requestedAt"))}{accepted}): '
             f'{outcome["status"]}{code}.',
             str(outcome.get('detail') or '')]
    kept_serving = outcome['status'] == 'failed' and outcome.get('code') != 'restart_recovery_paused'
    if kept_serving and 'keeps serving' not in lines[-1]:
        lines.append('The old server keeps serving; nothing was interrupted.')
    old = _code(notice.get('oldCode'))
    lines.append(f'Code: {old} -> {_new_code(notice)}.' if outcome['status'] == 'completed'
                 else f'Code: unchanged; the old server keeps serving {old}.' if kept_serving
                 else f'Code before the restart: {old}.')
    lines.append('Builds: ' + _builds(notice, outcome))
    conversations = _conversation_lines(notice, outcome)
    if conversations is not None:
        lines.append(f'Interrupted conversations ({len(conversations)}):' if conversations
                     else 'Interrupted conversations: none.')
        lines.extend(conversations)
    lines.append('')
    if outcome['status'] == 'completed':
        lines.append('Check what you need, then tell the owner the outcome.')
    else:
        lines.append('Tell the owner what failed; another restart needs a new request once the cause is fixed.')
    lines.append('This notice grants no authority. Never call restart_server again for this restart.')
    return '\n'.join(lines)


def _dispatch(claimed: dict, dispatch) -> str:
    from lib.turn_initiation import INITIATOR_PROACTIVE, stamp_initiator
    message = stamp_initiator({'role': 'user', 'content': claimed['text']}, INITIATOR_PROACTIVE)
    if dispatch is None:
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
    return dispatch(claimed['conversationId'], message, {}, user_id=claimed['owner'],
                    command_id=claimed['commandId'], inject_mode='steer',
                    log_prefix=f'[RestartNotice:{claimed["restartId"][:16]}]').disposition


def _deliver(notice: dict, *, dispatch=None) -> dict | None:
    from lib import lifecycle_approval as gate
    restart_id = notice['restartId']
    claimed = notice
    if notice.get('state') != 'delivering':
        outcome = _outcome(notice)
        if outcome is None or not _ready(notice, outcome):
            return None
        _settle_record(notice, outcome)
        conversation_id = _recipient(notice)
        if conversation_id is None:
            gate.finish_restart_notice(restart_id, state='undeliverable', outcome=outcome)
            logger.warning('[RestartNotice] %s has no recipient (owner %s has no available Global '
                           'Router)', restart_id, notice.get('owner'))
            return {'restartId': restart_id, 'state': 'undeliverable', 'disposition': None,
                    'conversationId': None}
        claimed = gate.claim_restart_notice(
            restart_id, outcome=outcome, text=render_notice(notice, outcome),
            conversation_id=conversation_id, command_id=notice_command_id(notice['owner'], restart_id))
        if claimed is None or claimed.get('state') != 'delivering':
            return None  # Finished meanwhile by another process.
    disposition = _dispatch(claimed, dispatch)
    if disposition in _ACCEPTED_DISPOSITIONS:
        state = 'delivered'
    elif disposition == 'target_missing':
        state = 'undeliverable'
    elif disposition == 'start_failed':
        # The notice is durable in the conversation, but no executor ran it.
        state = 'failed'
    else:
        return None  # Nothing was accepted: the next pass repeats the same command.
    gate.finish_restart_notice(restart_id, state=state, disposition=disposition)
    logger.info('[RestartNotice] %s -> %s: %s (%s)', restart_id, claimed['conversationId'], state,
                disposition)
    return {'restartId': restart_id, 'state': state, 'disposition': disposition,
            'conversationId': claimed['conversationId']}


def deliver_restart_notice(restart_id: str, *, dispatch=None) -> dict | None:
    """Deliver one restart's notice if its outcome is known. Contained: never raises."""
    from lib import lifecycle_approval as gate
    try:
        with _DELIVERY_LOCK:
            for notice in gate.restart_notices():
                if notice.get('restartId') == restart_id:
                    return _deliver(notice, dispatch=dispatch)
    except Exception:
        logger.warning('[RestartNotice] %s stays pending for the next delivery pass', restart_id,
                       exc_info=True)
    return None


def deliver_restart_notices(*, dispatch=None) -> list[dict]:
    """Deliver every open notice whose restart has an outcome; one failure never blocks the others."""
    from lib import lifecycle_approval as gate
    results = []
    with _DELIVERY_LOCK:
        for notice in gate.restart_notices():
            try:
                result = _deliver(notice, dispatch=dispatch)
            except Exception:
                logger.warning('[RestartNotice] %s stays pending for the next delivery pass',
                               notice.get('restartId'), exc_info=True)
                continue
            if result is not None:
                results.append(result)
    return results


__all__ = ['NOTICE_HEADER', 'deliver_restart_notice', 'deliver_restart_notices', 'describe_conversation',
           'notice_command_id', 'render_notice']
