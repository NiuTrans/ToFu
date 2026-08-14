"""Authoritative Turn / Attempt lifecycle for the v2 chat protocol.

One visible row owns one stable ``turn_id``.  Every execution against that
row owns a distinct ``attempt_id``.  This module is the only write path for
the three v2 tables and deliberately keeps task ids as an internal bridge to
the existing model/tool executor.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from lib.database import (
    DOMAIN_CHAT,
    allocate_scoped_sequence,
    assert_write_transaction,
    lock_scoped_sequence,
    pooled_db,
    pooled_write_transaction,
)
from lib.log import get_logger

logger = get_logger(__name__)

ACTORS = frozenset({'human', 'assistant', 'planner', 'critic', 'virtual_user'})
OPERATIONS = frozenset({'generate', 'continue', 'checkpoint_resume', 'regenerate'})
TERMINAL_STATUSES = frozenset({'completed', 'interrupted', 'truncated', 'failed'})
LIVE_ATTEMPT_STATUSES = frozenset({'pending', 'running'})


@dataclass
class LifecycleConflict(RuntimeError):
    code: str
    message: str
    turn: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


class LifecycleNotFound(LookupError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _uuid() -> str:
    return str(uuid.uuid4())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _decoded(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        logger.debug('[TurnLifecycle] invalid JSON projection: %s', exc)
        return default


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, TypeError, IndexError) as exc:
        logger.debug('[TurnLifecycle] row key %r unavailable: %s', key, exc)
        return default


def _public_turn(row: Any, *, light: bool = False) -> dict[str, Any]:
    projection = _decoded(_row_value(row, 'projection'), {})
    if light:
        projection = {
            key: projection[key]
            for key in ('content', 'thinking', 'segments', 'model', 'usage')
            if key in projection
        }
    return {
        'turnId': _row_value(row, 'turn_id', ''),
        'conversationId': _row_value(row, 'conversation_id', ''),
        'laneId': _row_value(row, 'lane_id', 'main'),
        'parentTurnId': _row_value(row, 'parent_turn_id'),
        'ordinal': int(_row_value(row, 'ordinal', 0) or 0),
        'actor': _row_value(row, 'actor', ''),
        'kind': _row_value(row, 'kind', 'reply'),
        'runId': _row_value(row, 'run_id', ''),
        'status': _row_value(row, 'status', 'pending'),
        'currentAttemptId': _row_value(row, 'current_attempt_id'),
        'projection': projection,
        'projectionRevision': int(
            _row_value(row, 'projection_revision', 0) or 0),
        'settlement': _decoded(_row_value(row, 'settlement'), {}),
        'createdAt': int(_row_value(row, 'created_at', 0) or 0),
        'updatedAt': int(_row_value(row, 'updated_at', 0) or 0),
    }


def _public_attempt(row: Any) -> dict[str, Any]:
    return {
        'attemptId': _row_value(row, 'attempt_id', ''),
        'conversationId': _row_value(row, 'conversation_id', ''),
        'turnId': _row_value(row, 'turn_id', ''),
        'commandId': _row_value(row, 'command_id', ''),
        'operation': _row_value(row, 'operation', ''),
        'status': _row_value(row, 'status', ''),
        'baseProjectionRevision': int(
            _row_value(row, 'base_projection_revision', 0) or 0),
        'resumeAnchor': _decoded(_row_value(row, 'resume_anchor'), {}),
        'createdAt': int(_row_value(row, 'created_at', 0) or 0),
        'startedAt': _row_value(row, 'started_at'),
        'settledAt': _row_value(row, 'settled_at'),
    }


def _turn_row(db: Any, conversation_id: str, turn_id: str, user_id: Any):
    return db.execute(
        'SELECT * FROM conversation_turns WHERE conversation_id=? '
        'AND turn_id=? AND user_id=?',
        (conversation_id, turn_id, user_id),
    ).fetchone()


def _attempt_row(db: Any, attempt_id: str):
    return db.execute(
        'SELECT * FROM generation_attempts WHERE attempt_id=?',
        (attempt_id,),
    ).fetchone()


def _command_result(db: Any, conversation_id: str, command_id: str,
                    user_id: Any) -> dict[str, Any] | None:
    row = db.execute(
        'SELECT a.*, t.user_id AS owner_user_id FROM generation_attempts a '
        'JOIN conversation_turns t ON t.turn_id=a.turn_id '
        'WHERE a.conversation_id=? AND a.command_id=?',
        (conversation_id, command_id),
    ).fetchone()
    if row is None or str(_row_value(row, 'owner_user_id')) != str(user_id):
        return None
    turn = _turn_row(db, conversation_id, _row_value(row, 'turn_id'), user_id)
    submitted = None
    if turn is not None:
        parent_id = _row_value(turn, 'parent_turn_id')
        if parent_id:
            submitted = _turn_row(db, conversation_id, parent_id, user_id)
    rev_row = db.execute(
        'SELECT rev FROM conversations WHERE id=? AND user_id=?',
        (conversation_id, user_id),
    ).fetchone()
    result = {
        'turn': _public_turn(turn),
        'attempt': _public_attempt(row),
        'conversationRevision': int(_row_value(rev_row, 'rev', 0) or 0),
        # Cursor immediately after durable command acceptance. A lost-ACK
        # retry must replay every execution event (especially an interaction
        # request), not jump to the latest sequence merely because the command
        # row already exists.
        'streamCursor': 1,
        'idempotentReplay': True,
        '_needsStart': (_row_value(row, 'status') == 'pending'
                        and not _row_value(row, 'task_id', '')),
    }
    if submitted is not None:
        result['submittedTurn'] = _public_turn(submitted)
    return result


def latest_event_seq(db: Any, attempt_id: str) -> int:
    row = db.execute(
        'SELECT COALESCE(MAX(seq), 0) AS seq FROM attempt_events '
        'WHERE attempt_id=?', (attempt_id,),
    ).fetchone()
    return int(_row_value(row, 'seq', 0) or 0)


def _append_event(db: Any, *, attempt_id: str, conversation_id: str,
                  turn_id: str, projection_revision: int, event_type: str,
                  payload: dict[str, Any]) -> int:
    assert_write_transaction(db, label='append v2 attempt event')
    seq = allocate_scoped_sequence(
        db, 'attempt_events', attempt_id)
    envelope = {
        'conversationId': conversation_id,
        'turnId': turn_id,
        'attemptId': attempt_id,
        'seq': seq,
        'projectionRevision': int(projection_revision),
        'type': event_type,
        'payload': payload,
    }
    db.execute(
        'INSERT INTO attempt_events(attempt_id,seq,conversation_id,turn_id,'
        'projection_revision,type,payload,created_at) VALUES (?,?,?,?,?,?,?,?)',
        (attempt_id, seq, conversation_id, turn_id,
         int(projection_revision), event_type, _json(envelope), _now_ms()),
    )
    return seq


def _bump_conversation(db: Any, conversation_id: str, user_id: Any,
                       now: int) -> int:
    assert_write_transaction(db, label='bump v2 conversation revision')
    # ``conversations.rev`` is shared by the legacy message protocol and v2
    # turn metadata.  Under normalized-row authority, messages_rows_rev must
    # stay equal to it even when this particular bump changes only v2 tables;
    # otherwise every projection event makes the canonical transcript look
    # stale and the next server boot correctly fails its authority preflight.
    # Keep the equality as a CAS precondition: never bless an already-stale
    # row set merely because a v2 event happened afterwards.
    from lib.database.conversation_repository import (
        conversation_rows_authoritative,
    )
    if conversation_rows_authoritative():
        cursor = db.execute(
            'UPDATE conversations SET rev=rev+1, messages_rows_rev=rev+1, '
            'updated_at=? WHERE id=? AND user_id=? '
            'AND messages_rows_rev=rev',
            (now, conversation_id, user_id),
        )
        if getattr(cursor, 'rowcount', None) != 1:
            raise LifecycleConflict(
                'transcript_authority_stale',
                'Canonical conversation message rows are not current.',
            )
    else:
        db.execute(
            'UPDATE conversations SET rev=rev+1, updated_at=? '
            'WHERE id=? AND user_id=?', (now, conversation_id, user_id))
    row = db.execute(
        'SELECT rev FROM conversations WHERE id=? AND user_id=?',
        (conversation_id, user_id),
    ).fetchone()
    return int(_row_value(row, 'rev', 0) or 0)


def _normalize_projection(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        return {'content': raw}
    if not isinstance(raw, dict):
        return {'content': ''}
    result = dict(raw)
    for identity_key in ('turnId', 'attemptId', '_turnId', '_attemptId',
                         '_msgId', '_taskId', 'activeTaskId', 'role',
                         '_turnActor', '_turnKind', '_turnLaneId',
                         '_turnStatus', '_turnSettlement',
                         '_projectionRevision', '_commandPending', 'branches'):
        result.pop(identity_key, None)
    if 'content' not in result and 'text' in result:
        result['content'] = result.get('text') or ''
    result.setdefault('content', '')
    return result


def create_turn_pair(conversation_id: str, *, command_id: str,
                     input_projection: Any, config: dict[str, Any] | None,
                     lane_id: str = 'main', parent_turn_id: str | None = None,
                     kind: str = 'reply', output_actor: str = 'assistant',
                     run_id: str = '', user_id: Any = 1,
                     input_actor: str = 'human', input_kind: str = 'input',
                     require_parent_is_lane_tail: bool = False,
                     conversation_defaults: dict[str, Any] | None = None,
                     ) -> dict[str, Any]:
    """Atomically create the input turn, output turn and first attempt."""
    if not command_id:
        raise ValueError('commandId is required')
    if output_actor not in ACTORS or output_actor == 'human':
        raise ValueError('invalid output actor')
    if input_actor not in {'human', 'virtual_user', 'critic'}:
        raise ValueError('invalid input actor')
    lane_id = lane_id or 'main'
    now = _now_ms()
    with pooled_write_transaction(DOMAIN_CHAT, label='create v2 turn pair') as db:
        # Serialize both first-message conversation creation and later ordinal
        # allocation. This is intentionally acquired before probing the parent
        # row: two tabs creating the same client-minted conv id cannot both
        # observe "missing" and race an INSERT.
        lock_scoped_sequence(db, 'conversation_turns', conversation_id)
        lock_scoped_sequence(
            db, 'turn_commands', f'{conversation_id}:{command_id}')
        conv = db.execute(
            'SELECT rev FROM conversations WHERE id=? AND user_id=?',
            (conversation_id, user_id),
        ).fetchone()
        if conv is None:
            defaults = dict(conversation_defaults or {})
            if not defaults.get('allowCreate'):
                raise LifecycleNotFound('Conversation not found')
            title = str(defaults.get('title') or 'New Chat')[:500]
            settings = defaults.get('settings')
            if not isinstance(settings, dict):
                settings = {}
            else:
                settings = dict(settings)
            settings.pop('activeTaskId', None)
            settings['_turnProtocolV2'] = True
            created_at = int(defaults.get('createdAt') or now)
            # Conversation creation must cross the transcript repository even
            # for v2's empty message array.  In row-authority mode it is the
            # one sanctioned initializer for the frozen archive and atomically
            # stamps the empty normalized row set current at rev 0.
            from lib.database.conversation_repository import upsert_conversation
            upsert_conversation(
                db, conversation_id, [], title=title,
                created_at=created_at, updated_at=now, user_id=user_id,
                settings=_json(settings), full=True,
            )
            conv = db.execute(
                'SELECT rev FROM conversations WHERE id=? AND user_id=?',
                (conversation_id, user_id),
            ).fetchone()
        # The command row is the cross-process idempotency boundary.  The
        # conversation row serializes ordinal allocation for distinct commands
        # that arrive concurrently from separate tabs or server workers.
        replay = _command_result(db, conversation_id, command_id, user_id)
        if replay:
            return replay
        if conversation_defaults:
            # Per-conversation UI choices belong to the accepted command
            # transaction; they are metadata, never a second transcript
            # authority. Apply only after the replay check so a lost-ACK retry
            # with a mutated body cannot smuggle in a second settings write.
            settings = conversation_defaults.get('settings')
            if isinstance(settings, dict):
                settings = dict(settings)
                settings.pop('activeTaskId', None)
                settings['_turnProtocolV2'] = True
                db.execute(
                    'UPDATE conversations SET settings=?,updated_at=? '
                    'WHERE id=? AND user_id=?',
                    (_json(settings), now, conversation_id, user_id),
                )
        if parent_turn_id:
            parent = _turn_row(db, conversation_id, parent_turn_id, user_id)
            if parent is None:
                raise LifecycleConflict(
                    'invalid_parent_turn', 'Parent turn does not exist')
        if input_actor == 'human':
            live = db.execute(
                'SELECT t.* FROM conversation_turns t '
                'JOIN generation_attempts a '
                'ON a.attempt_id=t.current_attempt_id '
                'WHERE t.conversation_id=? AND t.user_id=? AND t.lane_id=? '
                "AND a.status IN ('pending','running') "
                'ORDER BY t.ordinal DESC LIMIT 1',
                (conversation_id, user_id, lane_id),
            ).fetchone()
            if live is not None:
                raise LifecycleConflict(
                    'lane_busy',
                    'This lane already has a live generation attempt.',
                    _public_turn(live),
                )
        ord_row = db.execute(
            'SELECT COALESCE(MAX(ordinal), -1) AS ordinal '
            'FROM conversation_turns WHERE conversation_id=? AND lane_id=?',
            (conversation_id, lane_id),
        ).fetchone()
        if require_parent_is_lane_tail:
            tail = db.execute(
                'SELECT turn_id FROM conversation_turns WHERE conversation_id=? '
                'AND lane_id=? ORDER BY ordinal DESC LIMIT 1',
                (conversation_id, lane_id),
            ).fetchone()
            if tail is None or _row_value(tail, 'turn_id') != parent_turn_id:
                raise LifecycleConflict(
                    'lane_advanced',
                    'The lane advanced while the automatic continuation was prepared.',
                    _public_turn(parent) if parent is not None else None,
                )
        input_ordinal = int(_row_value(ord_row, 'ordinal', -1)) + 1
        input_turn_id, output_turn_id, attempt_id = _uuid(), _uuid(), _uuid()
        input_attempt_id = _uuid() if input_actor != 'human' else None
        submitted_projection = _normalize_projection(input_projection)
        submitted_settlement = {
            'outcome': 'completed',
            'cause': ('submitted' if input_actor == 'human'
                      else 'orchestration_generated'),
            'resumeOptions': [],
        }
        db.execute(
            'INSERT INTO conversation_turns(turn_id,conversation_id,user_id,'
            'lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,'
            'current_attempt_id,projection,projection_revision,settlement,'
            'created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (input_turn_id, conversation_id, user_id, lane_id, parent_turn_id,
             input_ordinal, input_actor, input_kind or 'input', run_id,
             'completed', input_attempt_id, _json(submitted_projection), 1,
             _json(submitted_settlement), now, now),
        )
        if input_attempt_id:
            db.execute(
                'INSERT INTO generation_attempts(attempt_id,conversation_id,turn_id,'
                'command_id,task_id,operation,status,base_projection_revision,'
                'resume_anchor,config,error,created_at,started_at,settled_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (input_attempt_id, conversation_id, input_turn_id,
                 f'{command_id}:input', '', 'generate', 'completed', 0,
                 _json({}), _json({'runId': run_id}), _json({}), now, now, now),
            )
            _append_event(
                db, attempt_id=input_attempt_id,
                conversation_id=conversation_id, turn_id=input_turn_id,
                projection_revision=1, event_type='terminal_settlement',
                payload={'status': 'completed',
                         'settlement': submitted_settlement,
                         'projection': submitted_projection},
            )
        db.execute(
            'INSERT INTO conversation_turns(turn_id,conversation_id,user_id,'
            'lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,'
            'current_attempt_id,projection,projection_revision,settlement,'
            'created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (output_turn_id, conversation_id, user_id, lane_id, input_turn_id,
             input_ordinal + 1, output_actor, kind or 'reply', run_id,
             'pending', attempt_id, _json({'content': '', 'thinking': '',
                                           'segments': [], 'toolRounds': []}),
             1, _json({}), now, now),
        )
        db.execute(
            'INSERT INTO generation_attempts(attempt_id,conversation_id,turn_id,'
            'command_id,task_id,operation,status,base_projection_revision,'
            'resume_anchor,config,error,created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (attempt_id, conversation_id, output_turn_id, command_id, '',
             'generate', 'pending', 0, _json({}), _json(config or {}),
             _json({}), now),
        )
        _append_event(
            db, attempt_id=attempt_id, conversation_id=conversation_id,
            turn_id=output_turn_id, projection_revision=1,
            event_type='status_changed', payload={'status': 'pending'},
        )
        revision = _bump_conversation(db, conversation_id, user_id, now)
        turn = _turn_row(db, conversation_id, output_turn_id, user_id)
        attempt = _attempt_row(db, attempt_id)
        return {
            'submittedTurn': _public_turn(_turn_row(
                db, conversation_id, input_turn_id, user_id)),
            'turn': _public_turn(turn),
            'attempt': _public_attempt(attempt),
            'conversationRevision': revision,
            'streamCursor': 1,
            'idempotentReplay': False,
            '_needsStart': True,
        }


def announce_related_turns(attempt_id: str, turn_ids: list[str]) -> bool:
    """Publish server-created orchestration identities on a parent stream."""
    if not attempt_id or not turn_ids:
        return False
    now = _now_ms()
    with pooled_write_transaction(DOMAIN_CHAT, label='announce related v2 turns') as db:
        lock_scoped_sequence(db, 'attempt_events', attempt_id)
        attempt = _attempt_row(db, attempt_id)
        if attempt is None or _row_value(attempt, 'status') not in LIVE_ATTEMPT_STATUSES:
            return False
        root = db.execute(
            'SELECT * FROM conversation_turns WHERE turn_id=?',
            (_row_value(attempt, 'turn_id'),),
        ).fetchone()
        if root is None or _row_value(root, 'current_attempt_id') != attempt_id:
            return False
        related = []
        related_attempts = []
        for turn_id in turn_ids:
            row = db.execute(
                'SELECT * FROM conversation_turns WHERE turn_id=? AND '
                'conversation_id=?',
                (turn_id, _row_value(attempt, 'conversation_id')),
            ).fetchone()
            if row is None:
                continue
            related.append(_public_turn(row))
            current_attempt_id = _row_value(row, 'current_attempt_id')
            if current_attempt_id:
                current_attempt = _attempt_row(db, current_attempt_id)
                if current_attempt is not None:
                    related_attempts.append(_public_attempt(current_attempt))
        if not related:
            return False
        old_revision = int(_row_value(root, 'projection_revision', 0) or 0)
        new_revision = old_revision + 1
        db.execute(
            'UPDATE conversation_turns SET projection_revision=?,updated_at=? '
            'WHERE turn_id=? AND current_attempt_id=?',
            (new_revision, now, _row_value(root, 'turn_id'), attempt_id),
        )
        _append_event(
            db, attempt_id=attempt_id,
            conversation_id=_row_value(attempt, 'conversation_id'),
            turn_id=_row_value(root, 'turn_id'),
            projection_revision=new_revision,
            event_type='projection_updated',
            payload={'projection': _decoded(_row_value(root, 'projection'), {}),
                     'turns': related, 'attempts': related_attempts,
                     'updateKind': 'related_turns_created'},
        )
        _bump_conversation(
            db, _row_value(attempt, 'conversation_id'),
            _row_value(root, 'user_id'), now)
        return True


def _resume_options(settlement: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for item in settlement.get('resumeOptions') or []:
        operation = item if isinstance(item, str) else item.get('operation')
        if operation:
            result.add(operation)
    return result


def _option_anchor(settlement: dict[str, Any], operation: str) -> dict[str, Any]:
    for item in settlement.get('resumeOptions') or []:
        if isinstance(item, dict) and item.get('operation') == operation:
            return item.get('anchor') or {}
    return {}


def create_attempt(conversation_id: str, turn_id: str, *, command_id: str,
                   operation: str, expected_projection_revision: int,
                   config: dict[str, Any] | None = None,
                   resume_anchor: dict[str, Any] | None = None,
                   input_update: dict[str, Any] | None = None,
                   expected_input_projection_revision: int | None = None,
                   user_id: Any = 1) -> dict[str, Any]:
    if not command_id:
        raise ValueError('commandId is required')
    if operation not in OPERATIONS - {'generate'}:
        raise ValueError('operation must be continue, checkpoint_resume, or regenerate')
    now = _now_ms()
    with pooled_write_transaction(DOMAIN_CHAT, label='create v2 attempt') as db:
        row = _turn_row(db, conversation_id, turn_id, user_id)
        if row is None:
            raise LifecycleNotFound('Turn not found')
        lock_scoped_sequence(
            db, 'turn_commands', f'{conversation_id}:{command_id}')
        replay = _command_result(db, conversation_id, command_id, user_id)
        if replay:
            return replay
        # Revision validation and attempt replacement form one per-turn CAS.
        lock_scoped_sequence(db, 'conversation_turn_attempts', turn_id)
        row = _turn_row(db, conversation_id, turn_id, user_id)
        turn = _public_turn(row)
        if int(expected_projection_revision) != turn['projectionRevision']:
            raise LifecycleConflict(
                'stale_projection',
                'The turn changed since this command was prepared.', turn)
        current_id = turn.get('currentAttemptId')
        if current_id:
            current = _attempt_row(db, current_id)
            if current and _row_value(current, 'status') in LIVE_ATTEMPT_STATUSES:
                raise LifecycleConflict(
                    'attempt_in_progress', 'This turn already has a live attempt.', turn)
        available = _resume_options(turn['settlement'])
        if operation != 'regenerate' and operation not in available:
            raise LifecycleConflict(
                'operation_not_available',
                f'{operation} is not available for the current settlement.', turn)
        authoritative_anchor = _option_anchor(turn['settlement'], operation)
        if (resume_anchor is not None
                and dict(resume_anchor) != dict(authoritative_anchor)):
            raise LifecycleConflict(
                'invalid_resume_anchor',
                'The requested resume anchor is not the current server checkpoint.',
                turn,
            )
        # A checkpoint is an authority fact, not client-provided prefill.  A
        # client may echo it for conflict detection but cannot select/modify it.
        anchor = dict(authoritative_anchor)
        submitted_turn = None
        if input_update is not None:
            if operation != 'regenerate':
                raise LifecycleConflict(
                    'input_update_not_allowed',
                    'Only regenerate may atomically edit its submitted turn.', turn)
            parent_id = _row_value(row, 'parent_turn_id')
            parent = (_turn_row(db, conversation_id, parent_id, user_id)
                      if parent_id else None)
            if parent is None or _row_value(parent, 'actor') not in {
                    'human', 'virtual_user', 'critic'}:
                raise LifecycleConflict(
                    'invalid_input_turn',
                    'The generated turn has no editable submitted parent.', turn)
            parent_revision = int(_row_value(parent, 'projection_revision', 0) or 0)
            if (expected_input_projection_revision is None or
                    int(expected_input_projection_revision) != parent_revision):
                raise LifecycleConflict(
                    'stale_input_projection',
                    'The submitted turn changed since editing began.',
                    _public_turn(parent))
            updated_input = _normalize_projection(input_update)
            db.execute(
                'UPDATE conversation_turns SET projection=?,projection_revision=?, '
                'updated_at=? WHERE turn_id=? AND projection_revision=?',
                (_json(updated_input), parent_revision + 1, now, parent_id,
                 parent_revision),
            )
            submitted_turn = _public_turn(
                _turn_row(db, conversation_id, parent_id, user_id))
        projection = dict(turn['projection'])
        if operation == 'regenerate':
            projection = {'content': '', 'thinking': '', 'segments': [],
                          'toolRounds': []}
        elif operation == 'checkpoint_resume':
            projection['content'] = anchor.get('content', '')
            projection['thinking'] = anchor.get('thinking', '')
            kept = int(anchor.get('keptToolRounds', 0) or 0)
            projection['toolRounds'] = list(
                (projection.get('toolRounds') or [])[:kept])
            projection['segments'] = list(anchor.get('segments') or [])
        new_revision = turn['projectionRevision'] + 1
        attempt_id = _uuid()
        if current_id:
            db.execute(
                "UPDATE generation_attempts SET status='superseded', "
                'superseded_at=? WHERE attempt_id=? AND status NOT IN '
                "('pending','running')", (now, current_id))
        db.execute(
            'INSERT INTO generation_attempts(attempt_id,conversation_id,turn_id,'
            'command_id,task_id,operation,status,base_projection_revision,'
            'resume_anchor,config,error,created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (attempt_id, conversation_id, turn_id, command_id, '', operation,
             'pending', turn['projectionRevision'], _json(anchor),
             _json(config or {}), _json({}), now),
        )
        db.execute(
            "UPDATE conversation_turns SET status='pending', current_attempt_id=?, "
            'projection=?, projection_revision=?, settlement=?, updated_at=? '
            'WHERE turn_id=? AND projection_revision=?',
            (attempt_id, _json(projection), new_revision, _json({}), now,
             turn_id, turn['projectionRevision']),
        )
        _append_event(
            db, attempt_id=attempt_id, conversation_id=conversation_id,
            turn_id=turn_id, projection_revision=new_revision,
            event_type='status_changed',
            payload={'status': 'pending', 'operation': operation,
                     **({'turns': [submitted_turn]} if submitted_turn else {})},
        )
        revision = _bump_conversation(db, conversation_id, user_id, now)
        result = {
            'turn': _public_turn(_turn_row(
                db, conversation_id, turn_id, user_id)),
            'attempt': _public_attempt(_attempt_row(db, attempt_id)),
            'conversationRevision': revision,
            'streamCursor': 1,
            'idempotentReplay': False,
            '_needsStart': True,
        }
        if submitted_turn is not None:
            result['submittedTurn'] = submitted_turn
        return result


def bind_task(attempt_id: str, task_id: str) -> dict[str, Any] | None:
    """Bind the internal executor task and expose the attempt as running."""
    now = _now_ms()
    with pooled_write_transaction(DOMAIN_CHAT, label='bind v2 attempt task') as db:
        attempt = _attempt_row(db, attempt_id)
        if attempt is None:
            return None
        turn_id = _row_value(attempt, 'turn_id')
        conversation_id = _row_value(attempt, 'conversation_id')
        turn = db.execute(
            'SELECT * FROM conversation_turns WHERE turn_id=?',
            (turn_id,),
        ).fetchone()
        if turn is None or _row_value(turn, 'current_attempt_id') != attempt_id:
            return None
        # A very fast task may have settled before the HTTP starter binds it.
        # Persist task_id, but never regress a terminal attempt to running.
        db.execute(
            'UPDATE generation_attempts SET task_id=?, status=CASE '
            "WHEN status='pending' THEN 'running' ELSE status END, "
            'started_at=COALESCE(started_at,?) WHERE attempt_id=?',
            (task_id, now, attempt_id),
        )
        if _row_value(attempt, 'status') != 'pending':
            return _public_attempt(_attempt_row(db, attempt_id))
        old_revision = int(_row_value(turn, 'projection_revision', 0) or 0)
        new_revision = old_revision + 1
        updated = db.execute(
            "UPDATE conversation_turns SET status='running', "
            'projection_revision=?, updated_at=? WHERE turn_id=? '
            "AND current_attempt_id=? AND status='pending' "
            'AND projection_revision=?',
            (new_revision, now, turn_id, attempt_id, old_revision),
        )
        if getattr(updated, 'rowcount', 0):
            _append_event(
                db, attempt_id=attempt_id, conversation_id=conversation_id,
                turn_id=turn_id, projection_revision=new_revision,
                event_type='status_changed', payload={'status': 'running'},
            )
            _bump_conversation(
                db, conversation_id, _row_value(turn, 'user_id'), now)
        return _public_attempt(_attempt_row(db, attempt_id))


def claim_attempt_start(attempt_id: str) -> bool:
    """Acquire the one-shot executor-dispatch lease for an accepted attempt.

    This closes the commit-to-task-bind window: a concurrent lost-ACK retry
    sees the durable claim and attaches to the same attempt rather than
    launching a second billable request.  A process crash after the claim is
    intentionally recovered as ``interrupted`` on boot, never auto-retried.
    """
    with pooled_write_transaction(DOMAIN_CHAT, label='claim v2 attempt start') as db:
        lock_scoped_sequence(db, 'attempt_dispatch', attempt_id)
        updated = db.execute(
            "UPDATE generation_attempts SET task_id=? WHERE attempt_id=? "
            "AND status='pending' AND task_id=''",
            (f'@dispatching:{attempt_id}', attempt_id),
        )
        return bool(getattr(updated, 'rowcount', 0))


def fail_start(attempt_id: str, error: Any) -> None:
    task = {'_attemptId': attempt_id, 'id': '', 'status': 'error',
            'error': error, 'content': '', 'thinking': '', 'toolRounds': []}
    record_task_event(task, {'type': 'error', 'error': error})


def _task_projection(task: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    projection = dict(previous)
    cfg = task.get('config') or {}
    # Endpoint/Flow phases commit their visible rows independently through
    # ``sync_visible_run_turns``.  Later orchestration bookkeeping events must
    # not fold the aggregate task buffer back over the first phase's bubble.
    owns_visible_run_turns = bool(task.get('_v2VisibleRunTurnIds'))
    content = (projection.get('content', '') if owns_visible_run_turns else
               (task.get('content') or cfg.get('contentPrefix') or ''))
    checkpoint_rounds = (task.get('_checkpointToolRounds')
                         or cfg.get('checkpointToolRounds') or [])
    projection.update({
        'content': content,
        'thinking': (task.get('thinking') if task.get('thinking') is not None
                     else projection.get('thinking', '')),
        'toolRounds': list(checkpoint_rounds) + list(task.get('toolRounds') or []),
    })
    for source, target in (
        ('segments', 'segments'), ('usage', 'usage'), ('model', 'model'),
        ('preset', 'preset'), ('thinkingDepth', 'thinkingDepth'),
        ('modifiedFiles', 'modifiedFiles'),
        ('modifiedFileList', 'modifiedFileList'), ('todoState', 'todoState'),
    ):
        if task.get(source) is not None:
            projection[target] = task[source]
    return projection


def _has_checkpoint(projection: dict[str, Any]) -> tuple[bool, int]:
    rounds = projection.get('toolRounds') or []
    kept = 0
    for item in rounds:
        if isinstance(item, dict) and item.get('status') in ('done', 'completed'):
            kept += 1
        else:
            break
    return kept > 0, kept


def _supports_lossless_prefill(task: dict[str, Any], projection: dict[str, Any]) -> bool:
    if not projection.get('content'):
        return False
    model = task.get('model') or (task.get('config') or {}).get('model') or ''
    if not model:
        return False
    try:
        from lib.model_info import model_supports_assistant_prefill
        return bool(model_supports_assistant_prefill(model))
    except Exception as exc:
        logger.debug('[TurnLifecycle] prefill capability probe failed: %s', exc)
        return False


def _settlement(task: dict[str, Any], raw_event: dict[str, Any],
                projection: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    event_type = str(raw_event.get('type') or '')
    finish = (raw_event.get('finishReason') or task.get('finishReason') or '')
    error = raw_event.get('error') or task.get('error')
    if event_type == 'aborted' or task.get('aborted') or task.get('status') == 'aborted':
        status, outcome, cause = 'interrupted', 'interrupted', 'user_abort'
    elif error or event_type == 'error' or task.get('status') == 'error':
        status, outcome, cause = 'failed', 'failed', 'generation_error'
    elif finish in {'length', 'max_tokens', 'context_length', 'content_filter'}:
        status, outcome, cause = 'truncated', 'truncated', finish
    else:
        status, outcome, cause = 'completed', 'completed', 'provider_finished'
    options: list[dict[str, Any]] = []
    if status in {'interrupted', 'truncated', 'failed'} and _supports_lossless_prefill(task, projection):
        options.append({
            'operation': 'continue',
            'anchor': {'type': 'lossless_prefill',
                       'contentChars': len(projection.get('content') or '')},
        })
    has_checkpoint, kept = _has_checkpoint(projection)
    if status in {'interrupted', 'truncated', 'failed'} and has_checkpoint:
        rounds = projection.get('toolRounds') or []
        last = rounds[kept - 1] if kept else {}
        options.append({
            'operation': 'checkpoint_resume',
            'anchor': {
                'type': 'tool_checkpoint', 'keptToolRounds': kept,
                'content': last.get('assistantContent') or '',
                'thinking': last.get('thinking') or '', 'segments': [],
            },
        })
    options.append({'operation': 'regenerate', 'anchor': {'type': 'turn_start'}})
    settlement = {
        'outcome': outcome,
        'cause': cause,
        'providerFinishReason': finish or None,
        'error': error or None,
        'resumeOptions': options,
    }
    if task.get('_v2NextAttemptId'):
        settlement['continuation'] = {
            'turnId': task.get('_v2NextTurnId') or '',
            'attemptId': task['_v2NextAttemptId'],
        }
    return status, settlement


_INTERACTION_EVENTS = frozenset({
    'stdin_request', 'human_guidance_request', 'write_approval_request',
    'ask_human', 'approval_request',
})
_TERMINAL_EVENTS = frozenset({'done', 'error', 'aborted'})


def record_task_event(task: dict[str, Any], raw_event: dict[str, Any]) -> bool:
    """Persist one task projection/event before it becomes client-visible.

    Returns False for a legacy task, stale attempt, duplicate terminal event,
    or superseded executor.  Those events must not mutate v2 authority.
    """
    attempt_id = task.get('_attemptId') or task.get('attemptId')
    if not attempt_id:
        return False
    now = _now_ms()
    with pooled_write_transaction(DOMAIN_CHAT, label='record v2 attempt event') as db:
        attempt = _attempt_row(db, attempt_id)
        if attempt is None or _row_value(attempt, 'status') not in LIVE_ATTEMPT_STATUSES:
            return False
        # Cross-thread/process event producers serialize on one durable row.
        # This makes the projection CAS + seq allocation one ordered unit.
        lock_scoped_sequence(db, 'attempt_events', attempt_id)
        attempt = _attempt_row(db, attempt_id)
        if attempt is None or _row_value(attempt, 'status') not in LIVE_ATTEMPT_STATUSES:
            return False
        turn_id = _row_value(attempt, 'turn_id')
        turn = db.execute(
            'SELECT * FROM conversation_turns WHERE turn_id=?',
            (turn_id,),
        ).fetchone()
        if turn is None or _row_value(turn, 'current_attempt_id') != attempt_id:
            return False
        previous = _decoded(_row_value(turn, 'projection'), {})
        projection = _task_projection(task, previous)
        old_revision = int(_row_value(turn, 'projection_revision', 0) or 0)
        new_revision = old_revision + 1
        event_kind = str(raw_event.get('type') or 'projection')
        terminal = event_kind in _TERMINAL_EVENTS
        if terminal:
            status, settlement = _settlement(task, raw_event, projection)
            attempt_status = status
            error = settlement.get('error') or {}
            updated = db.execute(
                'UPDATE conversation_turns SET status=?, projection=?, '
                'projection_revision=?, settlement=?, updated_at=? '
                'WHERE turn_id=? AND current_attempt_id=? '
                'AND projection_revision=?',
                (status, _json(projection), new_revision, _json(settlement), now,
                 turn_id, attempt_id, old_revision),
            )
            if not getattr(updated, 'rowcount', 0):
                return False
            db.execute(
                'UPDATE generation_attempts SET status=?, error=?, settled_at=? '
                'WHERE attempt_id=? AND status IN (\'pending\',\'running\')',
                (attempt_status, _json(error), now, attempt_id),
            )
            event_type = 'terminal_settlement'
            payload = {'status': status, 'settlement': settlement,
                       'projection': projection}
        else:
            updated = db.execute(
                "UPDATE conversation_turns SET status='running', projection=?, "
                'projection_revision=?, updated_at=? WHERE turn_id=? '
                'AND current_attempt_id=? AND projection_revision=?',
                (_json(projection), new_revision, now, turn_id, attempt_id,
                 old_revision),
            )
            if not getattr(updated, 'rowcount', 0):
                return False
            db.execute(
                "UPDATE generation_attempts SET status='running', "
                'started_at=COALESCE(started_at,?) WHERE attempt_id=? '
                "AND status='pending'", (now, attempt_id))
            event_type = ('interaction_request' if event_kind in _INTERACTION_EVENTS
                          else 'projection_updated')
            payload = {'projection': projection}
            if event_type == 'interaction_request':
                payload['request'] = raw_event
            else:
                payload['updateKind'] = event_kind
        _append_event(
            db, attempt_id=attempt_id,
            conversation_id=_row_value(attempt, 'conversation_id'),
            turn_id=turn_id, projection_revision=new_revision,
            event_type=event_type, payload=payload,
        )
        _bump_conversation(
            db, _row_value(attempt, 'conversation_id'),
            _row_value(turn, 'user_id'), now)
        return True


def _visible_turn_shape(message: dict[str, Any], default_kind: str
                        ) -> tuple[str, str, dict[str, Any]]:
    """Translate one orchestration message without exposing marker fields."""
    role = message.get('role')
    if message.get('_isVirtualUser'):
        actor, kind = 'virtual_user', 'autopilot_virtual_user'
    elif message.get('_isEndpointReview'):
        actor, kind = 'critic', 'endpoint_critic'
    elif message.get('_isEndpointPlanner'):
        actor, kind = 'planner', 'endpoint_planner'
    elif message.get('_flowNodeId') or message.get('_flowRunId'):
        actor = 'critic' if role == 'user' else 'assistant'
        kind = 'flow_node'
    else:
        actor = 'critic' if role == 'user' else 'assistant'
        kind = default_kind or 'endpoint_worker'
    projection = {
        key: value for key, value in message.items()
        if key != 'role' and not key.startswith('_')
    }
    projection.setdefault('content', '')
    projection.setdefault('thinking', '')
    projection.setdefault('segments', [])
    projection.setdefault('toolRounds', [])
    phase = {
        'iteration': (message.get('_epIteration')
                      or message.get('_epPlannerIteration')),
        'approved': message.get('_epApproved'),
        'nextPhase': message.get('_epNextPhase'),
        'flowNodeId': message.get('_flowNodeId'),
        'flowRunId': message.get('_flowRunId'),
    }
    projection['orchestration'] = {
        key: value for key, value in phase.items() if value is not None
    }
    return actor, kind, projection


def sync_visible_run_turns(task: dict[str, Any], messages: list[dict[str, Any]],
                           *, default_kind: str = 'endpoint_worker') -> int | None:
    """Commit Endpoint/Flow/Autopilot visible messages as explicit turns.

    The first generated row reuses the output ``turn_id`` allocated by the
    command.  Later phase rows use deterministic identities and terminal
    synthetic attempts, so replaying the accumulated phase list is idempotent
    without consulting array tails or public task ids.
    """
    attempt_id = task.get('_attemptId')
    root_turn_id = task.get('_turnId')
    conversation_id = task.get('convId')
    if not (task.get('_turnProtocolV2') and attempt_id and root_turn_id
            and conversation_id and messages):
        return None
    now = _now_ms()
    visible_ids: list[str] = []
    with pooled_write_transaction(DOMAIN_CHAT, label='sync v2 visible run turns') as db:
        lock_scoped_sequence(db, 'attempt_events', attempt_id)
        attempt = _attempt_row(db, attempt_id)
        root = db.execute(
            'SELECT * FROM conversation_turns WHERE turn_id=? '
            'AND conversation_id=?', (root_turn_id, conversation_id),
        ).fetchone()
        if attempt is None or root is None:
            return None
        if _row_value(root, 'current_attempt_id') != attempt_id:
            return None
        run_id = (_row_value(root, 'run_id') or
                  (task.get('config') or {}).get('runId') or attempt_id)
        changed = False
        previous_turn_id = _row_value(root, 'parent_turn_id')
        related: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            actor, kind, projection = _visible_turn_shape(
                message, default_kind)
            if index == 0:
                turn_id = root_turn_id
                visible_ids.append(turn_id)
                previous_turn_id = turn_id
                if not str(_row_value(root, 'kind', '')).startswith(
                        ('endpoint_', 'flow_', 'autopilot_')):
                    old_revision = int(
                        _row_value(root, 'projection_revision', 0) or 0)
                    db.execute(
                        'UPDATE conversation_turns SET actor=?,kind=?,run_id=?, '
                        'projection=?,projection_revision=?,updated_at=? '
                        'WHERE turn_id=? AND current_attempt_id=?',
                        (actor, kind, run_id, _json(projection),
                         old_revision + 1, now, root_turn_id, attempt_id),
                    )
                    root = db.execute(
                        'SELECT * FROM conversation_turns WHERE turn_id=?',
                        (root_turn_id,),
                    ).fetchone()
                    changed = True
                related.append(_public_turn(root))
                continue

            turn_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f'turn-attempt:{attempt_id}:visible:{index}'))
            child_attempt_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f'turn-attempt:{attempt_id}:visible-attempt:{index}'))
            visible_ids.append(turn_id)
            existing = db.execute(
                'SELECT * FROM conversation_turns WHERE turn_id=?',
                (turn_id,),
            ).fetchone()
            if existing is None:
                ordinal_row = db.execute(
                    'SELECT COALESCE(MAX(ordinal),-1) AS ordinal '
                    'FROM conversation_turns WHERE conversation_id=? AND lane_id=?',
                    (conversation_id, _row_value(root, 'lane_id', 'main')),
                ).fetchone()
                ordinal = int(_row_value(ordinal_row, 'ordinal', -1)) + 1
                settlement = {
                    'outcome': 'completed', 'cause': 'phase_completed',
                    'providerFinishReason': None, 'error': None,
                    'resumeOptions': [
                        {'operation': 'regenerate',
                         'anchor': {'type': 'turn_start'}},
                    ],
                }
                db.execute(
                    'INSERT INTO conversation_turns(turn_id,conversation_id,user_id,'
                    'lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,'
                    'current_attempt_id,projection,projection_revision,settlement,'
                    'created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (turn_id, conversation_id, _row_value(root, 'user_id'),
                     _row_value(root, 'lane_id', 'main'), previous_turn_id,
                     ordinal, actor, kind, run_id, 'completed', child_attempt_id,
                     _json(projection), 1, _json(settlement), now, now),
                )
                db.execute(
                    'INSERT INTO generation_attempts(attempt_id,conversation_id,'
                    'turn_id,command_id,task_id,operation,status,'
                    'base_projection_revision,resume_anchor,config,error,'
                    'created_at,started_at,settled_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (child_attempt_id, conversation_id, turn_id,
                     f'run:{attempt_id}:visible:{index}', '', 'generate',
                     'completed', 0, _json({}), _json({'runId': run_id}),
                     _json({}), now, now, now),
                )
                _append_event(
                    db, attempt_id=child_attempt_id,
                    conversation_id=conversation_id, turn_id=turn_id,
                    projection_revision=1, event_type='terminal_settlement',
                    payload={'status': 'completed', 'settlement': settlement,
                             'projection': projection},
                )
                existing = db.execute(
                    'SELECT * FROM conversation_turns WHERE turn_id=?',
                    (turn_id,),
                ).fetchone()
                changed = True
            related.append(_public_turn(existing))
            previous_turn_id = turn_id

        if changed:
            root = db.execute(
                'SELECT * FROM conversation_turns WHERE turn_id=?',
                (root_turn_id,),
            ).fetchone()
            old_revision = int(_row_value(root, 'projection_revision', 0) or 0)
            new_revision = old_revision + 1
            db.execute(
                'UPDATE conversation_turns SET projection_revision=?,updated_at=? '
                'WHERE turn_id=? AND current_attempt_id=?',
                (new_revision, now, root_turn_id, attempt_id),
            )
            root = db.execute(
                'SELECT * FROM conversation_turns WHERE turn_id=?',
                (root_turn_id,),
            ).fetchone()
            related[0] = _public_turn(root)
            _append_event(
                db, attempt_id=attempt_id, conversation_id=conversation_id,
                turn_id=root_turn_id, projection_revision=new_revision,
                event_type='projection_updated',
                payload={'projection': _decoded(_row_value(root, 'projection'), {}),
                         'turns': related, 'updateKind': 'visible_turns_committed'},
            )
            _bump_conversation(
                db, conversation_id, _row_value(root, 'user_id'), now)
    task['_v2VisibleRunTurnIds'] = visible_ids
    # Legacy callers expect a message-array index only for translation.  V2
    # translation will address a turn id and must never write via that index.
    return None


def list_turns(conversation_id: str, *, user_id: Any = 1,
               lane_id: str | None = None, after_ordinal: int | None = None,
               limit: int = 500, light: bool = False) -> dict[str, Any]:
    limit = min(max(int(limit or 500), 1), 2000)
    clauses = ['conversation_id=?', 'user_id=?']
    params: list[Any] = [conversation_id, user_id]
    if lane_id:
        clauses.append('lane_id=?')
        params.append(lane_id)
    if after_ordinal is not None:
        clauses.append('ordinal>?')
        params.append(int(after_ordinal))
    with pooled_db(DOMAIN_CHAT) as db:
        conv = db.execute(
            'SELECT rev FROM conversations WHERE id=? AND user_id=?',
            (conversation_id, user_id),
        ).fetchone()
        if conv is None:
            raise LifecycleNotFound('Conversation not found')
        rows = db.execute(
            'SELECT * FROM conversation_turns WHERE ' + ' AND '.join(clauses) +
            ' ORDER BY lane_id, ordinal LIMIT ?', (*params, limit),
        ).fetchall()
        marker = db.execute(
            "SELECT value FROM schema_meta WHERE key='_turn_schema_version'"
        ).fetchone()
        return {
            'conversationId': conversation_id,
            'conversationRevision': int(_row_value(conv, 'rev', 0) or 0),
            'turns': [_public_turn(row, light=light) for row in rows],
            'cutoverActive': str(_row_value(marker, 'value', '')) == '2',
            # Only an unfiltered, unwindowed result can authorize deletion of
            # local identities that are absent from the snapshot.
            'authoritativeFull': bool(
                lane_id is None and after_ordinal is None and len(rows) < limit),
        }


def get_turn(conversation_id: str, turn_id: str, *, user_id: Any = 1) -> dict[str, Any]:
    with pooled_db(DOMAIN_CHAT) as db:
        row = _turn_row(db, conversation_id, turn_id, user_id)
        if row is None:
            raise LifecycleNotFound('Turn not found')
        return _public_turn(row)


def get_attempt(attempt_id: str, *, user_id: Any = 1) -> dict[str, Any]:
    with pooled_db(DOMAIN_CHAT) as db:
        row = db.execute(
            'SELECT a.*,t.user_id FROM generation_attempts a '
            'JOIN conversation_turns t ON t.turn_id=a.turn_id '
            'WHERE a.attempt_id=?', (attempt_id,),
        ).fetchone()
        if row is None or str(_row_value(row, 'user_id')) != str(user_id):
            raise LifecycleNotFound('Attempt not found')
        return _public_attempt(row)


def get_conversation_revision(conversation_id: str, *, user_id: Any = 1) -> int:
    with pooled_db(DOMAIN_CHAT) as db:
        row = db.execute(
            'SELECT rev FROM conversations WHERE id=? AND user_id=?',
            (conversation_id, user_id),
        ).fetchone()
        if row is None:
            raise LifecycleNotFound('Conversation not found')
        return int(_row_value(row, 'rev', 0) or 0)


def update_turn_projection(conversation_id: str, turn_id: str, *,
                           projection: dict[str, Any],
                           expected_projection_revision: int,
                           user_id: Any = 1) -> dict[str, Any]:
    """CAS-edit one settled visible turn without creating an attempt."""
    now = _now_ms()
    with pooled_write_transaction(DOMAIN_CHAT, label='edit v2 turn projection') as db:
        lock_scoped_sequence(db, 'conversation_turn_attempts', turn_id)
        row = _turn_row(db, conversation_id, turn_id, user_id)
        if row is None:
            raise LifecycleNotFound('Turn not found')
        turn = _public_turn(row)
        if turn['status'] in LIVE_ATTEMPT_STATUSES:
            raise LifecycleConflict(
                'turn_in_progress', 'A running turn cannot be edited.', turn)
        if int(expected_projection_revision) != turn['projectionRevision']:
            raise LifecycleConflict(
                'stale_projection', 'The turn changed since editing began.', turn)
        normalized = _normalize_projection(projection)
        new_revision = turn['projectionRevision'] + 1
        updated = db.execute(
            'UPDATE conversation_turns SET projection=?,projection_revision=?, '
            'updated_at=? WHERE turn_id=? AND projection_revision=?',
            (_json(normalized), new_revision, now, turn_id,
             turn['projectionRevision']),
        )
        if not getattr(updated, 'rowcount', 0):
            latest = _public_turn(_turn_row(
                db, conversation_id, turn_id, user_id))
            raise LifecycleConflict(
                'stale_projection', 'The turn changed while the edit was applied.', latest)
        revision = _bump_conversation(db, conversation_id, user_id, now)
        return {
            'turn': _public_turn(_turn_row(
                db, conversation_id, turn_id, user_id)),
            'conversationRevision': revision,
        }


def create_branch_lane(conversation_id: str, parent_turn_id: str, *,
                       title: str, anchor_text: str = '',
                       parent_selection: str = '', kind: str = 'branch',
                       expected_projection_revision: int,
                       user_id: Any = 1) -> dict[str, Any]:
    """Create server-issued branch lane metadata on its parent turn."""
    now = _now_ms()
    with pooled_write_transaction(DOMAIN_CHAT, label='create v2 branch lane') as db:
        lock_scoped_sequence(db, 'conversation_turn_attempts', parent_turn_id)
        row = _turn_row(db, conversation_id, parent_turn_id, user_id)
        if row is None:
            raise LifecycleNotFound('Parent turn not found')
        parent = _public_turn(row)
        if parent['status'] in LIVE_ATTEMPT_STATUSES:
            raise LifecycleConflict(
                'parent_in_progress', 'A running parent turn cannot be branched.', parent)
        if int(expected_projection_revision) != parent['projectionRevision']:
            raise LifecycleConflict(
                'stale_projection', 'The parent turn changed before branch creation.', parent)
        lane_id = f'lane_{_uuid()}'
        lane = {
            'laneId': lane_id,
            'parentTurnId': parent_turn_id,
            'title': str(title or 'Branch')[:200],
            'icon': '⑂',
            'kind': str(kind or 'branch')[:80],
            'anchorText': str(anchor_text or '')[:1000],
            'parentSelection': str(parent_selection or '')[:10000],
            'createdAt': now,
        }
        projection = dict(parent['projection'])
        descriptors = list(projection.get('_branchLanes') or [])
        descriptors.append(lane)
        projection['_branchLanes'] = descriptors
        new_revision = parent['projectionRevision'] + 1
        db.execute(
            'UPDATE conversation_turns SET projection=?,projection_revision=?, '
            'updated_at=? WHERE turn_id=? AND projection_revision=?',
            (_json(projection), new_revision, now, parent_turn_id,
             parent['projectionRevision']),
        )
        revision = _bump_conversation(db, conversation_id, user_id, now)
        return {
            'turn': _public_turn(_turn_row(
                db, conversation_id, parent_turn_id, user_id)),
            'lane': lane,
            'conversationRevision': revision,
        }


def delete_branch_lane(conversation_id: str, parent_turn_id: str,
                       lane_id: str, *, user_id: Any = 1) -> dict[str, Any]:
    """Delete one explicit branch lane and all of its diagnostic attempts."""
    now = _now_ms()
    with pooled_write_transaction(DOMAIN_CHAT, label='delete v2 branch lane') as db:
        lock_scoped_sequence(db, 'conversation_turns', conversation_id)
        parent_row = _turn_row(db, conversation_id, parent_turn_id, user_id)
        if parent_row is None:
            raise LifecycleNotFound('Parent turn not found')
        live = db.execute(
            'SELECT t.* FROM conversation_turns t JOIN generation_attempts a '
            'ON a.attempt_id=t.current_attempt_id WHERE t.conversation_id=? '
            'AND t.lane_id=? AND a.status IN (\'pending\',\'running\') LIMIT 1',
            (conversation_id, lane_id),
        ).fetchone()
        if live is not None:
            raise LifecycleConflict(
                'lane_busy', 'Stop the branch attempt before deleting its lane.',
                _public_turn(live))
        parent = _public_turn(parent_row)
        projection = dict(parent['projection'])
        descriptors = list(projection.get('_branchLanes') or [])
        kept = [item for item in descriptors if item.get('laneId') != lane_id]
        if len(kept) == len(descriptors):
            raise LifecycleNotFound('Branch lane not found')
        projection['_branchLanes'] = kept
        attempt_rows = db.execute(
            'SELECT a.attempt_id FROM generation_attempts a '
            'JOIN conversation_turns t ON t.turn_id=a.turn_id '
            'WHERE t.conversation_id=? AND t.lane_id=?',
            (conversation_id, lane_id),
        ).fetchall()
        for attempt_row in attempt_rows:
            attempt_id = _row_value(attempt_row, 'attempt_id')
            db.execute('DELETE FROM attempt_events WHERE attempt_id=?', (attempt_id,))
            db.execute(
                "DELETE FROM scoped_sequences WHERE namespace='attempt_events' "
                'AND scope_key=?', (attempt_id,))
        db.execute(
            'DELETE FROM generation_attempts WHERE turn_id IN ('
            'SELECT turn_id FROM conversation_turns WHERE conversation_id=? '
            'AND lane_id=?)', (conversation_id, lane_id))
        db.execute(
            'DELETE FROM conversation_turns WHERE conversation_id=? AND lane_id=?',
            (conversation_id, lane_id))
        new_revision = parent['projectionRevision'] + 1
        db.execute(
            'UPDATE conversation_turns SET projection=?,projection_revision=?, '
            'updated_at=? WHERE turn_id=?',
            (_json(projection), new_revision, now, parent_turn_id),
        )
        revision = _bump_conversation(db, conversation_id, user_id, now)
        return {
            'turn': _public_turn(_turn_row(
                db, conversation_id, parent_turn_id, user_id)),
            'deletedLaneId': lane_id,
            'conversationRevision': revision,
        }


def delete_turns(conversation_id: str, turn_ids: list[str], *,
                 user_id: Any = 1) -> dict[str, Any]:
    """Delete explicitly named settled visible turns by stable identity."""
    wanted = list(dict.fromkeys(str(item) for item in turn_ids if item))
    if not wanted:
        raise ValueError('turnIds required')
    now = _now_ms()
    with pooled_write_transaction(DOMAIN_CHAT, label='delete v2 turns') as db:
        lock_scoped_sequence(db, 'conversation_turns', conversation_id)
        rows = []
        for turn_id in wanted:
            row = _turn_row(db, conversation_id, turn_id, user_id)
            if row is None:
                raise LifecycleNotFound('Turn not found')
            rows.append(row)
        for row in rows:
            attempt_id = _row_value(row, 'current_attempt_id')
            attempt = _attempt_row(db, attempt_id) if attempt_id else None
            if attempt is not None and _row_value(attempt, 'status') in LIVE_ATTEMPT_STATUSES:
                raise LifecycleConflict(
                    'turn_in_progress', 'A running turn cannot be deleted.',
                    _public_turn(row))
        delete_ids = set(wanted)
        lane_ids = set()
        for row in rows:
            projection = _decoded(_row_value(row, 'projection'), {})
            lane_ids.update(
                item.get('laneId') for item in projection.get('_branchLanes') or []
                if isinstance(item, dict) and item.get('laneId'))
        for lane_id in lane_ids:
            lane_rows = db.execute(
                'SELECT * FROM conversation_turns WHERE conversation_id=? '
                'AND user_id=? AND lane_id=?',
                (conversation_id, user_id, lane_id),
            ).fetchall()
            for lane_row in lane_rows:
                attempt_id = _row_value(lane_row, 'current_attempt_id')
                attempt = _attempt_row(db, attempt_id) if attempt_id else None
                if attempt is not None and _row_value(attempt, 'status') in LIVE_ATTEMPT_STATUSES:
                    raise LifecycleConflict(
                        'lane_busy', 'A child branch is still running.',
                        _public_turn(lane_row))
                delete_ids.add(_row_value(lane_row, 'turn_id'))
        for turn_id in delete_ids:
            attempt_rows = db.execute(
                'SELECT attempt_id FROM generation_attempts WHERE turn_id=?',
                (turn_id,),
            ).fetchall()
            for attempt_row in attempt_rows:
                attempt_id = _row_value(attempt_row, 'attempt_id')
                db.execute('DELETE FROM attempt_events WHERE attempt_id=?', (attempt_id,))
                db.execute(
                    "DELETE FROM scoped_sequences WHERE namespace='attempt_events' "
                    'AND scope_key=?', (attempt_id,))
            db.execute('DELETE FROM generation_attempts WHERE turn_id=?', (turn_id,))
        for turn_id in delete_ids:
            db.execute('DELETE FROM conversation_turns WHERE turn_id=?', (turn_id,))
        revision = _bump_conversation(db, conversation_id, user_id, now)
        return {
            'deletedTurnIds': sorted(delete_ids),
            'conversationRevision': revision,
        }


def read_events(attempt_id: str, *, after: int = 0,
                user_id: Any = 1, limit: int = 1000) -> list[dict[str, Any]]:
    with pooled_db(DOMAIN_CHAT) as db:
        owner = db.execute(
            'SELECT t.user_id FROM generation_attempts a '
            'JOIN conversation_turns t ON t.turn_id=a.turn_id '
            'WHERE a.attempt_id=?', (attempt_id,),
        ).fetchone()
        if owner is None or str(_row_value(owner, 'user_id')) != str(user_id):
            raise LifecycleNotFound('Attempt not found')
        rows = db.execute(
            'SELECT payload FROM attempt_events WHERE attempt_id=? AND seq>? '
            'ORDER BY seq LIMIT ?',
            (attempt_id, int(after or 0), min(max(limit, 1), 5000)),
        ).fetchall()
        return [_decoded(_row_value(row, 'payload'), {}) for row in rows]


def attempt_is_terminal(attempt_id: str, *, user_id: Any = 1) -> bool:
    with pooled_db(DOMAIN_CHAT) as db:
        row = db.execute(
            'SELECT a.status,t.user_id FROM generation_attempts a '
            'JOIN conversation_turns t ON t.turn_id=a.turn_id '
            'WHERE a.attempt_id=?', (attempt_id,),
        ).fetchone()
        if row is None or str(_row_value(row, 'user_id')) != str(user_id):
            raise LifecycleNotFound('Attempt not found')
        return _row_value(row, 'status') not in LIVE_ATTEMPT_STATUSES


def abort_attempt(attempt_id: str, *, user_id: Any = 1) -> dict[str, Any]:
    with pooled_db(DOMAIN_CHAT) as db:
        row = db.execute(
            'SELECT a.*,t.user_id FROM generation_attempts a '
            'JOIN conversation_turns t ON t.turn_id=a.turn_id '
            'WHERE a.attempt_id=?', (attempt_id,),
        ).fetchone()
        if row is None or str(_row_value(row, 'user_id')) != str(user_id):
            raise LifecycleNotFound('Attempt not found')
        task_id = _row_value(row, 'task_id', '')
        status = _row_value(row, 'status')
    if status not in LIVE_ATTEMPT_STATUSES:
        return {'attemptId': attempt_id, 'status': status, 'alreadyTerminal': True}
    task = None
    if task_id:
        from lib.tasks_pkg import tasks, tasks_lock
        with tasks_lock:
            task = tasks.get(task_id)
        if task is not None:
            task['aborted'] = True
            task['_abort_timestamp'] = time.time()
            task['_abort_reason'] = 'v2_attempt_abort'
    if task is None:
        turn = get_turn(
            _row_value(row, 'conversation_id'), _row_value(row, 'turn_id'),
            user_id=user_id)
        projection = turn.get('projection') or {}
        record_task_event(
            {'_attemptId': attempt_id, 'id': task_id, 'status': 'aborted',
             'aborted': True, 'content': projection.get('content') or '',
             'thinking': projection.get('thinking') or '',
             'toolRounds': projection.get('toolRounds') or [],
             'segments': projection.get('segments') or [],
             'model': projection.get('model') or ''},
            {'type': 'aborted'},
        )
    return {'attemptId': attempt_id, 'status': 'abort_signaled'}


def build_api_messages(conversation_id: str, turn_id: str,
                       config: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Project v2 turns into the existing executor's API-ready message form."""
    with pooled_db(DOMAIN_CHAT) as db:
        target = db.execute(
            'SELECT * FROM conversation_turns WHERE conversation_id=? '
            'AND turn_id=?', (conversation_id, turn_id),
        ).fetchone()
        if target is None:
            return None
        lane_id = _row_value(target, 'lane_id', 'main')
        rows: list[Any] = []
        if lane_id != 'main':
            first = db.execute(
                'SELECT parent_turn_id FROM conversation_turns '
                'WHERE conversation_id=? AND lane_id=? ORDER BY ordinal LIMIT 1',
                (conversation_id, lane_id),
            ).fetchone()
            parent_id = _row_value(first, 'parent_turn_id')
            parent = db.execute(
                'SELECT ordinal,lane_id FROM conversation_turns WHERE turn_id=?',
                (parent_id,),
            ).fetchone() if parent_id else None
            if parent is not None:
                rows.extend(db.execute(
                    'SELECT * FROM conversation_turns WHERE conversation_id=? '
                    'AND lane_id=? AND ordinal<=? ORDER BY ordinal',
                    (conversation_id, _row_value(parent, 'lane_id', 'main'),
                     int(_row_value(parent, 'ordinal', 0))),
                ).fetchall())
        rows.extend(db.execute(
            'SELECT * FROM conversation_turns WHERE conversation_id=? '
            'AND lane_id=? AND ordinal<=? ORDER BY ordinal',
            (conversation_id, lane_id, int(_row_value(target, 'ordinal', 0))),
        ).fetchall())
    raw: list[dict[str, Any]] = []
    for row in rows:
        projection = _decoded(_row_value(row, 'projection'), {})
        actor = _row_value(row, 'actor')
        role = 'user' if actor in {'human', 'virtual_user', 'critic'} else 'assistant'
        msg = dict(projection)
        msg['role'] = role
        msg['_turnId'] = _row_value(row, 'turn_id')
        raw.append(msg)
    from lib.tasks_pkg.conv_message_builder import _transform_messages
    exclude_last = bool(config.get('excludeLast'))
    return _transform_messages(raw, config, exclude_last=exclude_last)


