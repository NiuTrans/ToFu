"""Transactional Project Brain event stream and bounded projection.

This module is the storage authority for signal-derived project work.  Every
command locks one explicit ``owner_user_id + normalized project_key`` scope,
appends exactly one semantic event, folds the projection in the same
transaction, and returns a push hint.  Application code never reads these
tables directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import re
import time
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)


PROJECT_BRAIN_VERSION = 1
WORK_HISTORY_LIMIT = 100
ACTIVE_WORK_LIMIT = 100
NARRATIVE_LIMIT = 500
NARRATIVE_TEXT_LIMIT_BYTES = 720
WATCH_LIMIT = 100
CHECKER_VERSION_LIMIT = 128
CHARTER_DECISION_LIMIT = 256
CURSOR_LIMIT = 1000
EVENT_CHECKPOINT_THRESHOLD = 1200
EVENT_CHECKPOINT_TAIL = 600

_WORK_TERMINAL = frozenset({'completed', 'failed', 'cancelled'})
_WORK_STATUSES = frozenset({'active', *_WORK_TERMINAL})
_TRAILING_SEPARATORS = re.compile(r'[/\\]+$')


def _project_identity(payload: Mapping[str, Any]) -> tuple[int, str]:
    owner_user_id = _integer(payload, 'owner_user_id', minimum=1)
    project_key = _TRAILING_SEPARATORS.sub(
        '', _required_text(payload, 'project_key', 4096).strip())
    if not project_key:
        raise StorageError(
            'database_protocol_error', 'Project Brain project_key is empty')
    return owner_user_id, project_key


def _bounded_utf8(value: Any, max_bytes: int) -> str:
    encoded = str(value or '').strip().encode('utf-8', 'replace')
    if len(encoded) <= max_bytes:
        return encoded.decode('utf-8')
    return encoded[:max_bytes].decode('utf-8', 'ignore').rstrip()


def _empty_projection(owner_user_id: int, project_key: str) -> dict[str, Any]:
    return {
        'version': PROJECT_BRAIN_VERSION,
        'ownerUserId': owner_user_id,
        'projectKey': project_key,
        'headSequence': 0,
        'checkpointSequence': 0,
        'workItems': [],
        'narratives': [],
        'charter': {'decisions': []},
        'checkers': [],
        'watch': [],
        'cursors': {},
    }


def _load_projection(
    session: Session,
    owner_user_id: int,
    project_key: str,
) -> dict[str, Any]:
    row = session.fetch_one(
        'SELECT projection_json FROM storage_project_brain_projects '
        'WHERE owner_user_id=? AND project_key=?',
        (owner_user_id, project_key),
    )
    if row is None:
        return _empty_projection(owner_user_id, project_key)
    document = _load(row['projection_json'])
    if not isinstance(document, Mapping):
        raise StorageError(
            'database_integrity', 'Project Brain projection is invalid')
    projection = dict(document)
    if int(projection.get('version') or 0) != PROJECT_BRAIN_VERSION:
        raise StorageError(
            'database_integrity', 'Unsupported Project Brain projection version')
    if (int(projection.get('ownerUserId') or 0) != owner_user_id
            or str(projection.get('projectKey') or '') != project_key):
        raise StorageError(
            'database_integrity', 'Project Brain projection ownership mismatch')
    # Retired attention collection: dropped on load so the next save rewrites
    # the row without it; historical attention_added events stay inert.
    projection.pop('attention', None)
    return projection


def _save_projection(
    session: Session,
    owner_user_id: int,
    project_key: str,
    projection: Mapping[str, Any],
    now_ms: int,
) -> None:
    session.execute(
        'INSERT INTO storage_project_brain_projects('
        'owner_user_id,project_key,head_sequence,checkpoint_sequence,'
        'projection_json,updated_at_ms) VALUES (?,?,?,?,?,?) '
        'ON CONFLICT(owner_user_id,project_key) DO UPDATE SET '
        'head_sequence=excluded.head_sequence,'
        'checkpoint_sequence=excluded.checkpoint_sequence,'
        'projection_json=excluded.projection_json,'
        'updated_at_ms=excluded.updated_at_ms',
        (
            owner_user_id,
            project_key,
            int(projection.get('headSequence') or 0),
            int(projection.get('checkpointSequence') or 0),
            _dump(dict(projection)),
            now_ms,
        ),
    )


def _public_work_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            'id', 'taskId', 'conversationId', 'title', 'trigger', 'status',
            'changedPaths', 'artifacts', 'resultSummary', 'startedAt',
            'finishedAt',
        )
    }


def _public_projection(projection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'version': int(projection.get('version') or PROJECT_BRAIN_VERSION),
        'ownerUserId': int(projection.get('ownerUserId') or 0),
        'projectKey': str(projection.get('projectKey') or ''),
        'headSequence': int(projection.get('headSequence') or 0),
        'checkpointSequence': int(projection.get('checkpointSequence') or 0),
        'workItems': [
            _public_work_item(item)
            for item in projection.get('workItems') or ()
            if isinstance(item, Mapping)
        ],
        'narratives': [dict(item) for item in projection.get('narratives') or ()
                       if isinstance(item, Mapping)],
        'charter': dict(projection.get('charter') or {'decisions': []}),
        'checkers': [dict(item) for item in projection.get('checkers') or ()
                     if isinstance(item, Mapping)],
        'watch': [dict(item) for item in projection.get('watch') or ()
                  if isinstance(item, Mapping)],
    }


def _bounded_work_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = [item for item in items if item.get('status') == 'active']
    terminal = [item for item in items if item.get('status') in _WORK_TERMINAL]
    active.sort(key=lambda item: int(item.get('startedAt') or 0))
    terminal.sort(key=lambda item: int(item.get('finishedAt') or 0))
    return active[-ACTIVE_WORK_LIMIT:] + terminal[-WORK_HISTORY_LIMIT:]


def _work_index(projection: Mapping[str, Any], work_id: str) -> int:
    for index, item in enumerate(projection.get('workItems') or ()):
        if isinstance(item, Mapping) and str(item.get('id') or '') == work_id:
            return index
    return -1


def _string_list(
    value: Any,
    *,
    field: str,
    limit: int,
    item_limit: int,
    allow_empty: bool = True,
) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise StorageError(
            'database_protocol_error', f'Invalid Project Brain {field}')
    result: list[str] = []
    for item in value:
        if (not isinstance(item, str) or len(item) > item_limit
                or (not allow_empty and not item)):
            raise StorageError(
                'database_protocol_error', f'Invalid Project Brain {field}')
        result.append(item)
    return result


def _artifacts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 100:
        raise StorageError(
            'database_protocol_error', 'Invalid Project Brain artifacts')
    result = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise StorageError(
                'database_protocol_error', 'Invalid Project Brain artifact')
        artifact = {
            'id': _required_text(raw, 'id', 256),
            'title': str(raw.get('title') or ''),
            'format': str(raw.get('format') or ''),
            'path': str(raw.get('path') or ''),
        }
        if (len(artifact['title']) > 500 or len(artifact['format']) > 128
                or len(artifact['path']) > 4096):
            raise StorageError(
                'database_protocol_error', 'Invalid Project Brain artifact')
        result.append(artifact)
    return result


def _validated_work_item(value: Mapping[str, Any]) -> dict[str, Any]:
    task_id = _required_text(value, 'taskId', 256)
    work_id = _required_text(value, 'id', 128)
    expected_id = 'pw_' + hashlib.sha256(
        task_id.encode('utf-8', 'replace')).hexdigest()[:24]
    if work_id != expected_id:
        raise StorageError(
            'database_protocol_error', 'Project work id is not deterministic')
    trigger = _required_text(value, 'trigger', 32)
    if trigger not in {'todo_write', 'file_write', 'isolated_workspace'}:
        raise StorageError(
            'database_protocol_error', 'Invalid Project work trigger')
    status = _required_text(value, 'status', 32)
    if status != 'active':
        raise StorageError(
            'database_protocol_error', 'New Project work must be active')
    summary = value.get('resultSummary')
    if not isinstance(summary, str) or len(summary) > 4000:
        raise StorageError(
            'database_protocol_error', 'Invalid Project work resultSummary')
    finished = value.get('finishedAt')
    if finished is not None:
        raise StorageError(
            'database_protocol_error', 'New Project work cannot be terminal')
    priority = int(value.get('_titlePriority') or 0)
    if priority < 1 or priority > 1000:
        raise StorageError(
            'database_protocol_error', 'Invalid Project work title priority')
    return {
        'id': work_id,
        'taskId': task_id,
        'conversationId': _required_text(value, 'conversationId', 256),
        'title': _required_text(value, 'title', 500),
        'trigger': trigger,
        'status': status,
        'changedPaths': _string_list(
            value.get('changedPaths'), field='changedPaths', limit=200,
            item_limit=4096, allow_empty=False),
        'artifacts': _artifacts(value.get('artifacts')),
        'resultSummary': summary,
        'startedAt': _integer(value, 'startedAt', minimum=0),
        'finishedAt': None,
        '_titlePriority': priority,
        '_titleRefined': bool(value.get('_titleRefined', False)),
    }


def _validated_checker(value: Mapping[str, Any]) -> dict[str, Any]:
    argv = _string_list(
        value.get('argv'), field='checker argv', limit=32,
        item_limit=4096, allow_empty=False)
    globs = _string_list(
        value.get('pathGlobs'), field='checker pathGlobs', limit=64,
        item_limit=4096, allow_empty=False)
    if not argv or not globs or not isinstance(value.get('enabled'), bool):
        raise StorageError(
            'database_protocol_error', 'Invalid Checker definition')
    return {
        'checkerId': _required_text(value, 'checkerId', 128),
        'version': _integer(value, 'version', minimum=1),
        'label': _required_text(value, 'label', 256),
        'argv': argv,
        'cwd': _required_text(value, 'cwd', 4096),
        'pathGlobs': globs,
        'timeoutMs': _integer(
            value, 'timeoutMs', minimum=100, maximum=3_600_000),
        'enabled': value['enabled'],
    }


def _validated_watch_item(value: Mapping[str, Any]) -> dict[str, Any]:
    status = _required_text(value, 'status', 32)
    if status not in {'active', 'resolved'}:
        raise StorageError(
            'database_protocol_error', 'Invalid Project Watch status')
    latest = value.get('latestResult')
    if latest is not None and not isinstance(latest, Mapping):
        raise StorageError(
            'database_protocol_error', 'Invalid Project Watch latestResult')
    latest_result = None
    if isinstance(latest, Mapping):
        latest_text = str(latest.get('text') or '')
        latest_trigger = str(latest.get('trigger') or '')
        if len(latest_text) > 4000 or len(latest_trigger) > 64:
            raise StorageError(
                'database_protocol_error',
                'Invalid Project Watch latestResult')
        latest_result = {
            'text': latest_text,
            'trigger': latest_trigger,
            'timestamp': _integer(latest, 'timestamp', minimum=0),
        }
    return {
        'id': _required_text(value, 'id', 128),
        'kind': _required_text(value, 'kind', 64),
        'text': _required_text(value, 'text', 4000),
        'status': status,
        'sourceConversationId': str(
            value.get('sourceConversationId') or '')[:256],
        'createdAt': _integer(value, 'createdAt', minimum=0),
        'updatedAt': _integer(value, 'updatedAt', minimum=0),
        'latestResult': latest_result,
    }


def _append_narrative(
    projection: dict[str, Any],
    *,
    sequence: int,
    kind: str,
    text: str,
    timestamp: int,
    work_id: str = '',
    conversation_id: str = '',
) -> None:
    entry = {
        'sequence': sequence,
        'kind': kind[:64] or 'note',
        # One row must fit the 900-token page even for non-ASCII text. One
        # token per UTF-8 byte is deliberately conservative; delivery never
        # clips a stored row and therefore never acknowledges unseen content.
        'text': _bounded_utf8(text, NARRATIVE_TEXT_LIMIT_BYTES),
        'timestamp': timestamp,
    }
    if work_id:
        entry['workId'] = work_id
    if conversation_id:
        entry['conversationId'] = conversation_id
    narratives = [dict(item) for item in projection.get('narratives') or ()
                  if isinstance(item, Mapping)]
    narratives.append(entry)
    projection['narratives'] = narratives[-NARRATIVE_LIMIT:]


def _fold_event(projection: dict[str, Any], event: Mapping[str, Any]) -> None:
    kind = str(event.get('kind') or '')
    payload = event.get('payload')
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    sequence = int(event.get('projectSequence') or 0)
    timestamp = int(event.get('timestamp') or 0)

    if kind == 'work_started':
        item = dict(payload.get('workItem') or {})
        if str(item.get('status') or '') != 'active':
            raise StorageError(
                'database_protocol_error', 'New Project work must be active')
        work_id = str(item.get('id') or '')
        if not work_id:
            raise StorageError(
                'database_protocol_error', 'Project work id is required')
        if _work_index(projection, work_id) < 0:
            projection['workItems'] = _bounded_work_items(
                [*projection.get('workItems', []), item])
    elif kind == 'work_title_refined':
        work_id = str(payload.get('workId') or '')
        index = _work_index(projection, work_id)
        if index >= 0:
            item = dict(projection['workItems'][index])
            if item.get('status') == 'active' and not item.get('_titleRefined'):
                priority = int(payload.get('titlePriority') or 0)
                if priority > int(item.get('_titlePriority') or 0):
                    item['title'] = str(payload.get('title') or '').strip()[:500]
                    item['_titlePriority'] = priority
                    item['_titleRefined'] = True
                    projection['workItems'][index] = item
    elif kind == 'work_changed':
        work_id = str(payload.get('workId') or '')
        index = _work_index(projection, work_id)
        if index >= 0:
            item = dict(projection['workItems'][index])
            if item.get('status') == 'active':
                paths = [str(path).strip()[:4096]
                         for path in payload.get('changedPaths') or ()
                         if str(path).strip()]
                artifacts = [dict(value) for value in payload.get('artifacts') or ()
                             if isinstance(value, Mapping)]
                item['changedPaths'] = list(dict.fromkeys(
                    [*(item.get('changedPaths') or ()), *paths]))[-200:]
                artifact_by_id = {
                    str(value.get('id') or value.get('path') or index): value
                    for index, value in enumerate(item.get('artifacts') or ())
                    if isinstance(value, Mapping)
                }
                for artifact in artifacts:
                    key = str(artifact.get('id') or artifact.get('path') or '')
                    if key:
                        artifact_by_id[key] = artifact
                item['artifacts'] = list(artifact_by_id.values())[-100:]
                projection['workItems'][index] = item
    elif kind == 'work_finished':
        work_id = str(payload.get('workId') or '')
        index = _work_index(projection, work_id)
        if index >= 0:
            item = dict(projection['workItems'][index])
            status = str(payload.get('status') or '')
            if item.get('status') == 'active' and status in _WORK_TERMINAL:
                item['status'] = status
                item['resultSummary'] = str(
                    payload.get('resultSummary') or '').strip()[:4000]
                item['finishedAt'] = timestamp
                projection['workItems'][index] = item
                projection['workItems'] = _bounded_work_items(
                    list(projection['workItems']))
                has_output = bool(item.get('changedPaths') or item.get('artifacts'))
                if has_output or status in {'failed', 'cancelled'}:
                    summary = item['resultSummary'] or (
                        f"{item.get('title') or work_id}: {status}")
                    _append_narrative(
                        projection,
                        sequence=sequence,
                        kind='work_result',
                        text=summary,
                        timestamp=timestamp,
                        work_id=work_id,
                        conversation_id=str(item.get('conversationId') or ''),
                    )
    elif kind == 'narrative_added':
        _append_narrative(
            projection,
            sequence=sequence,
            kind=str(payload.get('narrativeKind') or 'note'),
            text=str(payload.get('text') or ''),
            timestamp=timestamp,
            work_id=str(payload.get('workId') or ''),
            conversation_id=str(payload.get('conversationId') or ''),
        )
    elif kind == 'checker_registered':
        definition = dict(payload.get('definition') or {})
        checkers = [dict(item) for item in projection.get('checkers') or ()
                    if isinstance(item, Mapping)]
        key = (str(definition.get('checkerId') or ''),
               int(definition.get('version') or 0))
        if not any((str(item.get('checkerId') or ''),
                    int(item.get('version') or 0)) == key for item in checkers):
            checkers.append(definition)
        projection['checkers'] = checkers
    elif kind == 'decision_promoted':
        decision = dict(payload.get('decision') or {})
        charter = dict(projection.get('charter') or {})
        decisions = [dict(item) for item in charter.get('decisions') or ()
                     if isinstance(item, Mapping)]
        decision_id = str(decision.get('decisionId') or '')
        existing = next((item for item in decisions
                         if str(item.get('decisionId') or '') == decision_id), None)
        if existing is None:
            decisions.append(decision)
        charter['decisions'] = decisions
        projection['charter'] = charter
        _append_narrative(
            projection,
            sequence=sequence,
            kind='decision',
            text=str(decision.get('text') or ''),
            timestamp=timestamp,
            conversation_id=str(
                decision.get('sourceConversationId') or ''),
        )
    elif kind == 'checker_result':
        result = dict(payload.get('result') or {})
        checker_ref = dict(result.get('checkerRef') or {})
        decision_id = str(payload.get('decisionId') or '')
        charter = dict(projection.get('charter') or {})
        decisions = [dict(item) for item in charter.get('decisions') or ()
                     if isinstance(item, Mapping)]
        for decision in decisions:
            ref = decision.get('checkerRef') or {}
            same_checker = (
                str(ref.get('id') or '') == str(checker_ref.get('id') or '')
                and int(ref.get('version') or 0)
                == int(checker_ref.get('version') or 0)
            )
            if same_checker and (
                    not decision_id
                    or str(decision.get('decisionId') or '') == decision_id):
                decision['latestVerification'] = result
        charter['decisions'] = decisions
        projection['charter'] = charter
        if not bool(result.get('ok')):
            label = str(result.get('label') or checker_ref.get('id') or 'Checker')
            reason = str(result.get('summary') or 'checker failed')
            text = f'{label}: {reason}'
            work_id = str(result.get('workId') or '')
            _append_narrative(
                projection,
                sequence=sequence,
                kind='checker_failed',
                text=text,
                timestamp=timestamp,
                work_id=work_id,
            )
    elif kind.startswith('watch_'):
        watch = [dict(item) for item in projection.get('watch') or ()
                 if isinstance(item, Mapping)]
        item = dict(payload.get('item') or {})
        item_id = str(item.get('id') or payload.get('itemId') or '')
        previous = next((row for row in watch
                         if str(row.get('id') or '') == item_id), {})
        if kind == 'watch_deleted':
            watch = [row for row in watch if str(row.get('id') or '') != item_id]
            narrative_text = f"Watch removed: {previous.get('text') or item_id}"
            source_conversation_id = str(
                previous.get('sourceConversationId') or '')
        else:
            watch = [row for row in watch if str(row.get('id') or '') != item_id]
            watch.append(item)
            action = ('resolved' if item.get('status') == 'resolved'
                      else ('added' if kind == 'watch_added' else 'updated'))
            narrative_text = f"Watch {action}: {item.get('text') or item_id}"
            source_conversation_id = str(
                item.get('sourceConversationId') or '')
        projection['watch'] = watch
        _append_narrative(
            projection,
            sequence=sequence,
            kind=kind,
            text=narrative_text,
            timestamp=timestamp,
            conversation_id=source_conversation_id,
        )
    elif kind in {'cursor_initialized', 'cursor_confirmed'}:
        conversation_id = str(payload.get('conversationId') or '')
        if conversation_id:
            cursors = dict(projection.get('cursors') or {})
            current = dict(cursors.get(conversation_id) or {})
            requested = int(payload.get('deliveredSequence') or sequence)
            current['deliveredSequence'] = max(
                int(current.get('deliveredSequence') or 0), requested)
            current['updatedAt'] = timestamp
            cursors[conversation_id] = current
            if len(cursors) > CURSOR_LIMIT:
                ordered = sorted(
                    cursors.items(),
                    key=lambda pair: int((pair[1] or {}).get('updatedAt') or 0),
                )
                cursors = dict(ordered[-CURSOR_LIMIT:])
            projection['cursors'] = cursors
    elif kind == 'legacy_migrated':
        projection['watch'] = [dict(item) for item in payload.get('watch') or ()
                               if isinstance(item, Mapping)][-WATCH_LIMIT:]
    elif kind == 'projection_checkpoint':
        projection['checkpointSequence'] = sequence

    projection['headSequence'] = sequence


def _append_event_and_fold(
    session: Session,
    owner_user_id: int,
    project_key: str,
    projection: dict[str, Any],
    *,
    kind: str,
    payload: Mapping[str, Any],
    timestamp: int,
) -> dict[str, Any]:
    from lib.task_event_contract import PROJECT_BRAIN_STREAM_KIND

    sequence = int(projection.get('headSequence') or 0) + 1
    event = {
        'ownerUserId': owner_user_id,
        'projectKey': project_key,
        'projectSequence': sequence,
        'kind': kind,
        'timestamp': timestamp,
        'payload': dict(payload),
    }
    task_id = f'project-brain:{owner_user_id}:{project_key}'
    count = session.execute(
        'INSERT INTO storage_events('
        'task_id,sequence,stream_kind,event_type,event_kind,owner_user_id,'
        'project_key,project_sequence,event_json,created_at_ms) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (
            task_id, sequence, PROJECT_BRAIN_STREAM_KIND,
            'project_brain', kind, owner_user_id, project_key, sequence,
            _dump(event), timestamp,
        ),
    )
    if int(count or 0) != 1:
        raise StorageError(
            'database_conflict', 'Project Brain sequence allocation conflict')
    _fold_event(projection, event)
    _save_projection(
        session, owner_user_id, project_key, projection, timestamp)
    if (sequence - int(projection.get('checkpointSequence') or 0)
            >= EVENT_CHECKPOINT_THRESHOLD):
        _write_projection_checkpoint(
            session, owner_user_id, project_key, projection, timestamp)
    return event


def _write_projection_checkpoint(
    session: Session,
    owner_user_id: int,
    project_key: str,
    projection: dict[str, Any],
    timestamp: int,
) -> None:
    """Persist one rebuild snapshot and reclaim its reconstructible prefix."""
    from lib.task_event_contract import PROJECT_BRAIN_STREAM_KIND

    sequence = int(projection.get('headSequence') or 0) + 1
    snapshot = dict(projection)
    snapshot['headSequence'] = sequence
    snapshot['checkpointSequence'] = sequence
    event = {
        'ownerUserId': owner_user_id,
        'projectKey': project_key,
        'projectSequence': sequence,
        'kind': 'projection_checkpoint',
        'timestamp': timestamp,
        'payload': {'snapshot': snapshot},
    }
    task_id = f'project-brain:{owner_user_id}:{project_key}'
    session.execute(
        'INSERT INTO storage_events('
        'task_id,sequence,stream_kind,event_type,event_kind,owner_user_id,'
        'project_key,project_sequence,event_json,created_at_ms) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (
            task_id, sequence, PROJECT_BRAIN_STREAM_KIND,
            'project_brain', 'projection_checkpoint', owner_user_id,
            project_key, sequence, _dump(event), timestamp,
        ),
    )
    projection.clear()
    projection.update(snapshot)
    _save_projection(
        session, owner_user_id, project_key, projection, timestamp)
    session.execute(
        'DELETE FROM storage_events WHERE owner_user_id=? AND project_key=? '
        'AND project_sequence>0 AND project_sequence<=?',
        (owner_user_id, project_key, sequence - EVENT_CHECKPOINT_TAIL),
    )


def _command_result(
    projection: Mapping[str, Any], event: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        'ok': True,
        'event': dict(event) if event is not None else None,
        'projection': _public_projection(projection),
        'pushHint': {
            'type': 'project_brain_changed',
            'projectSequence': int(projection.get('headSequence') or 0),
        },
    }


def _project_brain_get(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, project_key = _project_identity(payload)
    return _public_projection(
        _load_projection(session, owner_user_id, project_key))


def _project_brain_command(
    session: Session,
    payload: Mapping[str, Any],
    action: str,
) -> Any:
    owner_user_id, project_key = _project_identity(payload)
    session.lock_key('project_brain.project', f'{owner_user_id}:{project_key}')
    projection = _load_projection(session, owner_user_id, project_key)
    now_ms = _integer(
        payload, 'timestamp', default=int(time.time() * 1000), minimum=0)

    if action == 'work.start':
        work_item = payload.get('work_item')
        if not isinstance(work_item, Mapping):
            raise StorageError(
                'database_protocol_error', 'work_item must be an object')
        validated_work = _validated_work_item(work_item)
        work_id = validated_work['id']
        existing_index = _work_index(projection, work_id)
        if existing_index >= 0:
            existing_work = projection['workItems'][existing_index]
            if (str(existing_work.get('taskId') or '')
                    != validated_work['taskId']
                    or str(existing_work.get('conversationId') or '')
                    != validated_work['conversationId']):
                raise StorageError(
                    'database_conflict',
                    'Project work ownership is immutable')
            return _command_result(projection, None)
        active_count = sum(
            1 for item in projection.get('workItems') or ()
            if isinstance(item, Mapping) and item.get('status') == 'active')
        if active_count >= ACTIVE_WORK_LIMIT:
            raise StorageError(
                'storage_payload_too_large', 'Project active-work limit reached')
        event_payload = {'workItem': validated_work}
        kind = 'work_started'
    elif action == 'work.refine':
        work_id = _required_text(payload, 'work_id', 128)
        index = _work_index(projection, work_id)
        if index < 0:
            raise StorageError('database_not_found', 'Project work item not found')
        item = projection['workItems'][index]
        title = _required_text(payload, 'title', 500).strip()
        priority = _integer(payload, 'title_priority', minimum=1, maximum=1000)
        if (item.get('status') != 'active' or item.get('_titleRefined')
                or priority <= int(item.get('_titlePriority') or 0)):
            return _command_result(projection, None)
        event_payload = {
            'workId': work_id, 'title': title, 'titlePriority': priority}
        kind = 'work_title_refined'
    elif action == 'work.change':
        work_id = _required_text(payload, 'work_id', 128)
        index = _work_index(projection, work_id)
        if index < 0:
            raise StorageError('database_not_found', 'Project work item not found')
        event_payload = {
            'workId': work_id,
            'changedPaths': _string_list(
                payload.get('changed_paths') or [], field='changedPaths',
                limit=200, item_limit=4096, allow_empty=False),
            'artifacts': _artifacts(payload.get('artifacts') or []),
        }
        kind = 'work_changed'
    elif action == 'work.finish':
        work_id = _required_text(payload, 'work_id', 128)
        index = _work_index(projection, work_id)
        if index < 0:
            raise StorageError('database_not_found', 'Project work item not found')
        status = _required_text(payload, 'status', 32)
        if status not in _WORK_TERMINAL:
            raise StorageError(
                'database_protocol_error', 'Invalid Project work terminal status')
        if projection['workItems'][index].get('status') in _WORK_TERMINAL:
            return _command_result(projection, None)
        event_payload = {
            'workId': work_id,
            'status': status,
            'resultSummary': str(payload.get('result_summary') or '')[:4000],
        }
        kind = 'work_finished'
    elif action == 'narrative.add':
        narrative_text = _bounded_utf8(
            _required_text(payload, 'text', 720),
            NARRATIVE_TEXT_LIMIT_BYTES,
        )
        event_payload = {
            'narrativeKind': _required_text(payload, 'kind', 64),
            'text': narrative_text,
            'workId': str(payload.get('work_id') or '')[:128],
            'conversationId': str(payload.get('conversation_id') or '')[:256],
        }
        kind = 'narrative_added'
    elif action == 'checker.register':
        definition = payload.get('definition')
        if not isinstance(definition, Mapping):
            raise StorageError(
                'database_protocol_error', 'Checker definition must be an object')
        validated_definition = _validated_checker(definition)
        checker_id = validated_definition['checkerId']
        version = validated_definition['version']
        existing = next((item for item in projection.get('checkers') or ()
                         if str(item.get('checkerId') or '') == checker_id
                         and int(item.get('version') or 0) == version), None)
        if existing is not None:
            if dict(existing) != validated_definition:
                raise StorageError(
                    'database_conflict', 'Checker versions are immutable')
            return _command_result(projection, None)
        if len(projection.get('checkers') or ()) >= CHECKER_VERSION_LIMIT:
            raise StorageError(
                'storage_payload_too_large', 'Project checker-version limit reached')
        event_payload = {'definition': validated_definition}
        kind = 'checker_registered'
    elif action == 'decision.promote':
        decision = payload.get('decision')
        if not isinstance(decision, Mapping):
            raise StorageError(
                'database_protocol_error', 'Decision must be an object')
        checker_ref = decision.get('checkerRef')
        if not isinstance(checker_ref, Mapping):
            raise StorageError(
                'database_protocol_error', 'Decision checkerRef is required')
        checker_id = _required_text(checker_ref, 'id', 128)
        version = _integer(checker_ref, 'version', minimum=1)
        registered = any(
            str(item.get('checkerId') or '') == checker_id
            and int(item.get('version') or 0) == version
            for item in projection.get('checkers') or ()
        )
        if not registered:
            raise StorageError(
                'database_not_found', 'Decision references an unknown checker version')
        decision_id = _required_text(decision, 'decisionId', 128)
        _required_text(decision, 'text', 4000)
        _required_text(decision, 'sourceConversationId', 256)
        _required_text(decision, 'sourceTurnId', 256)
        if ('latestVerification' not in decision
                or (decision.get('latestVerification') is not None
                    and not isinstance(
                        decision.get('latestVerification'), Mapping))):
            raise StorageError(
                'database_protocol_error',
                'Decision latestVerification must be null or an object')
        existing_decision = next((
            item for item in (projection.get('charter') or {}).get('decisions') or ()
            if str(item.get('decisionId') or '') == decision_id
        ), None)
        if existing_decision is not None:
            if dict(existing_decision) != dict(decision):
                raise StorageError(
                    'database_conflict', 'Charter decisions are immutable')
            return _command_result(projection, None)
        if len((projection.get('charter') or {}).get('decisions') or ()) \
                >= CHARTER_DECISION_LIMIT:
            raise StorageError(
                'storage_payload_too_large', 'Project Charter decision limit reached')
        event_payload = {'decision': dict(decision)}
        kind = 'decision_promoted'
    elif action == 'checker.result':
        result = payload.get('result')
        if not isinstance(result, Mapping):
            raise StorageError(
                'database_protocol_error', 'Checker result must be an object')
        checker_ref = result.get('checkerRef')
        if not isinstance(checker_ref, Mapping):
            raise StorageError(
                'database_protocol_error', 'Checker result ref is required')
        checker_id = _required_text(checker_ref, 'id', 128)
        version = _integer(checker_ref, 'version', minimum=1)
        if not any(
                str(item.get('checkerId') or '') == checker_id
                and int(item.get('version') or 0) == version
                for item in projection.get('checkers') or ()):
            raise StorageError(
                'database_not_found', 'Checker result version is not registered')
        if (not isinstance(result.get('ok'), bool)
                or not isinstance(result.get('timedOut'), bool)
                or len(str(result.get('output') or '')) > 4000
                or len(str(result.get('summary') or '')) > 1000):
            raise StorageError(
                'database_protocol_error', 'Invalid Checker result')
        exit_code = result.get('exitCode')
        if (exit_code is not None
                and (not isinstance(exit_code, int)
                     or isinstance(exit_code, bool))):
            raise StorageError(
                'database_protocol_error', 'Invalid Checker exitCode')
        normalized_result = {
            'checkerRef': {'id': checker_id, 'version': version},
            'label': _required_text(result, 'label', 256),
            'ok': result['ok'],
            'exitCode': exit_code,
            'timedOut': result['timedOut'],
            'durationMs': _integer(result, 'durationMs', minimum=0),
            'reason': _required_text(result, 'reason', 64),
            'summary': str(result.get('summary') or ''),
            'output': str(result.get('output') or ''),
            'workId': str(result.get('workId') or '')[:128],
            'timestamp': _integer(result, 'timestamp', minimum=0),
        }
        event_payload = {
            'result': normalized_result,
            'decisionId': str(payload.get('decision_id') or '')[:128],
        }
        kind = 'checker_result'
    elif action.startswith('watch.'):
        item = payload.get('item')
        item_id = str(payload.get('item_id') or '')
        if action != 'watch.delete' and not isinstance(item, Mapping):
            raise StorageError(
                'database_protocol_error', 'Watch item must be an object')
        if action == 'watch.delete':
            item_id = _required_text(payload, 'item_id', 128)
            if not any(str(row.get('id') or '') == item_id
                       for row in projection.get('watch') or ()):
                raise StorageError(
                    'database_not_found', 'Project Watch item not found')
            event_payload = {'itemId': item_id}
            kind = 'watch_deleted'
        else:
            validated_item = _validated_watch_item(item)
            candidate_id = validated_item['id']
            exists = any(
                str(row.get('id') or '') == candidate_id
                for row in projection.get('watch') or ()
                if isinstance(row, Mapping)
            )
            if action == 'watch.add' and not exists \
                    and len(projection.get('watch') or ()) >= WATCH_LIMIT:
                raise StorageError(
                    'storage_payload_too_large', 'Project Watch item limit reached')
            if action == 'watch.add' and exists:
                current = next(row for row in projection.get('watch') or ()
                               if str(row.get('id') or '') == candidate_id)
                if dict(current) != validated_item:
                    raise StorageError(
                        'database_conflict', 'Project Watch item already exists')
                return _command_result(projection, None)
            if action == 'watch.update' and not exists:
                raise StorageError(
                    'database_not_found', 'Project Watch item not found')
            event_payload = {'item': validated_item}
            kind = 'watch_updated' if action == 'watch.update' else 'watch_added'
    elif action == 'cursor.confirm':
        conversation_id = _required_text(payload, 'conversation_id', 256)
        delivered = _integer(payload, 'delivered_sequence', minimum=0)
        current = dict((projection.get('cursors') or {}).get(conversation_id) or {})
        if delivered <= int(current.get('deliveredSequence') or 0):
            return _command_result(projection, None)
        event_payload = {
            'conversationId': conversation_id,
            'deliveredSequence': min(
                delivered, int(projection.get('headSequence') or 0)),
        }
        kind = 'cursor_confirmed'
    else:
        raise StorageError(
            'database_protocol_error', f'Unknown Project Brain action: {action}')

    event = _append_event_and_fold(
        session,
        owner_user_id,
        project_key,
        projection,
        kind=kind,
        payload=event_payload,
        timestamp=now_ms,
    )
    return _command_result(projection, event)


def _project_brain_work_start(session: Session, payload: Mapping[str, Any]) -> Any:
    return _project_brain_command(session, payload, 'work.start')


def _project_brain_work_refine(session: Session, payload: Mapping[str, Any]) -> Any:
    return _project_brain_command(session, payload, 'work.refine')


def _project_brain_work_change(session: Session, payload: Mapping[str, Any]) -> Any:
    return _project_brain_command(session, payload, 'work.change')


def _project_brain_work_finish(session: Session, payload: Mapping[str, Any]) -> Any:
    return _project_brain_command(session, payload, 'work.finish')


def _project_brain_narrative_add(session: Session, payload: Mapping[str, Any]) -> Any:
    return _project_brain_command(session, payload, 'narrative.add')


def _project_brain_checker_register(session: Session, payload: Mapping[str, Any]) -> Any:
    return _project_brain_command(session, payload, 'checker.register')


def _project_brain_checker_result(session: Session, payload: Mapping[str, Any]) -> Any:
    return _project_brain_command(session, payload, 'checker.result')


def _project_brain_decision_promote(session: Session, payload: Mapping[str, Any]) -> Any:
    return _project_brain_command(session, payload, 'decision.promote')


def _project_brain_watch_add(session: Session, payload: Mapping[str, Any]) -> Any:
    return _project_brain_command(session, payload, 'watch.add')


def _project_brain_watch_update(session: Session, payload: Mapping[str, Any]) -> Any:
    return _project_brain_command(session, payload, 'watch.update')


def _project_brain_watch_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    return _project_brain_command(session, payload, 'watch.delete')


def _project_brain_cursor_prepare(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, project_key = _project_identity(payload)
    conversation_id = _required_text(payload, 'conversation_id', 256)
    session.lock_key('project_brain.project', f'{owner_user_id}:{project_key}')
    projection = _load_projection(session, owner_user_id, project_key)
    cursors = projection.get('cursors') or {}
    if conversation_id not in cursors:
        now_ms = _integer(
            payload, 'timestamp', default=int(time.time() * 1000), minimum=0)
        event = _append_event_and_fold(
            session,
            owner_user_id,
            project_key,
            projection,
            kind='cursor_initialized',
            payload={
                'conversationId': conversation_id,
                'deliveredSequence': int(projection.get('headSequence') or 0) + 1,
            },
            timestamp=now_ms,
        )
        return {
            'initialized': True,
            'entries': [],
            'fromSequence': int(event['projectSequence']),
            'toSequence': int(event['projectSequence']),
            'headSequence': int(event['projectSequence']),
            'deliveryToken': '',
        }

    delivered = int((cursors.get(conversation_id) or {}).get(
        'deliveredSequence') or 0)
    limit = _integer(payload, 'limit', default=12, minimum=1, maximum=12)
    token_budget = _integer(
        payload, 'token_budget', default=900, minimum=1, maximum=900)
    entries: list[dict[str, Any]] = []
    spent = 0
    for item in projection.get('narratives') or ():
        if not isinstance(item, Mapping):
            continue
        sequence = int(item.get('sequence') or 0)
        if sequence <= delivered:
            continue
        # Provider-neutral upper bound: one token per UTF-8 byte plus the row
        # envelope. It stops before the first non-fitting row, preserving
        # strict sequence pagination.
        cost = max(1, len(str(item.get('text') or '').encode('utf-8'))) + 12
        if entries and spent + cost > token_budget:
            break
        if not entries and cost > token_budget:
            raise StorageError(
                'database_integrity',
                'Stored Project narrative exceeds the delivery budget')
        entries.append(dict(item))
        spent += cost
        if len(entries) >= limit:
            break
    to_sequence = int(entries[-1]['sequence']) if entries else delivered
    token = ''
    if entries:
        token = hashlib.sha256(
            f'{owner_user_id}\0{project_key}\0{conversation_id}\0'
            f'{delivered}\0{to_sequence}'.encode('utf-8')
        ).hexdigest()
    return {
        'initialized': False,
        'entries': entries,
        'fromSequence': delivered,
        'toSequence': to_sequence,
        'headSequence': int(projection.get('headSequence') or 0),
        'deliveryToken': token,
    }


def _project_brain_cursor_confirm(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, project_key = _project_identity(payload)
    conversation_id = _required_text(payload, 'conversation_id', 256)
    delivered = _integer(payload, 'delivered_sequence', minimum=0)
    from_sequence = _integer(payload, 'from_sequence', minimum=0)
    expected = hashlib.sha256(
        f'{owner_user_id}\0{project_key}\0{conversation_id}\0'
        f'{from_sequence}\0{delivered}'.encode('utf-8')
    ).hexdigest()
    if str(payload.get('delivery_token') or '') != expected:
        raise StorageError(
            'database_protocol_error', 'Invalid narrative delivery token')
    return _project_brain_command(session, payload, 'cursor.confirm')


def _project_brain_list_active(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id = _integer(payload, 'owner_user_id', minimum=1)
    rows = session.fetch_all(
        'SELECT projection_json FROM storage_project_brain_projects '
        'WHERE owner_user_id=? ORDER BY updated_at_ms DESC,project_key LIMIT 1000',
        (owner_user_id,),
    )
    items = []
    for row in rows:
        projection = _load(row['projection_json'])
        if not isinstance(projection, Mapping):
            continue
        active = [
            _public_work_item(item)
            for item in projection.get('workItems') or ()
            if isinstance(item, Mapping) and item.get('status') == 'active'
        ]
        if active:
            items.append({
                'projectKey': str(projection.get('projectKey') or ''),
                'workItems': active,
            })
    return {'projects': items}


def _project_brain_recovery_snapshot(
    session: Session, _payload: Mapping[str, Any],
) -> Any:
    """System-owned bounded snapshot for post-restart terminal reconciliation."""
    rows = session.fetch_all(
        'SELECT owner_user_id,project_key,projection_json '
        'FROM storage_project_brain_projects '
        'ORDER BY updated_at_ms DESC,owner_user_id,project_key LIMIT 1000')
    projects = []
    for row in rows:
        projection = _load(row['projection_json'])
        if not isinstance(projection, Mapping):
            continue
        active = [
            _public_work_item(item)
            for item in projection.get('workItems') or ()
            if isinstance(item, Mapping) and item.get('status') == 'active'
        ]
        if active:
            projects.append({
                'ownerUserId': int(row['owner_user_id']),
                'projectKey': str(row['project_key']),
                'workItems': active,
            })
    return {'projects': projects, 'capped': len(rows) >= 1000}


def _project_brain_rebuild(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    """Rebuild one projection from its retained checkpoint and event tail.

    This maintenance operation is both a repair seam and the executable proof
    that retained project events remain replayable after prefix reclamation.
    The replacement is written only after the full stream passes ownership and
    monotonic-sequence validation.
    """
    owner_user_id, project_key = _project_identity(payload)
    session.lock_key('project_brain.project', f'{owner_user_id}:{project_key}')
    rows = session.fetch_all(
        'SELECT project_sequence,event_json FROM storage_events '
        'WHERE owner_user_id=? AND project_key=? AND project_sequence>0 '
        'ORDER BY project_sequence',
        (owner_user_id, project_key),
    )
    if not rows:
        raise StorageError(
            'database_not_found', 'Project Brain event stream not found')

    parsed: list[dict[str, Any]] = []
    checkpoint_index = -1
    for index, row in enumerate(rows):
        event = _load(row['event_json'])
        if not isinstance(event, Mapping):
            raise StorageError(
                'database_integrity', 'Project Brain event is invalid')
        document = dict(event)
        sequence = int(document.get('projectSequence') or 0)
        if sequence != int(row['project_sequence'] or 0):
            raise StorageError(
                'database_integrity', 'Project Brain event sequence mismatch')
        if (int(document.get('ownerUserId') or 0) != owner_user_id
                or str(document.get('projectKey') or '') != project_key):
            raise StorageError(
                'database_integrity', 'Project Brain event ownership mismatch')
        parsed.append(document)
        if document.get('kind') == 'projection_checkpoint':
            checkpoint_index = index

    replay_from = 0
    if checkpoint_index >= 0:
        checkpoint = parsed[checkpoint_index]
        snapshot = (checkpoint.get('payload') or {}).get('snapshot')
        if not isinstance(snapshot, Mapping):
            raise StorageError(
                'database_integrity', 'Project Brain checkpoint is invalid')
        projection = dict(snapshot)
        if (int(projection.get('ownerUserId') or 0) != owner_user_id
                or str(projection.get('projectKey') or '') != project_key):
            raise StorageError(
                'database_integrity', 'Project Brain checkpoint ownership mismatch')
        replay_from = checkpoint_index + 1
    else:
        first_sequence = int(parsed[0].get('projectSequence') or 0)
        if first_sequence != 1:
            raise StorageError(
                'database_integrity', 'Project Brain event prefix lacks checkpoint')
        projection = _empty_projection(owner_user_id, project_key)

    previous = int(projection.get('headSequence') or 0)
    for event in parsed[replay_from:]:
        sequence = int(event.get('projectSequence') or 0)
        if sequence != previous + 1:
            raise StorageError(
                'database_integrity', 'Project Brain event stream has a gap')
        _fold_event(projection, event)
        previous = sequence

    now_ms = int(time.time() * 1000)
    _save_projection(
        session, owner_user_id, project_key, projection, now_ms)
    return {
        'ok': True,
        'projectKey': project_key,
        'headSequence': int(projection.get('headSequence') or 0),
        'checkpointSequence': int(projection.get('checkpointSequence') or 0),
        'replayedEvents': len(parsed) - replay_from,
        'projection': _public_projection(projection),
    }


def _table_exists(session: Session, table_name: str) -> bool:
    if session.backend == 'postgres':
        row = session.fetch_one(
            'SELECT 1 AS present FROM information_schema.tables '
            "WHERE table_schema='public' AND table_name=?",
            (table_name,),
        )
    else:
        row = session.fetch_one(
            "SELECT 1 AS present FROM sqlite_master "
            "WHERE type='table' AND name=?", (table_name,))
    return row is not None


def _project_brain_cutover_status(
    session: Session, _payload: Mapping[str, Any],
) -> Any:
    row = session.fetch_one(
        'SELECT meta_value FROM storage_meta WHERE meta_key=?',
        ('project_brain_cutover_v1',),
    )
    return {'complete': bool(row and str(row['meta_value']) == 'complete')}


def _project_brain_cutover(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    """Atomically migrate durable legacy intent and remove old authorities."""
    marker = session.fetch_one(
        'SELECT meta_value FROM storage_meta WHERE meta_key=?',
        ('project_brain_cutover_v1',),
    )
    if marker and str(marker['meta_value']) == 'complete':
        return {'ok': True, 'alreadyComplete': True, 'projects': 0}
    now_ms = _integer(
        payload, 'timestamp', default=int(time.time() * 1000), minimum=0)

    if session.fetch_one(
        'SELECT 1 AS present FROM storage_project_brain_projects LIMIT 1'):
        raise StorageError(
            'database_integrity',
            'Project Brain projection exists before cutover receipt',
        )

    projects: dict[tuple[int, str], dict[str, Any]] = {}
    if _table_exists(session, 'storage_watch_items'):
        watch_rows = session.fetch_all(
            'SELECT * FROM storage_watch_items '
            'ORDER BY user_id,project_path,updated_at,item_id')
        for row in watch_rows:
            owner_user_id = int(row['user_id'])
            normalized_project = _TRAILING_SEPARATORS.sub(
                '', str(row['project_path'] or ''))
            if not normalized_project:
                continue
            latest = None
            if _table_exists(session, 'storage_watch_responses'):
                response = session.fetch_one(
                    'SELECT response,trigger,ts FROM storage_watch_responses '
                    'WHERE item_id=? ORDER BY sequence DESC LIMIT 1',
                    (row['item_id'],),
                )
                if response and str(response['response'] or '').strip():
                    latest = {
                        'text': str(response['response'] or '')[:4000],
                        'trigger': str(response['trigger'] or '')[:64],
                        'timestamp': int(response['ts'] or 0),
                    }
            bucket = projects.setdefault(
                (owner_user_id, normalized_project), {'watch': []})
            bucket['watch'].append({
                'id': str(row['item_id'] or '')[:128],
                'kind': str(row['kind'] or 'concern')[:64],
                'text': str(row['text'] or '')[:4000],
                'status': ('active' if str(row['status'] or 'open') == 'open'
                           else 'resolved'),
                'sourceConversationId': str(row['created_by_conv'] or '')[:256],
                'createdAt': int(row['created_at'] or 0),
                'updatedAt': int(row['updated_at'] or 0),
                'latestResult': latest,
            })

    migrated = 0
    for (owner_user_id, project_key), legacy in sorted(projects.items()):
        projection = _empty_projection(owner_user_id, project_key)
        _append_event_and_fold(
            session, owner_user_id, project_key, projection,
            kind='legacy_migrated',
            payload={'watch': list(legacy['watch'])[-WATCH_LIMIT:]},
            timestamp=now_ms,
        )
        verified = _load_projection(session, owner_user_id, project_key)
        if (len(verified.get('watch') or ())
                != min(len(legacy['watch']), WATCH_LIMIT)):
            raise StorageError(
                'database_integrity', 'Project Brain cutover verification failed')
        migrated += 1

    # Board/Feed/Status execution history is intentionally not imported.
    session.execute(
        "DELETE FROM storage_events WHERE stream_kind IN "
        "('project_feed','project_status')")
    session.execute(
        "DELETE FROM storage_records WHERE namespace='project_charter'")
    retired_queued_turns = 0
    if _table_exists(session, 'storage_queue_items'):
        for row in session.fetch_all(
                'SELECT id,kind,payload_json FROM storage_queue_items'):
            queued_payload = _load(row['payload_json'])
            is_board_kickoff = (
                isinstance(queued_payload, Mapping)
                and bool(queued_payload.get('boardTaskId')))
            if str(row['kind'] or '') == 'peer_msg' or is_board_kickoff:
                retired_queued_turns += int(session.execute(
                    'DELETE FROM storage_queue_items WHERE id=?',
                    (row['id'],),
                ) or 0)
    for table_name in (
        'storage_watch_responses', 'storage_watch_runs', 'storage_watch_items',
        'storage_board_tasks', 'project_events', 'project_tasks',
    ):
        session.execute(f'DROP TABLE IF EXISTS {table_name}')
    session.execute(
        'INSERT INTO storage_meta(meta_key,meta_value) VALUES (?,?) '
        'ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value',
        ('project_brain_cutover_v1', 'complete'),
    )
    return {
        'ok': True, 'alreadyComplete': False, 'projects': migrated,
        'verified': True, 'retiredQueuedTurns': retired_queued_turns,
    }


def _project_brain_relink_scope(
    session: Session,
    owner_user_id: int,
    old_project_key: str,
    new_project_key: str,
) -> bool:
    """Atomically re-key one projection and every retained project event."""
    old_project_key = _TRAILING_SEPARATORS.sub('', old_project_key)
    new_project_key = _TRAILING_SEPARATORS.sub('', new_project_key)
    for key in sorted((old_project_key, new_project_key)):
        session.lock_key('project_brain.project', f'{owner_user_id}:{key}')
    old_row = session.fetch_one(
        'SELECT projection_json,updated_at_ms '
        'FROM storage_project_brain_projects '
        'WHERE owner_user_id=? AND project_key=?',
        (owner_user_id, old_project_key),
    )
    if old_row is None:
        return False
    new_row = session.fetch_one(
        'SELECT projection_json,updated_at_ms '
        'FROM storage_project_brain_projects '
        'WHERE owner_user_id=? AND project_key=?',
        (owner_user_id, new_project_key),
    )
    projection = _load(old_row['projection_json'])
    if not isinstance(projection, Mapping):
        raise StorageError(
            'database_integrity', 'Project Brain projection is invalid')
    document = dict(projection)
    document['projectKey'] = new_project_key
    if new_row is not None:
        destination = _load(new_row['projection_json'])
        if not isinstance(destination, Mapping):
            raise StorageError(
                'database_integrity', 'Project Brain projection is invalid')
        merged = _merge_relinked_projections(
            owner_user_id,
            new_project_key,
            dict(destination),
            document,
        )
        _write_projection_checkpoint(
            session,
            owner_user_id,
            new_project_key,
            merged,
            max(
                int(old_row['updated_at_ms']),
                int(new_row['updated_at_ms']),
                int(time.time() * 1000),
            ),
        )
        # The checkpoint now carries the complete bounded state. The old event
        # stream is reconstructible transport history and must not remain as a
        # second project authority after the physical checkout move.
        session.execute(
            'DELETE FROM storage_events '
            'WHERE owner_user_id=? AND project_key=?',
            (owner_user_id, old_project_key),
        )
        session.execute(
            'DELETE FROM storage_project_brain_projects '
            'WHERE owner_user_id=? AND project_key=?',
            (owner_user_id, old_project_key),
        )
        return True

    new_task_id = f'project-brain:{owner_user_id}:{new_project_key}'
    rows = session.fetch_all(
        'SELECT sequence,stream_kind,event_type,event_kind,event_json,created_at_ms '
        'FROM storage_events WHERE owner_user_id=? AND project_key=? '
        'ORDER BY project_sequence',
        (owner_user_id, old_project_key),
    )
    session.execute(
        'DELETE FROM storage_events WHERE owner_user_id=? AND project_key=?',
        (owner_user_id, old_project_key),
    )
    for row in rows:
        event = _load(row['event_json'])
        if not isinstance(event, Mapping):
            raise StorageError(
                'database_integrity', 'Project Brain event is invalid')
        updated_event = dict(event)
        updated_event['projectKey'] = new_project_key
        sequence = int(updated_event.get('projectSequence') or row['sequence'])
        session.execute(
            'INSERT INTO storage_events('
            'task_id,sequence,stream_kind,event_type,event_kind,owner_user_id,'
            'project_key,project_sequence,event_json,created_at_ms) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (
                new_task_id, int(row['sequence']), row['stream_kind'],
                row['event_type'], row['event_kind'], owner_user_id,
                new_project_key, sequence, _dump(updated_event),
                int(row['created_at_ms']),
            ),
        )
    session.execute(
        'DELETE FROM storage_project_brain_projects '
        'WHERE owner_user_id=? AND project_key=?',
        (owner_user_id, old_project_key),
    )
    _save_projection(
        session, owner_user_id, new_project_key, document,
        int(old_row['updated_at_ms']),
    )
    return True


def _merge_relinked_projections(
    owner_user_id: int,
    project_key: str,
    destination: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    """Merge two bounded projections after stale clients used both paths.

    Project path identity can split briefly while browser tabs from before a
    rename are still running. Both projections belong to the same owner, so a
    relink folds their durable state into one checkpoint instead of choosing a
    winner. Delivery cursors are reconstructible and reset at this identity
    boundary; durable work, narratives, Charter, and Watch remain.
    """
    merged = {**source, **destination}
    merged.update({
        'version': PROJECT_BRAIN_VERSION,
        'ownerUserId': owner_user_id,
        'projectKey': project_key,
        'checkpointSequence': 0,
    })

    work_by_id: dict[str, dict[str, Any]] = {}
    for projection in (destination, source):
        for raw in projection.get('workItems') or ():
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            work_id = str(item.get('id') or '')
            if not work_id:
                continue
            previous = work_by_id.get(work_id)
            if previous is None:
                work_by_id[work_id] = item
                continue
            candidates = (previous, item)
            authoritative = max(
                candidates,
                key=lambda value: (
                    int(value.get('finishedAt') or 0),
                    int(value.get('startedAt') or 0),
                    value.get('status') in _WORK_TERMINAL,
                ),
            )
            combined = dict(authoritative)
            combined['changedPaths'] = list(dict.fromkeys(
                [*(previous.get('changedPaths') or ()),
                 *(item.get('changedPaths') or ())]
            ))[-200:]
            artifacts: dict[str, dict[str, Any]] = {}
            for artifact in [
                *(previous.get('artifacts') or ()),
                *(item.get('artifacts') or ()),
            ]:
                if not isinstance(artifact, Mapping):
                    continue
                key = str(artifact.get('id') or artifact.get('path') or '')
                if key:
                    artifacts[key] = dict(artifact)
            combined['artifacts'] = list(artifacts.values())[-100:]
            work_by_id[work_id] = combined
    merged['workItems'] = _bounded_work_items(list(work_by_id.values()))

    narratives: dict[tuple[Any, ...], dict[str, Any]] = {}
    for projection in (destination, source):
        for raw in projection.get('narratives') or ():
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            key = (
                str(item.get('kind') or ''),
                str(item.get('text') or ''),
                int(item.get('timestamp') or 0),
                str(item.get('workId') or ''),
                str(item.get('conversationId') or ''),
            )
            narratives[key] = item
    narrative_rows = sorted(
        narratives.values(),
        key=lambda item: (
            int(item.get('timestamp') or 0),
            int(item.get('sequence') or 0),
        ),
    )[-NARRATIVE_LIMIT:]
    combined_head = max(
        int(destination.get('headSequence') or 0)
        + int(source.get('headSequence') or 0),
        len(narrative_rows),
    )
    first_sequence = combined_head - len(narrative_rows) + 1
    for offset, item in enumerate(narrative_rows):
        item['sequence'] = first_sequence + offset
    merged['narratives'] = narrative_rows
    merged['headSequence'] = combined_head

    merged['checkers'] = _merge_relinked_immutable_rows(
        destination.get('checkers') or (),
        source.get('checkers') or (),
        identity=lambda item: (
            str(item.get('checkerId') or ''),
            int(item.get('version') or 0),
        ),
        maximum=CHECKER_VERSION_LIMIT,
        field='checkers',
    )
    destination_charter = dict(destination.get('charter') or {})
    source_charter = dict(source.get('charter') or {})
    decisions = _merge_relinked_immutable_rows(
        destination_charter.get('decisions') or (),
        source_charter.get('decisions') or (),
        identity=lambda item: str(item.get('decisionId') or ''),
        maximum=CHARTER_DECISION_LIMIT,
        field='charter decisions',
    )
    merged['charter'] = {
        **source_charter,
        **destination_charter,
        'decisions': decisions,
    }

    watch_by_id: dict[str, dict[str, Any]] = {}
    for projection in (destination, source):
        for raw in projection.get('watch') or ():
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            item_id = str(item.get('id') or '')
            current = watch_by_id.get(item_id)
            if current is None or int(item.get('updatedAt') or 0) >= int(
                current.get('updatedAt') or 0
            ):
                watch_by_id[item_id] = item
    if len(watch_by_id) > WATCH_LIMIT:
        raise StorageError(
            'storage_payload_too_large',
            'Project Brain watch limit reached during relink',
        )
    merged['watch'] = sorted(
        watch_by_id.values(), key=lambda item: int(item.get('updatedAt') or 0)
    )

    merged['cursors'] = {}
    return merged


def _merge_relinked_immutable_rows(
    destination_rows: Any,
    source_rows: Any,
    *,
    identity: Callable[[Mapping[str, Any]], Any],
    maximum: int,
    field: str,
) -> list[dict[str, Any]]:
    rows: dict[Any, dict[str, Any]] = {}
    for values in (destination_rows, source_rows):
        for raw in values:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            key = identity(item)
            previous = rows.get(key)
            if previous is not None and previous != item:
                raise StorageError(
                    'database_conflict',
                    f'Project Brain {field} identity conflict during relink',
                )
            rows[key] = item
    if len(rows) > maximum:
        raise StorageError(
            'storage_payload_too_large',
            f'Project Brain {field} limit reached during relink',
        )
    return list(rows.values())


__all__ = [name for name in globals() if name.startswith('_project_brain_')]
