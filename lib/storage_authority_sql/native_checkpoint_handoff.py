"""Atomic SQL bytes for the shared Rust accepted-attempt handoff validator.

Owner predicates and real row/key locks precede observations. Rust validates
lineage, version, writer death and frozen invoice inside this transaction; SQL
then retires source and creates target together. No journal policy or retry.
"""
import time
import orjson

from lib.storage_authority_sql.adapters.text_size import json_utf8_length_expression
from lib.storage_authority_sql.json_document import JsonBytes
from lib.storage_authority_sql.native_checkpoint_rows import HEADERS, SNAPSHOTS, _refuse
from lib.storage_authority_sql.operations_pkg._turns_core import _stored_object


def _attempt(session, owner, conversation, turn, attempt, maximum, include_config):
    columns = 'a.attempt_id, a.task_id, a.conversation_id, a.turn_id, a.status, a.operation'
    if include_config:
        length = json_utf8_length_expression(session.backend, 'config_json').replace('config_json', 'a.config_json')
        columns += f', CASE WHEN {length} <= {maximum * 6} THEN a.config_json ELSE NULL END AS config_json'
    row = session.fetch_one_for_update(
        f'SELECT {columns} FROM storage_generation_attempts a '
        'WHERE a.attempt_id=? AND a.conversation_id=? AND a.turn_id=? '
        'AND EXISTS (SELECT 1 FROM storage_conversation_turns t '
        'WHERE t.conversation_id=a.conversation_id AND t.turn_id=a.turn_id AND t.user_id=?)',
        (attempt, conversation, turn, owner))
    if row is None:
        return None
    result = {wire: row[column] for wire, column in (
        ('attemptId', 'attempt_id'), ('taskId', 'task_id'), ('conversationId', 'conversation_id'),
        ('turnId', 'turn_id'), ('status', 'status'), ('operation', 'operation'))}
    if include_config:
        if row['config_json'] is None:
            _refuse('database_integrity')
        result['config'] = _stored_object(row['config_json'], 'native accepted attempt config')
        if len(orjson.dumps(result['config'])) > maximum:
            _refuse('database_integrity')
    return result


def handoff(rows, request, deadline_at):
    if rows.validate_handoff is None:
        return {'ok': False, 'error': 'ineligible'}
    if not {'header', 'snapshot', 'source', 'resume'} <= request.keys():
        _refuse()
    source, resume = request['source'], request['resume']
    if (not isinstance(source, dict) or set(source) != {'task_id', 'expected_version', 'header'}
            or not isinstance(resume, dict) or set(resume) != {'conversation_id', 'turn_id', 'source_attempt_id',
                'target_attempt_id', 'source_task_id', 'reserved_micro'}
            or type(source['expected_version']) is not int or not 0 < source['expected_version'] < (1 << 63)-1
            or request['expected_version'] != 0 or source['task_id'] != resume['source_task_id']
            or source['task_id'] == request['task_id']
            or any(not isinstance(resume[name], str) or not resume[name] or len(resume[name].encode()) > rows.identity_bytes
                   for name in resume if name != 'reserved_micro')):
        _refuse()
    target, source_task = request['task_id'], source['task_id']
    for value, task, maximum in [(request['header'], target, request['header_bytes']),
            (request['snapshot'], target, request['snapshot_bytes']),
            (source['header'], source_task, request['header_bytes'])]:
        rows._value(value, task, maximum)
    target_key, source_key = rows.key(target), rows.key(source_task)
    if target_key == source_key:
        _refuse('database_integrity')
    encoded = [JsonBytes(orjson.dumps(rows._envelope(task, value))) for task, value in
               [(source_task, source['header']), (target, request['header']), (target, request['snapshot'])]]

    def transfer(session):
        conversation, turn = resume['conversation_id'], resume['turn_id']
        session.lock_key('turn_conversation', f'{rows.owner}:{conversation}')
        # Worker start updates its attempt before its Turn. Take row locks in
        # that order too; the conversation key serializes accepted successors.
        attempts = {}
        for attempt in sorted({resume['source_attempt_id'], resume['target_attempt_id']}):
            attempts[attempt] = _attempt(session, rows.owner, conversation, turn, attempt,
                rows.document_bytes, attempt == resume['target_attempt_id'])
        if any(value is None for value in attempts.values()):
            return {'ok': False, 'error': 'ineligible'}
        turn_row = session.fetch_one_for_update(
            'SELECT turn_id, conversation_id, current_attempt_id, status FROM storage_conversation_turns '
            'WHERE conversation_id=? AND turn_id=? AND user_id=?', (conversation, turn, rows.owner))
        if turn_row is None:
            return {'ok': False, 'error': 'ineligible'}
        for key in sorted((source_key, target_key)):
            session.lock_key(HEADERS, key)
        if session.fetch_one('SELECT 1 AS present FROM storage_records '
                'WHERE record_key=? AND namespace IN (?, ?)', (target_key, HEADERS, SNAPSHOTS)) is not None:
            return {'ok': False, 'error': 'conflict'}
        current = rows._header(session, source_task, source_key, request['header_bytes'])
        if current is None:
            return {'ok': False, 'error': 'conflict'}
        snapshot = None
        if request['header'].get('phase') == 'tools_pending':
            saved = session.fetch_one(
                f'SELECT {rows._projection(session, "s", request["snapshot_bytes"])} AS value_json '
                'FROM storage_records s WHERE s.namespace=? AND s.record_key=?', (SNAPSHOTS, source_key))
            if saved is not None:
                snapshot = rows._decode(saved['value_json'], source_task, request['snapshot_bytes'])
        observations = dict(accepted=attempts[resume['target_attempt_id']], source_attempt=attempts[resume['source_attempt_id']],
            turn={wire: turn_row[column] for wire, column in (('turnId', 'turn_id'), ('conversationId', 'conversation_id'),
                  ('currentAttemptId', 'current_attempt_id'), ('status', 'status'))},
            source_header=current['value'], source_version=current['version'], source_snapshot=snapshot)
        error = rows.validate_handoff(request, observations)
        if error is not None:
            return {'ok': False, 'error': error}
        now = max(1, time.time_ns() // 1_000_000)
        rows._write(session, HEADERS, source_key, encoded[0], source['expected_version'] + 1, now)
        rows.delete_snapshot(session, source_key)
        rows._write(session, HEADERS, target_key, encoded[1], 1, now)
        rows._write(session, SNAPSHOTS, target_key, encoded[2], 1, now)
        return {'ok': True, 'version': 1}

    return rows.command(request, transfer, deadline_at)
