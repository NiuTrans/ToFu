"""Conversations a restart interrupted resume from their checkpoint by themselves.

Design authority: docs/RUN_CONTINUITY.md, "Restart-paused conversations resume
by themselves". A server restart settles the Turns it interrupts
``interrupted`` with cause ``server_restart`` (the shutdown quiesce, or boot
recovery for a killed server). After the restart, the next server generation
continues each such main-lane tail Turn that no other owner resumes, through
the ordinary resume command exactly as the owner's Continue click would: the
operation its settlement offers (``continue``, else ``checkpoint_resume``, with
its anchor), the Turn's own accepted execution identity and public config, and
the command id ``restart-resume:<interrupted Attempt>``, so repeated passes,
processes and restarts resume each interruption once and redo no work.

Left alone: Turns other owners resume (dispatched Workers and Coordinator
wakes: project dispatch recovery; Goal and Flow runs: orchestration
continuity), a Turn waiting for the owner's answer (``answer_guidance``), a
Turn that offers only Regenerate, anything the owner stopped (cause
``user_abort``) or moved past (a newer Turn on the lane), a Turn that ended
before the stop, and everything after a manual shutdown or in a restart storm.

Candidates: the conversations each stopping process quiesced (one durable
record per stopping process, written before its tasks settle; namespace
``RESTART_PAUSED_NAMESPACE``), the conversations boot recovery settled for a
killed server, and those an open restart notice lists as interrupted. Every
candidate is validated against its durable Turn before anything starts.

Entry points: record_quiesced_conversations (shutdown quiesce),
set_boot_recovered (boot recovery), resume_restart_paused_turns (post-ready),
resume_conversation, describe_conversation, pass_finished, last_results.
Dependencies: lib.storage (record.*), lib.turn_lifecycle,
lib.conversation_sync (runtime command service, execution identity),
lib.project_dispatch.admission, lib.lifecycle_approval, lib.boot_identity.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import time
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

RESTART_PAUSED_NAMESPACE = 'conversation.restart_paused.v1'
RESUME_OPERATIONS = ('continue', 'checkpoint_resume')
# The storage record.list protocol maximum per page.
_RECORD_LIST_LIMIT = 1000

_PASS_FINISHED = False
_LAST_RESULTS: list[dict] = []
_BOOT_RECOVERED: list[dict] = []


def pass_finished() -> bool:
    """Whether this process ran its post-restart resume pass (restart notices wait for it)."""
    return _PASS_FINISHED


def last_results() -> list[dict]:
    """This process's resume pass results, one per candidate conversation."""
    return deepcopy(_LAST_RESULTS)


def set_boot_recovered(recovery_result) -> None:
    """Hand boot recovery's settled orphans (a killed server's Turns) to the post-ready pass."""
    global _BOOT_RECOVERED
    if isinstance(recovery_result, Mapping):
        _BOOT_RECOVERED = [dict(item) for item in recovery_result.get('recoveredTasks') or ()
                           if isinstance(item, Mapping)]


def _client(write=False):
    from lib.storage import get_storage_client
    return get_storage_client(write=True) if write else get_storage_client()


def record_quiesced_conversations(tasks) -> None:
    """Record the Turns a stopping process quiesced, before they settle. Contained: never raises."""
    from lib import boot_identity
    from lib.identity import require_user_id
    entries = []
    for task in tasks or ():
        try:
            owner = require_user_id(task.get('_userId'), context='restart quiesce record')
        except (TypeError, ValueError):
            continue
        identity = [task.get(name) for name in ('convId', '_turnId', '_attemptId')]
        if all(isinstance(value, str) and value for value in identity):
            entries.append({'owner': owner, 'conversationId': identity[0], 'turnId': identity[1],
                            'attemptId': identity[2]})
    if not entries:
        return
    key = f'{boot_identity.BOOT_ID}:{uuid.uuid4().hex[:12]}'
    try:
        _client(write=True).command('record.put', {
            'namespace': RESTART_PAUSED_NAMESPACE, 'key': key, 'expected_version': 0,
            'value': {'version': 1, 'bootId': boot_identity.BOOT_ID, 'recordedAt': time.time(),
                      'conversations': entries}}, 'restart-paused:' + key)
    except Exception:
        logger.warning('[RestartResume] quiesced conversations were not recorded; boot recovery '
                       'and restart notices still name them', exc_info=True)


def _quiesce_records() -> list[dict]:
    """Records of earlier generations: ``{'key', 'version', 'conversations'}``."""
    from lib import boot_identity
    records, after = [], ''
    while True:
        rows = _client().query('record.list', {
            'namespace': RESTART_PAUSED_NAMESPACE, 'prefix': '', 'after_key': after,
            'limit': _RECORD_LIST_LIMIT})
        rows = rows if isinstance(rows, list) else []
        for row in rows:
            value = row.get('value') if isinstance(row, Mapping) else None
            if (not isinstance(value, Mapping) or value.get('bootId') == boot_identity.BOOT_ID
                    or not isinstance(value.get('conversations'), list)):
                continue
            records.append({'key': row['key'], 'version': row['version'],
                            'conversations': [item for item in value['conversations']
                                              if isinstance(item, Mapping)]})
        if len(rows) < _RECORD_LIST_LIMIT:
            return records
        after = rows[-1]['key']


