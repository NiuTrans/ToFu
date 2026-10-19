"""One joined SQL transaction around Rust whole Goal policy and local IO.

A non-ok inner ABI status raises before this callback returns, rolling back all
actor, wallet and event writes. Only the outer durable ACK can publish leases.
"""
import time
import orjson

from lib.storage.errors import StorageError
from lib.storage_authority_sql.json_document import JsonBytes
from lib.storage_authority_sql.native_checkpoint_rows import HEADERS, SNAPSHOTS, _refuse
from lib.storage_authority_sql.native_checkpoint_goal_reads import GoalReads
from lib.storage_authority_sql.generated_checkpoint_storage_contract import SPEC
from lib.storage_authority_sql.operation_domains.billing import _wallet_settle
from lib.storage_authority_sql.operations_pkg._runs import _orchestration_event_append


class GoalPolicyRefusal(StorageError):
    """Typed inner policy refusal; caught only after the backend rolls back."""
    def __init__(self, native_error):
        super().__init__('database_conflict' if native_error == 'conflict' else 'database_protocol_error',
                         'Native Goal transaction refused')
        self.native_error = native_error


class GoalTransaction(GoalReads):
    def dispatch(self, request):
        if (not isinstance(request, dict) or set(request) != {'format', 'operation', 'tenant_id', 'owner_user_id', 'payload'}
                or request['format'] != SPEC['callback_format'] or request['tenant_id'] != self.rows.tenant
                or request['owner_user_id'] != self.rows.owner or type(request['tenant_id']) is not int
                or type(request['owner_user_id']) is not int or not isinstance(request['payload'], dict)):
            _refuse()
        operation, payload = request['operation'], request['payload']
        handlers = {'goal.event': self.event, 'goal.run': self.run, 'goal.attempt': self.attempt,
            'goal.turn': self.turn, 'goal.record': self.record, 'goal.wallet': self.wallet,
            'goal.ledger': self.ledger, 'goal.header': self.header, 'goal.snapshot': self.snapshot,
            'goal.put': self.put, 'goal.delete_snapshot': self.delete_snapshot,
            'goal.settle': self.settle, 'goal.append': self.append}
        handler = handlers.get(operation)
        if handler is None:
            _refuse()
        value = handler(**payload)
        if len(orjson.dumps(value)) > self.maximum:
            _refuse('database_integrity')
        return {'ok': True, 'value': value}

    def put(self, task, namespace, expected, value):
        if namespace not in ('header', 'snapshot'):
            _refuse()
        private = HEADERS if namespace == 'header' else SNAPSHOTS
        maximum = self.request['header_bytes' if namespace == 'header' else 'snapshot_bytes']
        self.rows._value(value, task, maximum)
        key = self.rows.key(task)
        self.session.lock_key(HEADERS, key)
        row = self.session.fetch_one('SELECT version FROM storage_records WHERE namespace=? AND record_key=?', (private, key))
        if (row['version'] if row else 0) != expected or not 0 <= expected < (1 << 63)-1:
            _refuse('database_conflict')
        encoded = JsonBytes(orjson.dumps(self.rows._envelope(task, value)))
        self.rows._write(self.session, private, key, encoded, expected+1, max(1, time.time_ns()//1_000_000))
        return True

    def delete_snapshot(self, task):
        key = self.rows.key(task)
        self.session.lock_key(HEADERS, key)
        self.rows.delete_snapshot(self.session, key)
        return True

    def settle(self, **intent):
        if self.wallet() != intent.get('user_id'):
            _refuse()
        return _wallet_settle(self.session, intent)

    def append(self, run, label, cursor, value):
        if not self.lock_run(run, label) or self.cursor(run, label) != cursor:
            _refuse('database_conflict')
        result = _orchestration_event_append(self.session, dict(run_id=run, user_id=self.rows.owner,
            tenant_id=label, sequence=cursor, event=value))
        if not result['inserted']:
            _refuse('database_conflict')
        return True


def execute(rows, request, deadline_at):
    fields = {'format', 'operation', 'tenant_id', 'owner_user_id', 'header_bytes',
              'snapshot_bytes', 'evidence_bytes', 'authorization', 'actors'}
    if (not isinstance(request, dict) or set(request) != fields or request['format'] != SPEC['callback_format']
            or request['operation'] not in ('goal_claim', 'goal_handoff')
            or type(request['tenant_id']) is not int or request['tenant_id'] != rows.tenant
            or type(request['owner_user_id']) is not int or request['owner_user_id'] != rows.owner
            or any(type(request[name]) is not int or not 0 < request[name] <= rows.document_bytes
                   for name in ('header_bytes', 'snapshot_bytes', 'evidence_bytes'))):
        _refuse()
    if rows.goal_transaction is None:
        return {'ok': False, 'error': 'ineligible'}

    def transaction(session):
        # Match accepted Turn writes: conversation key, attempts, then Turns.
        # The lookup is scoped and repeated with row locks by the Rust reader.
        authorization = request['authorization']
        claim = authorization if request['operation'] == 'goal_claim' else authorization.get('claim_request', {})
        attempt = claim.get('target_attempt_id') if isinstance(claim, dict) else None
        row = session.fetch_one('SELECT a.conversation_id FROM storage_generation_attempts a '
            'WHERE a.attempt_id=? AND EXISTS (SELECT 1 FROM storage_conversation_turns t '
            'WHERE t.turn_id=a.turn_id AND t.conversation_id=a.conversation_id AND t.user_id=?)', (attempt, rows.owner))
        if row is not None:
            session.lock_key('turn_conversation', f'{rows.owner}:{row["conversation_id"]}')
        io = GoalTransaction(rows, session, request)
        value = rows.goal_transaction(request, io.dispatch, deadline_at)
        return {'ok': True, 'value': value}

    try:
        return rows.command(request, transaction, deadline_at)
    except GoalPolicyRefusal as error:
        return {'ok': False, 'error': error.native_error}
