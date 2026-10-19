"""Bounded, locked SQL observations for the shared Rust whole Goal authority.

The caller supplies one active Session and authenticated scope. These adapters
project persisted rows; lineage, journal and billing decisions remain in Rust.
"""
import orjson

from lib.storage_authority_sql.adapters.text_size import json_utf8_length_expression, utf8_length_expression
from lib.storage_authority_sql.native_checkpoint_rows import HEADERS, SNAPSHOTS, _refuse
from lib.storage_authority_sql.operations_pkg._common import _load
from lib.storage_authority_sql.operations_pkg._records import _load_record_value
from lib.storage_authority_sql.operations_pkg._runs import _decode_run_error
from lib.storage_authority_sql.operation_domains.billing import _ledger_document


class GoalReads:
    def __init__(self, rows, session, request):
        self.rows, self.session, self.request = rows, session, request
        self.maximum = request['evidence_bytes']
        self._locked_runs = set()

    def bounded(self, column, *, json_column=False):
        length = (json_utf8_length_expression if json_column else utf8_length_expression)(self.session.backend, column)
        return f'CASE WHEN {length} <= {self.maximum * 6} THEN {column} ELSE NULL END AS {column}'

    def decoded(self, raw):
        if raw is None:
            _refuse('database_integrity')
        value = _load(raw)
        if len(orjson.dumps(value)) > self.maximum:
            _refuse('database_integrity')
        return value

    def lock_run(self, run, label):
        identity = (run, label)
        if identity not in self._locked_runs:
            if self.session.fetch_one_for_update('SELECT id FROM orchestration_runs '
                    'WHERE id=? AND user_id=? AND tenant_id=?', (run, self.rows.owner, label)) is None:
                return False
            self._locked_runs.add(identity)
        return True

    def run_row(self, run, label):
        if not self.lock_run(run, label):
            return None
        fields = ', '.join(self.bounded(column) for column in ('definition', 'input', 'error', 'final'))
        return self.session.fetch_one(
            f'SELECT id, user_id, tenant_id, orch_id, name, {fields}, status, created_by, '
            'created_at, updated_at, finished_at, continuity_pending, error IS NULL AS error_missing FROM orchestration_runs '
            'WHERE id=? AND user_id=? AND tenant_id=?', (run, self.rows.owner, label))

    def cursor(self, run, label):
        row = self.session.fetch_one('SELECT COALESCE(MAX(seq), -1)+1 AS cursor FROM orchestration_run_events '
            'WHERE run_id=? AND user_id=? AND tenant_id=?', (run, self.rows.owner, label))
        return row['cursor']

    def event(self, run, label, sequence):
        if not self.lock_run(run, label):
            return None
        row = self.session.fetch_one(f'SELECT {self.bounded("payload")} FROM orchestration_run_events '
            'WHERE run_id=? AND user_id=? AND tenant_id=? AND seq=?', (run, self.rows.owner, label, sequence))
        return self.decoded(row['payload']) if row else None

    def run(self, run, label):
        row = self.run_row(run, label)
        if row is None:
            return None
        if (any(row[column] is None for column in ('definition', 'input', 'final'))
                or (row['error'] is None and not row['error_missing'])):
            _refuse('database_integrity')
        # These are the same stored Goal status/policy events used by _runs.py.
        events = []
        for predicate, order in [("type IN ('goal_run_started','goal_run_transition')", 'DESC'),
                                 ("type='goal_run_started'", 'ASC')]:
            event = self.session.fetch_one(f'SELECT {self.bounded("payload")} FROM orchestration_run_events '
                f'WHERE run_id=? AND user_id=? AND tenant_id=? AND {predicate} ORDER BY seq {order} LIMIT 1',
                (run, self.rows.owner, label))
            events.append(self.decoded(event['payload']) if event else {})
        event, started = events
        core = dict(id=row['id'], owner=row['user_id'], tenant_label=row['tenant_id'],
                    orch_id=row['orch_id'], name=row['name'], definition=self.decoded(row['definition']),
                    input=row['input'], created_by=row['created_by'], created_at=row['created_at'])
        state = dict(continuity_pending=bool(row['continuity_pending']), status=row['status'], final_text=row['final'],
                     error=_decode_run_error(row['error']),
                     updated_at=row['updated_at'], finished_at=row['finished_at'], next_event_sequence=self.cursor(run, label),
                     goal_status=event.get('status', ''), goal_reason=event.get('reason', ''),
                     goal_policy=started.get('policy', {}), goal_outcome=event.get('outcome', {}),
                     goal_stop_report=event.get('stopReport', ''))
        return [core, state]

    def attempt(self, id):
        row = self.session.fetch_one_for_update(
            'SELECT a.attempt_id, a.task_id, a.conversation_id, a.turn_id, a.status, a.operation, a.command_id, '
            + self.bounded('config_json', json_column=True) + ' FROM storage_generation_attempts a '
            'WHERE a.attempt_id=? AND EXISTS (SELECT 1 FROM storage_conversation_turns t '
            'WHERE t.turn_id=a.turn_id AND t.conversation_id=a.conversation_id AND t.user_id=?)', (id, self.rows.owner))
        if row is None:
            return None
        return {**{wire: row[column] for wire, column in (
            ('attemptId', 'attempt_id'), ('taskId', 'task_id'), ('conversationId', 'conversation_id'),
            ('turnId', 'turn_id'), ('status', 'status'), ('operation', 'operation'), ('commandId', 'command_id'))},
            'config': self.decoded(row['config_json'])}

    def turn(self, conversation, id):
        row = self.session.fetch_one_for_update(
            'SELECT turn_id, conversation_id, parent_turn_id, current_attempt_id, status, lane_id, actor, kind '
            'FROM storage_conversation_turns WHERE conversation_id=? AND turn_id=? AND user_id=?',
            (conversation, id, self.rows.owner))
        return None if row is None else {wire: row[column] for wire, column in (
            ('turnId', 'turn_id'), ('conversationId', 'conversation_id'), ('parentTurnId', 'parent_turn_id'),
            ('currentAttemptId', 'current_attempt_id'), ('status', 'status'), ('laneId', 'lane_id'),
            ('actor', 'actor'), ('kind', 'kind'))}

    def record(self, task):
        row = self.session.fetch_one_for_update(f'SELECT {self.bounded("value_json", json_column=True)}, '
            'version, updated_at_ms FROM storage_records WHERE namespace=? AND record_key=?', ('task_results', task))
        if row is None:
            return None
        raw = self.decoded(row['value_json'])
        if not isinstance(raw, dict) or type(raw.get('user_id')) is not int or raw['user_id'] != self.rows.owner:
            return None
        value = _load_record_value('task_results', row['value_json'])
        return dict(value=value, version=row['version'], updated_at_ms=row['updated_at_ms'])

    def wallet(self):
        # If this owner has no account yet, serialize with account allocation so
        # absence cannot change during a cost-only claim's financial checks.
        row = self.session.fetch_one_for_update('SELECT id FROM tenant_users WHERE owner_user_id=?', (self.rows.owner,))
        if row is None:
            self.session.lock_key('identity.owner.allocate', 'owner_user_id')
            row = self.session.fetch_one_for_update('SELECT id FROM tenant_users WHERE owner_user_id=?', (self.rows.owner,))
        if row is not None:
            self.session.lock_key('billing.wallet', row['id'])
            return row['id']
        return None

    def ledger(self, wallet, kind, reference, root):
        if self.wallet() != wallet:
            _refuse('database_protocol_error')
        row = self.session.fetch_one('SELECT id, user_id, ts, amount_micro, kind, ref_type, ref_id, '
            f'balance_after_micro, {self.bounded("note")} FROM billing_ledger '
            'WHERE user_id=? AND kind=? AND ref_type=? AND ref_id=? LIMIT 1', (wallet, kind, reference, root))
        if row is not None and row['note'] is None:
            _refuse('database_integrity')
        return _ledger_document(row) if row is not None else None

    def header(self, task):
        key = self.rows.key(task)
        self.session.lock_key(HEADERS, key)
        value = self.rows._header(self.session, task, key, self.request['header_bytes'])
        return None if value is None else dict(value=value['value'], version=value['version'])

    def snapshot(self, task, version):
        current = self.header(task)
        if version and (current is None or current['version'] != version):
            _refuse('database_conflict')
        key = self.rows.key(task)
        row = self.session.fetch_one(
            f'SELECT {self.rows._projection(self.session, "s", self.request["snapshot_bytes"])} AS value_json, version '
            'FROM storage_records s WHERE namespace=? AND record_key=?', (SNAPSHOTS, key))
        if row is None:
            return None
        if version and not 0 < row['version'] <= version:
            _refuse('database_integrity')
        return self.rows._decode(row['value_json'], task, self.request['snapshot_bytes'])