def recover_running_attempts() -> int:
    """Atomically settle pre-boot attempts; never starts billable work."""
    now = _now_ms()
    recovered = 0
    with pooled_write_transaction(DOMAIN_CHAT, label='recover v2 attempts') as db:
        rows = db.execute(
            "SELECT a.*,t.user_id,t.projection,t.projection_revision "
            'FROM generation_attempts a JOIN conversation_turns t '
            'ON t.turn_id=a.turn_id AND t.current_attempt_id=a.attempt_id '
            "WHERE a.status IN ('pending','running')",
        ).fetchall()
        for row in rows:
            attempt_id = _row_value(row, 'attempt_id')
            turn_id = _row_value(row, 'turn_id')
            conversation_id = _row_value(row, 'conversation_id')
            projection = _decoded(_row_value(row, 'projection'), {})
            old_revision = int(_row_value(row, 'projection_revision', 0) or 0)
            new_revision = old_revision + 1
            fake_task = {
                'model': _decoded(_row_value(row, 'config'), {}).get('model', ''),
                'content': projection.get('content', ''),
                'thinking': projection.get('thinking', ''),
                'toolRounds': projection.get('toolRounds', []),
            }
            _, settlement = _settlement(
                fake_task, {'type': 'aborted', 'finishReason': 'interrupted'},
                projection)
            settlement['cause'] = 'server_restart'
            db.execute(
                "UPDATE generation_attempts SET status='interrupted', "
                'settled_at=? WHERE attempt_id=? AND status IN '
                "('pending','running')", (now, attempt_id))
            db.execute(
                "UPDATE conversation_turns SET status='interrupted', "
                'projection_revision=?,settlement=?,updated_at=? '
                'WHERE turn_id=? AND current_attempt_id=? '
                'AND projection_revision=?',
                (new_revision, _json(settlement), now, turn_id, attempt_id,
                 old_revision),
            )
            _append_event(
                db, attempt_id=attempt_id, conversation_id=conversation_id,
                turn_id=turn_id, projection_revision=new_revision,
                event_type='terminal_settlement',
                payload={'status': 'interrupted', 'settlement': settlement,
                         'projection': projection},
            )
            _bump_conversation(
                db, conversation_id, _row_value(row, 'user_id'), now)
            recovered += 1
    if recovered:
        logger.warning('[TurnLifecycle] settled %d pre-boot attempt(s) as interrupted',
                       recovered)
    return recovered