def _delete_record(record: dict) -> None:
    from lib.storage.errors import StorageError
    try:
        _client(write=True).command('record.delete', {
            'namespace': RESTART_PAUSED_NAMESPACE, 'key': record['key'],
            'expected_version': record['version']}, 'restart-paused-done:' + uuid.uuid4().hex)
    except StorageError as error:
        if error.code != 'database_conflict':
            raise


def _task_owner(task_id: str) -> int | None:
    """The owner of one boot-recovered task, from its durable result record."""
    row = _client().query('record.get', {'namespace': 'task_results', 'key': task_id})
    value = row.get('value') if isinstance(row, Mapping) else None
    owner = value.get('user_id') if isinstance(value, Mapping) else None
    text = str(owner or '')
    return int(text) if text.isdigit() and int(text) > 0 else None


def _candidates(records: list[dict], boot_recovered: list[dict]) -> list[tuple[int, str]]:
    from lib import boot_identity
    from lib import lifecycle_approval as gate
    pairs = [(int(item['owner']), str(item['conversationId'])) for record in records
             for item in record['conversations'] if item.get('owner') and item.get('conversationId')]
    for item in boot_recovered:
        if item.get('taskId') and item.get('conversationId'):
            owner = _task_owner(str(item['taskId']))
            if owner is not None:
                pairs.append((owner, str(item['conversationId'])))
    for notice in gate.restart_notices():
        accepted = notice.get('acceptedAt')
        if isinstance(accepted, (int, float)) and accepted < boot_identity.BOOT_TS:
            pairs.extend((int(notice['owner']), str(conversation))
                         for conversation in notice.get('interrupted') or ())
    return list(dict.fromkeys(pairs))


def _stand_down(previous_shutdown) -> str:
    if not isinstance(previous_shutdown, Mapping):
        return ''
    if previous_shutdown.get('restart_storm'):
        return ('restart storm: automatic resume stands down so a crash loop cannot re-run paid turns; '
                'Continue it when the server is stable')
    if previous_shutdown.get('verdict') == 'clean' and previous_shutdown.get('reason') == 'manual':
        return 'manual shutdown: the owner stopped the server, so nothing resumes by itself'
    return ''


def _result(owner, conversation_id, outcome, reason='', **fields) -> dict:
    return {'owner': owner, 'conversationId': conversation_id, 'outcome': outcome, 'reason': reason,
            'turnId': fields.get('turnId'), 'attemptId': fields.get('attemptId'),
            'operation': fields.get('operation')}


def _lane_tail(owner: int, conversation_id: str) -> dict | None:
    """The newest main-lane Turn's metadata (no projection body)."""
    from lib import turn_lifecycle as turns
    from lib.swarm.restart_recovery import read_recovery_turns
    page = read_recovery_turns(conversation_id, user_id=owner, lane_id='main', limit=1)
    headers = page.get('turns') or []
    if not headers:
        return None
    return turns.get_turn(conversation_id, headers[-1]['turnId'], user_id=owner, include_projection=False)


def resume_conversation(owner: int, conversation_id: str) -> dict:
    """Continue one conversation's restart-paused tail Turn in place, or say why not."""
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.execution_identity import restore_execution_identity
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.project_dispatch.admission import turn_dispatch

    def skip(reason, **fields):
        return _result(owner, conversation_id, 'not_resumed', reason, **fields)

    turn = _lane_tail(owner, conversation_id)
    if turn is None:
        return skip('no Turn on its main lane')
    ids = {'turnId': turn.get('turnId'), 'attemptId': turn.get('currentAttemptId')}
    settlement = turn.get('settlement') or {}
    if turn.get('status') != 'interrupted':
        return skip(f'its latest Turn is {turn.get("status")}, not interrupted', **ids)
    if settlement.get('cause') != 'server_restart':
        return skip(f'interrupted by {settlement.get("cause") or "an unknown cause"}, not by the restart', **ids)
    options = [option for option in settlement.get('resumeOptions') or () if isinstance(option, Mapping)]
    offered = [option.get('operation') for option in options]
    if 'answer_guidance' in offered:
        return skip('waiting for the owner\u2019s answer to its question (answer_guidance)', **ids)
    option = next((option for option in options if option.get('operation') in RESUME_OPERATIONS), None)
    if option is None:
        return skip('only ' + (', '.join(map(str, offered)) or 'nothing')
                    + ' is offered; regenerate would redo the work', **ids)
    attempt = turns.get_attempt(turn['currentAttemptId'], user_id=owner, include_execution_config=True)
    config = attempt.get('config')
    if not isinstance(config, Mapping):
        return skip('its accepted execution config is unavailable', **ids)
    if turn_dispatch(conversation_id, turn, config, user_id=owner) is not None:
        return skip('a project dispatch grant runs it; project dispatch recovery continues it', **ids)
    if config.get('autopilot') is True:
        return skip('a Goal run owns it; Goal continuity resumes it', **ids)
    if any(config.get(key) for key in ('activeFlow', 'flowDefinition', 'flowBuiltin', 'flowId')):
        return skip('a Flow run owns it; Flow runs resume on request (orchestration continuity)', **ids)
    principal, _ = restore_execution_identity({**config, '_turnOwnerUserId': owner})
    body = {'commandId': 'restart-resume:' + turn['currentAttemptId'], 'operation': option['operation'],
            'expectedProjectionRevision': turn['projectionRevision'],
            'config': {name: deepcopy(value) for name, value in config.items() if not name.startswith('_')}}
    if option.get('anchor') is not None:
        # The exact anchor the settlement offers, as a browser Continue sends it.
        body['resumeAnchor'] = deepcopy(option['anchor'])
    result = conversation_turn_commands.create_attempt(
        conversation_id, turn['turnId'], owner, body, request_started_at=time.time(),
        principal=principal).value
    resumed = (result.get('attempt') or {}).get('attemptId')
    logger.info('[RestartResume] %s resumed in place (%s, attempt %s%s)', conversation_id,
                option['operation'], resumed, ', replayed' if result.get('idempotentReplay') else '')
    return _result(owner, conversation_id, 'resumed', turnId=turn['turnId'], attemptId=resumed,
                   operation=option['operation'])


