"""Quiesced legacy transcript -> authoritative Turn / Attempt migration.

Planning and parity validation are pure.  Applying plans is one database
transaction for the whole maintenance window; any conversation mismatch rolls
the transaction back and leaves the v2 cutover marker unset.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lib.database import DOMAIN_CHAT, pooled_db, pooled_write_transaction
from lib.log import get_logger

_NAMESPACE = uuid.UUID('6991ad18-12c0-5f5c-b695-a97c9809a148')
_LEGAL_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
logger = get_logger(__name__)


def _uuid5(*parts: Any) -> str:
    return str(uuid.uuid5(_NAMESPACE, '\x1f'.join(str(p) for p in parts)))


def _decode(raw: Any, default: Any):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or '')
    except (TypeError, ValueError) as exc:
        logger.debug('[TurnMigration] legacy JSON decode fallback: %s', exc)
        return default


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canon(value).encode()).hexdigest()


def _timestamp_ms(value: Any, fallback: int) -> int:
    if isinstance(value, (int, float)):
        numeric = int(value)
        return numeric * 1000 if 0 < numeric < 10_000_000_000 else numeric
    if isinstance(value, str) and value:
        try:
            return _timestamp_ms(float(value), fallback)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                return int(parsed.timestamp() * 1000)
            except ValueError as exc:
                logger.debug('[TurnMigration] timestamp fallback value=%r: %s',
                             value, exc)
    return int(fallback)


def _actor(message: dict[str, Any]) -> str:
    flow_role = str(message.get('_flowRole') or '').lower()
    if flow_role in {'planner', 'critic', 'virtual_user'}:
        return flow_role
    if message.get('_isEndpointPlanner') or message.get('_endpointRole') == 'planner':
        return 'planner'
    if message.get('_isEndpointReview') or message.get('_endpointRole') == 'critic':
        return 'critic'
    if (message.get('_isVirtualUser') or message.get('_isAutopilotVU')
            or message.get('_endpointRole') == 'virtual_user'):
        return 'virtual_user'
    return 'human' if message.get('role') == 'user' else 'assistant'


def _kind(message: dict[str, Any], actor: str, lane_id: str) -> str:
    if message.get('_flowNodeId') or message.get('_flowRunId'):
        return 'flow_node'
    if actor == 'planner':
        return 'endpoint_planner'
    if actor == 'critic':
        return 'endpoint_critic'
    if message.get('_epIteration') is not None:
        return 'endpoint_worker'
    if actor == 'virtual_user':
        return 'autopilot_virtual_user'
    if message.get('_autopilotRunId'):
        return 'autopilot_reply'
    if lane_id != 'main' or message.get('_branchId'):
        return 'branch'
    return 'input' if actor == 'human' else 'reply'


def _projection(message: dict[str, Any]) -> dict[str, Any]:
    # Marker fields are migration inputs, not v2 projection facts. Preserve
    # user-visible metadata (including unrelated historical underscore fields
    # such as ``_ctx``), but translate orchestration markers into a typed
    # payload so the new UI never has to infer roles from them.
    inference_fields = {
        '_isEndpointPlanner', '_isEndpointReview', '_isVirtualUser',
        '_isAutopilotVU', '_endpointRole', '_epIteration',
        '_epPlannerIteration', '_epApproved', '_epNextPhase', '_flowNodeId',
        '_flowRunId', '_flowRole', '_autopilotRunId', '_branchId',
    }
    projection = {
        key: value for key, value in message.items()
        if key not in ({'role', '_msgId', '_taskId', 'branches', 'activeTaskId'}
                       | inference_fields)
    }
    orchestration = {
        'iteration': (message.get('_epIteration')
                      if message.get('_epIteration') is not None
                      else message.get('_epPlannerIteration')),
        'approved': message.get('_epApproved'),
        'nextPhase': message.get('_epNextPhase'),
        'flowNodeId': message.get('_flowNodeId'),
        'flowRole': message.get('_flowRole'),
    }
    orchestration = {
        key: value for key, value in orchestration.items() if value is not None
    }
    if orchestration:
        projection['orchestration'] = orchestration
    return projection


def _task_status(task: dict[str, Any] | None) -> str | None:
    status = (task or {}).get('status')
    return {
        'done': 'completed', 'completed': 'completed',
        'error': 'failed', 'failed': 'failed',
        'aborted': 'interrupted', 'interrupted': 'interrupted',
    }.get(status)


def _status(message: dict[str, Any], task: dict[str, Any] | None,
            actor: str) -> tuple[str, str, str | None]:
    if actor == 'human':
        return 'completed', 'submitted', None
    if actor == 'virtual_user':
        return 'completed', 'autopilot_generated', None
    finish = message.get('finishReason') or message.get('finish_reason')
    if finish in {'stop', 'completed', 'end_turn', 'tool_complete'}:
        return 'completed', 'provider_finished', finish
    if finish in {'length', 'max_tokens', 'context_length', 'content_filter'}:
        return 'truncated', str(finish), finish
    if finish in {'error', 'failed'} or message.get('error'):
        return 'failed', 'generation_error', finish
    if finish in {'interrupted', 'aborted', 'server_offline', 'killed'}:
        return 'interrupted', str(finish), finish
    aligned = _task_status(task)
    if aligned:
        return aligned, 'task_result', finish
    # Absence is not proof of success. This is the migration's most important
    # conservative rule: an unknown legacy assistant is recoverable, not done.
    return 'interrupted', 'legacy_unknown', finish


def _resume_options(status: str, projection: dict[str, Any]) -> list[dict[str, Any]]:
    if status == 'completed':
        return [{'operation': 'regenerate', 'anchor': {'type': 'turn_start'}}]
    options: list[dict[str, Any]] = []
    rounds = projection.get('toolRounds') or []
    kept = 0
    for item in rounds:
        if isinstance(item, dict) and item.get('status') in {'done', 'completed'}:
            kept += 1
        else:
            break
    if kept:
        last = rounds[kept - 1]
        options.append({
            'operation': 'checkpoint_resume',
            'anchor': {'type': 'tool_checkpoint', 'keptToolRounds': kept,
                       'content': last.get('assistantContent') or '',
                       'thinking': last.get('thinking') or '', 'segments': []},
        })
    options.append({'operation': 'regenerate', 'anchor': {'type': 'turn_start'}})
    return options


@dataclass(frozen=True)
class ConversationPlan:
    conversation_id: str
    user_id: Any
    turns: tuple[dict[str, Any], ...]
    attempts: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    source_top_level_count: int
    source_branch_count: int
    source_hash: str

    def report(self) -> dict[str, Any]:
        lane_counts: dict[str, int] = {}
        for turn in self.turns:
            lane_counts[turn['lane_id']] = lane_counts.get(turn['lane_id'], 0) + 1
        return {
            'conversationId': self.conversation_id,
            'topLevelMessages': self.source_top_level_count,
            'branchMessages': self.source_branch_count,
            'turns': len(self.turns),
            'attempts': len(self.attempts),
            'lanes': lane_counts,
            'sourceHash': self.source_hash,
        }


def plan_conversation(conversation_id: str, messages: list[dict[str, Any]], *,
                      user_id: Any = 1,
                      task_results: dict[str, dict[str, Any]] | None = None,
                      created_at: int = 0,
                      global_id_counts: dict[str, int] | None = None) -> ConversationPlan:
    task_results = task_results or {}
    counts: dict[str, int] = {}
    def count_ids(items):
        for message in items or []:
            mid = message.get('_msgId') if isinstance(message, dict) else None
            if mid:
                counts[str(mid)] = counts.get(str(mid), 0) + 1
            if isinstance(message, dict):
                for branch in message.get('branches') or []:
                    count_ids(branch.get('messages') or [])
    count_ids(messages)
    now = int(created_at or time.time() * 1000)
    turns: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    branch_count = 0

    def append_message(message: dict[str, Any], *, ordinal: int, lane_id: str,
                       parent_turn_id: str | None, original_ordinal: Any):
        raw_mid = str(message.get('_msgId') or '')
        globally_unique = (global_id_counts is None
                           or global_id_counts.get(raw_mid, 0) == 1)
        if (raw_mid and counts.get(raw_mid) == 1 and globally_unique
                and _LEGAL_ID.match(raw_mid)):
            turn_id = raw_mid
        else:
            turn_id = _uuid5(conversation_id, original_ordinal,
                             message.get('role') or '', lane_id)
        actor = _actor(message)
        projection = _projection(message)
        task = task_results.get(str(message.get('_taskId') or ''))
        status, cause, finish = _status(message, task, actor)
        settlement = {
            'outcome': status, 'cause': cause,
            'providerFinishReason': finish,
            'error': (message.get('error') or (task or {}).get('error') or None),
            'resumeOptions': _resume_options(status, projection),
        }
        attempt_id = None
        if actor != 'human':
            attempt_id = _uuid5(conversation_id, turn_id, 'synthetic-attempt')
        turn = {
            'turn_id': turn_id, 'conversation_id': conversation_id,
            'user_id': user_id, 'lane_id': lane_id,
            'parent_turn_id': parent_turn_id, 'ordinal': ordinal,
            'actor': actor, 'kind': _kind(message, actor, lane_id),
            'run_id': str(message.get('_autopilotRunId') or
                          message.get('_flowRunId') or ''),
            'status': status, 'current_attempt_id': attempt_id,
            'projection': projection, 'projection_revision': 1,
            'settlement': settlement,
            'created_at': _timestamp_ms(message.get('timestamp'), now),
            'updated_at': now,
        }
        turns.append(turn)
        if attempt_id:
            attempt = {
                'attempt_id': attempt_id, 'conversation_id': conversation_id,
                'turn_id': turn_id, 'command_id': f'legacy:{turn_id}',
                # One legacy Endpoint/Flow task can own several visible
                # messages. Use it for status alignment above, but do not
                # pretend it is a one-to-one attempt identity.
                'task_id': '',
                'operation': 'generate', 'status': status,
                'base_projection_revision': 0, 'resume_anchor': {},
                'config': {}, 'error': message.get('error') or {},
                'created_at': turn['created_at'], 'started_at': turn['created_at'],
                'settled_at': now, 'superseded_at': None,
            }
            attempts.append(attempt)
            events.append({
                'attempt_id': attempt_id, 'seq': 1,
                'conversation_id': conversation_id, 'turn_id': turn_id,
                'projection_revision': 1, 'type': 'terminal_settlement',
                'payload': {
                    'conversationId': conversation_id, 'turnId': turn_id,
                    'attemptId': attempt_id, 'seq': 1,
                    'projectionRevision': 1, 'type': 'terminal_settlement',
                    'payload': {'status': status, 'settlement': settlement,
                                'projection': projection},
                },
                'created_at': now,
            })
        return turn_id

    previous_main_id = None
    for ordinal, message in enumerate(messages):
        if not isinstance(message, dict):
            message = {'role': 'assistant', 'content': str(message)}
        parent_id = append_message(
            message, ordinal=ordinal, lane_id='main',
            parent_turn_id=previous_main_id,
            original_ordinal=ordinal)
        previous_main_id = parent_id
        for branch_index, branch in enumerate(message.get('branches') or []):
            lane_id = str(branch.get('id') or branch.get('laneId') or
                          _uuid5(conversation_id, ordinal, 'branch', branch_index))
            descriptor = {
                'laneId': lane_id,
                'parentTurnId': parent_id,
                'title': str(branch.get('title') or 'Branch'),
                'icon': str(branch.get('icon') or '⑂'),
                'kind': str(branch.get('kind') or 'branch'),
                'anchorText': str(branch.get('anchorText') or ''),
                'parentSelection': str(branch.get('parentSelection') or ''),
                'createdAt': _timestamp_ms(branch.get('createdAt'), now),
            }
            parent_turn = next(turn for turn in reversed(turns)
                               if turn['turn_id'] == parent_id)
            parent_turn['projection'].setdefault('_branchLanes', []).append(descriptor)
            for event in reversed(events):
                if event['turn_id'] == parent_id:
                    event['payload']['payload']['projection'] = parent_turn['projection']
                    break
            previous_parent = parent_id
            for branch_ordinal, branch_message in enumerate(branch.get('messages') or []):
                previous_parent = append_message(
                    branch_message, ordinal=branch_ordinal, lane_id=lane_id,
                    parent_turn_id=previous_parent,
                    original_ordinal=f'{ordinal}:b{branch_index}:{branch_ordinal}')
                branch_count += 1

    source_projection = [{
        'laneId': turn['lane_id'], 'ordinal': turn['ordinal'],
        'parentTurnId': turn['parent_turn_id'], 'actor': turn['actor'],
        'kind': turn['kind'], 'projection': turn['projection'],
    } for turn in turns]
    return ConversationPlan(
        conversation_id, user_id, tuple(turns), tuple(attempts), tuple(events),
        len(messages), branch_count, _hash(source_projection))


def _task_results_for(db: Any, conversation_id: str) -> dict[str, dict[str, Any]]:
    rows = db.execute(
        'SELECT task_id,status,content,thinking,error,tool_rounds,metadata '
        'FROM task_results WHERE conv_id=?', (conversation_id,),
    ).fetchall()
    result = {}
    for row in rows:
        result[row['task_id']] = {
            'status': row['status'], 'content': row['content'],
            'thinking': row['thinking'], 'error': _decode(row['error'], {}),
            'toolRounds': _decode(row['tool_rounds'], []),
            'metadata': _decode(row['metadata'], {}),
        }
    return result


def plan_database(*, user_id: Any = 1) -> list[ConversationPlan]:
    from lib.database.conversation_repository import iter_conversation_snapshots
    with pooled_db(DOMAIN_CHAT) as db:
        snapshots = list(iter_conversation_snapshots(
                db, user_id=user_id, metadata_columns=('created_at',),
                order_by='id_asc'))
        global_counts: dict[str, int] = {}
        def count_global(items):
            for message in items or []:
                if not isinstance(message, dict):
                    continue
                mid = str(message.get('_msgId') or '')
                if mid:
                    global_counts[mid] = global_counts.get(mid, 0) + 1
                for branch in message.get('branches') or []:
                    count_global(branch.get('messages') or [])
        for snapshot in snapshots:
            count_global(snapshot.messages)
        plans = []
        for snapshot in snapshots:
            plans.append(plan_conversation(
                snapshot['id'], snapshot.messages, user_id=user_id,
                task_results=_task_results_for(db, snapshot['id']),
                created_at=int(snapshot.get('created_at') or 0),
                global_id_counts=global_counts))
        return plans


def _validate_written(db: Any, plan: ConversationPlan) -> None:
    rows = db.execute(
        'SELECT turn_id,lane_id,parent_turn_id,ordinal,actor,kind,status,'
        'current_attempt_id,projection FROM conversation_turns '
        'WHERE conversation_id=? ORDER BY lane_id,ordinal',
        (plan.conversation_id,),
    ).fetchall()
    if len(rows) != len(plan.turns):
        raise RuntimeError(
            f'{plan.conversation_id}: turn count mismatch '
            f'{len(rows)} != {len(plan.turns)}')
    if len({row['turn_id'] for row in rows}) != len(rows):
        raise RuntimeError(f'{plan.conversation_id}: duplicate turn id')
    known_ids = {turn['turn_id'] for turn in plan.turns}
    if any(turn['parent_turn_id'] and turn['parent_turn_id'] not in known_ids
           for turn in plan.turns):
        raise RuntimeError(f'{plan.conversation_id}: broken parent relation')
    expected = sorted(plan.turns, key=lambda t: (t['lane_id'], t['ordinal']))
    for actual, wanted in zip(rows, expected):
        if (actual['turn_id'], actual['lane_id'], actual['parent_turn_id'],
                int(actual['ordinal']), actual['actor'], actual['kind'],
                actual['status'], actual['current_attempt_id']) != (
                wanted['turn_id'], wanted['lane_id'], wanted['parent_turn_id'],
                wanted['ordinal'], wanted['actor'], wanted['kind'],
                wanted['status'], wanted['current_attempt_id']):
            raise RuntimeError(f'{plan.conversation_id}: identity/order mismatch')
        if _hash(_decode(actual['projection'], {})) != _hash(wanted['projection']):
            raise RuntimeError(f'{plan.conversation_id}: projection hash mismatch')
    attempt_count = db.execute(
        'SELECT COUNT(*) AS n FROM generation_attempts WHERE conversation_id=?',
        (plan.conversation_id,),
    ).fetchone()['n']
    event_count = db.execute(
        'SELECT COUNT(*) AS n FROM attempt_events WHERE conversation_id=?',
        (plan.conversation_id,),
    ).fetchone()['n']
    if int(attempt_count) != len(plan.attempts):
        raise RuntimeError(f'{plan.conversation_id}: attempt count mismatch')
    if int(event_count) != len(plan.events):
        raise RuntimeError(f'{plan.conversation_id}: event count mismatch')


def apply_plans(plans: list[ConversationPlan]) -> dict[str, Any]:
    now = int(time.time() * 1000)
    with pooled_write_transaction(DOMAIN_CHAT, label='apply turn v2 migration') as db:
        running = db.execute(
            "SELECT COUNT(*) AS n FROM task_results WHERE status='running'"
        ).fetchone()
        if int(running['n'] or 0):
            raise RuntimeError(
                'migration requires quiesced task admission; running task_results exist')
        existing = db.execute(
            'SELECT turn_id FROM conversation_turns LIMIT 1').fetchone()
        if existing is not None:
            raise RuntimeError(
                'conversation_turns already contains writes; offline legacy '
                'migration is no longer a safe rollback-free operation')
        for plan in plans:
            for turn in plan.turns:
                db.execute(
                    'INSERT INTO conversation_turns(turn_id,conversation_id,user_id,'
                    'lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,'
                    'current_attempt_id,projection,projection_revision,settlement,'
                    'created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (turn['turn_id'], turn['conversation_id'], turn['user_id'],
                     turn['lane_id'], turn['parent_turn_id'], turn['ordinal'],
                     turn['actor'], turn['kind'], turn['run_id'], turn['status'],
                     turn['current_attempt_id'], _canon(turn['projection']),
                     turn['projection_revision'], _canon(turn['settlement']),
                     turn['created_at'], turn['updated_at']))
            for attempt in plan.attempts:
                db.execute(
                    'INSERT INTO generation_attempts(attempt_id,conversation_id,'
                    'turn_id,command_id,task_id,operation,status,'
                    'base_projection_revision,resume_anchor,config,error,created_at,'
                    'started_at,settled_at,superseded_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (attempt['attempt_id'], attempt['conversation_id'],
                     attempt['turn_id'], attempt['command_id'], attempt['task_id'],
                     attempt['operation'], attempt['status'],
                     attempt['base_projection_revision'],
                     _canon(attempt['resume_anchor']), _canon(attempt['config']),
                     _canon(attempt['error']), attempt['created_at'],
                     attempt['started_at'], attempt['settled_at'],
                     attempt['superseded_at']))
            for event in plan.events:
                db.execute(
                    'INSERT INTO attempt_events(attempt_id,seq,conversation_id,'
                    'turn_id,projection_revision,type,payload,created_at) '
                    'VALUES (?,?,?,?,?,?,?,?)',
                    (event['attempt_id'], event['seq'], event['conversation_id'],
                     event['turn_id'], event['projection_revision'], event['type'],
                     _canon(event['payload']), event['created_at']))
                db.execute(
                    'INSERT INTO scoped_sequences(namespace,scope_key,value) '
                    "VALUES ('attempt_events',?,?) ON CONFLICT(namespace,scope_key) "
                    'DO UPDATE SET value=excluded.value',
                    (event['attempt_id'], event['seq']))
            _validate_written(db, plan)
            settings_row = db.execute(
                'SELECT settings FROM conversations WHERE id=? AND user_id=?',
                (plan.conversation_id, plan.user_id),
            ).fetchone()
            settings = _decode(settings_row['settings'] if settings_row else None, {})
            settings.pop('activeTaskId', None)
            settings['_turnProtocolV2'] = True
            db.execute(
                'UPDATE conversations SET settings=? WHERE id=? AND user_id=?',
                (_canon(settings), plan.conversation_id, plan.user_id),
            )
        db.execute(
            "INSERT INTO schema_meta(key,value) VALUES ('_turn_schema_version','2') "
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
        db.execute(
            "INSERT INTO schema_meta(key,value) VALUES ('_turn_cutover_at',?) "
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (str(now),))
    return {
        'schemaVersion': 2,
        'conversations': len(plans),
        'turns': sum(len(plan.turns) for plan in plans),
        'attempts': sum(len(plan.attempts) for plan in plans),
    }


__all__ = ['ConversationPlan', 'plan_conversation', 'plan_database', 'apply_plans']