def cleanup_superseded_attempts(*, retention_ms: int = 6 * 60 * 60 * 1000,
                                limit: int = 500) -> int:
    """Bounded diagnostic-retention cleanup for replaced attempts."""
    cutoff = _now_ms() - max(int(retention_ms), 0)
    with pooled_write_transaction(
            DOMAIN_CHAT, label='cleanup superseded v2 attempts') as db:
        rows = db.execute(
            "SELECT a.attempt_id FROM generation_attempts a "
            "WHERE a.status='superseded' AND a.superseded_at IS NOT NULL "
            'AND a.superseded_at<? AND NOT EXISTS ('
            'SELECT 1 FROM conversation_turns t '
            'WHERE t.current_attempt_id=a.attempt_id) '
            'ORDER BY a.superseded_at LIMIT ?',
            (cutoff, min(max(int(limit), 1), 5000)),
        ).fetchall()
        ids = [_row_value(row, 'attempt_id') for row in rows]
        for attempt_id in ids:
            db.execute('DELETE FROM attempt_events WHERE attempt_id=?',
                       (attempt_id,))
            db.execute(
                "DELETE FROM scoped_sequences WHERE namespace='attempt_events' "
                'AND scope_key=?', (attempt_id,))
            db.execute('DELETE FROM generation_attempts WHERE attempt_id=?',
                       (attempt_id,))
        return len(ids)


__all__ = [
    'LifecycleConflict', 'LifecycleNotFound', 'TERMINAL_STATUSES',
    'create_turn_pair', 'announce_related_turns',
    'create_attempt', 'claim_attempt_start',
    'bind_task', 'fail_start',
    'record_task_event', 'sync_visible_run_turns',
    'list_turns', 'get_turn', 'get_attempt', 'update_turn_projection',
    'create_branch_lane', 'delete_branch_lane',
    'delete_turns',
    'get_conversation_revision', 'read_events',
    'attempt_is_terminal', 'abort_attempt', 'build_api_messages',
    'recover_running_attempts', 'cleanup_superseded_attempts',
]