def resume_restart_paused_turns(*, previous_shutdown=None, boot_recovered=None) -> list[dict]:
    """The post-ready pass of a server generation; one result per candidate conversation.

    A transient storage failure keeps that candidate's quiesce record and is
    raised after the other candidates ran, so the boot step's existing retry
    repeats the pass; a repeat replays the same resume commands.
    """
    global _PASS_FINISHED, _LAST_RESULTS
    from lib.storage.errors import StorageError
    retry = None
    try:
        records = _quiesce_records()
        candidates = _candidates(records, _BOOT_RECOVERED if boot_recovered is None else boot_recovered)
        stand_down = _stand_down(previous_shutdown)
        results, unsettled = [], set()
        for owner, conversation_id in candidates:
            if stand_down:
                results.append(_result(owner, conversation_id, 'not_resumed', stand_down))
                continue
            try:
                results.append(resume_conversation(owner, conversation_id))
            except Exception as error:
                if isinstance(error, StorageError) and error.retryable:
                    retry, unsettled = error, unsettled | {(owner, conversation_id)}
                logger.warning('[RestartResume] %s was not resumed: %s', conversation_id, error,
                               exc_info=not isinstance(error, (PermissionError, ValueError, StorageError)))
                results.append(_result(owner, conversation_id, 'not_resumed',
                                       f'resume failed: {type(error).__name__}: {error}'))
        for record in records:
            if not any((int(item['owner']), str(item['conversationId'])) in unsettled
                       for item in record['conversations']):
                _delete_record(record)
        _LAST_RESULTS = results
        if any(item['outcome'] == 'resumed' for item in results) or stand_down:
            logger.warning('[RestartResume] %d of %d interrupted conversation(s) resumed%s',
                           sum(item['outcome'] == 'resumed' for item in results), len(results),
                           f' ({stand_down})' if stand_down else '')
    finally:
        _PASS_FINISHED = True
    if retry is not None:
        raise retry
    return results


def describe_conversation(owner: int, conversation_id: str, since) -> str:
    """Where one conversation stands after a restart accepted at ``since`` (durable Turn state)."""
    from lib import turn_lifecycle as turns
    turn = _lane_tail(owner, conversation_id)
    if turn is None:
        return 'no Turn found'
    status, settlement = turn.get('status'), turn.get('settlement') or {}
    attempt = (turns.get_attempt(turn['currentAttemptId'], user_id=owner)
               if turn.get('currentAttemptId') else {})
    operation = attempt.get('operation')
    after = isinstance(since, (int, float)) and (attempt.get('createdAt') or 0) / 1000 >= since
    resumed = operation in RESUME_OPERATIONS and after
    if status in ('pending', 'running'):
        if resumed:
            return f'resumed from its checkpoint ({operation}), running'
        return 'continuing in a newer Turn, running' if after else 'kept running through the restart'
    if status == 'interrupted' and settlement.get('cause') == 'server_restart':
        reason = next((item['reason'] for item in _LAST_RESULTS
                       if item['conversationId'] == conversation_id and item['outcome'] == 'not_resumed'), '')
        return 'still interrupted, not resumed' + (f': {reason}' if reason else '')
    if resumed:
        return f'resumed from its checkpoint ({operation}), then {status}'
    cause = settlement.get('cause')
    return f'{status} ({cause})' if cause else str(status)


__all__ = ['RESTART_PAUSED_NAMESPACE', 'RESUME_OPERATIONS', 'describe_conversation', 'last_results',
           'pass_finished', 'record_quiesced_conversations', 'resume_conversation',
           'resume_restart_paused_turns', 'set_boot_recovered']
