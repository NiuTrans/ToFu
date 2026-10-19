"""Tool occurrence effect evidence at the common handler boundary.

Entry: begin_tool_effect, finish_tool_effect, uncertain_effect_error.
Policy: docs/contracts/TOOL_EFFECT_RECOVERY.md.
Conversation attempts use their event commit barrier; registered children use
their existing task-result checkpoint. Storage-free calls keep process scope.
This module owns no journal or retry loop.
Approval stays upstream; every retry must re-enter the ordinary authority gate.
"""
from uuid import uuid4
import threading

from lib.tools.effect_recovery import effect_recovery_policy


class ToolEffectDurabilityError(RuntimeError):
    """Handler entry was refused because its exact intent was not committed."""


class PreparedToolEffects:
    """One bounded native batch; identities are server-owned and ACK-gated.

    This carrier grants no approval. The common handler still commits intent
    and domain proof through its ordinary selected-store barriers.
    """
    def __init__(self, task, occurrences):
        from lib.identity import require_user_id
        self.task, self.task_id = task, task['id']
        self.owner = require_user_id(task.get('_userId'), context='prepared tool effects')
        self.rows = {}
        self.claimed = set()
        self.armed = False
        self.closed = False
        self.lock = threading.Lock()
        for name, call_id, number, row in occurrences:
            if id(row) in self.rows:
                raise ToolEffectDurabilityError('Prepared tool occurrence is duplicated')
            self.rows[id(row)] = (row, name, call_id, number, uuid4().hex)
        if not self.rows:
            raise ToolEffectDurabilityError('Prepared effect batch is empty')

    def identities(self):
        """Bind each operation to its ordinary tool counter, not a model round."""
        return tuple((name, call_id, number, operation)
                     for _, name, call_id, number, operation in self.rows.values())

    def attach(self):
        with self.lock:
            if self.closed or self.task.get('_preparedToolEffects') not in (None, self):
                raise ToolEffectDurabilityError('Another tool batch owns handler entry')
            self.task['_preparedToolEffects'] = self

    def arm(self):
        with self.lock:
            if self.closed or self.armed or self.task.get('_preparedToolEffects') is not self:
                raise ToolEffectDurabilityError('Prepared effect acknowledgement changed')
            self.armed = True

    def claim(self, task, name, call_id, number, row):
        with self.lock:
            selected = self.rows.get(id(row))
            if (self.closed or not self.armed or task is not self.task
                    or task.get('id') != self.task_id or type(task.get('_userId')) is not int
                    or task['_userId'] != self.owner or selected is None
                    or selected[0] is not row or selected[1:4] != (name, call_id, number)
                    or id(row) in self.claimed):
                raise ToolEffectDurabilityError('Tool entry has no exact committed occurrence')
            self.claimed.add(id(row))
            return selected[4]

    def close(self):
        with self.lock:
            self.closed = True
            if self.task.get('_preparedToolEffects') is self:
                self.task.pop('_preparedToolEffects')
            self.rows.clear()
            self.claimed.clear()


def begin_tool_effect(task, name, call_id, number, round_entry):
    from lib.conversation_sync.attempt_identity import is_conversation_attempt
    from lib.tasks_pkg.invocation_effects import durable_child
    policy = effect_recovery_policy(task.get('_toolContractDocumentsByName'), name)
    scope = 'attempt' if is_conversation_attempt(task) else 'invocation' if durable_child(task) else 'process'
    prepared = task.get('_preparedToolEffects')
    operation = (prepared.claim(task, name, call_id, number, round_entry)
                 if prepared is not None else uuid4().hex)
    effect = {'operationId': operation, 'state': 'entered', 'recovery': policy,
              'scope': scope}
    round_entry['executionEffect'] = effect
    if scope != 'process' and policy != 'read_only':
        from lib.agent_core.events import EventType, build_event
        from lib.tasks_pkg.manager import append_event
        event = build_event(EventType.TOOL_EFFECT, roundNum=number, toolName=name,
                            toolCallId=call_id, executionEffect=dict(effect))
        try:
            if scope == 'invocation':
                from lib.tasks_pkg.invocation_effects import persist_child_effect
                persist_child_effect(task, round_entry)
                append_event(task, event)
            elif append_event(task, event) is not True:
                raise ToolEffectDurabilityError('Exact tool effect intent was not durably acknowledged')
        except BaseException:
            # This process knows it never called the handler. A lost storage
            # ACK can leave an entered record on disk; recovery remains honest
            # about that uncertainty instead of manufacturing success.
            round_entry['executionEffect'] = {**effect, 'state': 'not_started'}
            raise
    return effect


def bind_tool_effect_reconciliation(context, reference):
    """Commit domain-owned evidence before its publication; no provider args enter here."""
    from lib.conversation_sync.validation import decode
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.manager import append_event
    row = context.round_entry
    effect = row.get('executionEffect') or {}
    if effect.get('scope') not in ('attempt', 'invocation'):
        return
    if effect.get('state') != 'entered' or effect.get('reconciliation') is not None:
        raise ToolEffectDurabilityError('Tool occurrence cannot bind another publication')
    reference = decode('ProjectFileWriteReconciliation', reference)
    from lib.conversation_sync.turn_images import turn_image_owner_scope
    child = effect['scope'] == 'invocation'
    inspection = decode('InvocationToolEffectInspectionReference' if child else 'ToolEffectInspectionReference', {
        'conversationId': context.task.get('convId'),
        ('invocationId' if child else 'turnId'): context.task['id'] if child else context.task.get('_turnId'),
        'operationId': effect['operationId'],
        'ownerScope': turn_image_owner_scope(context.owner_user_id, context.task.get('convId')),
    })
    candidate = {**effect, 'reconciliation': reference,
                 ('invocationInspection' if child else 'inspection'): inspection}
    event = build_event(EventType.TOOL_EFFECT, roundNum=context.round_num,
                        toolName=context.tool_name, toolCallId=context.tool_call_id,
                        executionEffect=candidate)
    # The Turn commit snapshots the common round projection as well as this
    # event. Both must carry the same reference before the exact ACK barrier.
    # A failed ACK retains possible evidence, never permission to publish.
    row['executionEffect'] = candidate
    if child:
        from lib.tasks_pkg.invocation_effects import persist_child_effect
        persist_child_effect(context.task, row)
        append_event(context.task, event)
    elif append_event(context.task, event) is not True:
        raise ToolEffectDurabilityError('File publication reference was not durably acknowledged')


def finish_tool_effect(round_entry, *, returned=False):
    effect = round_entry.get('executionEffect')
    if isinstance(effect, dict) and effect.get('state') == 'entered':
        round_entry['executionEffect'] = {**effect, 'state': 'returned' if returned else 'unknown'}


def uncertain_effect_error(round_entry, message):
    """A failed handler can have committed externally; do not advertise replay."""
    effect = round_entry.get('executionEffect') or {}
    if effect.get('state') != 'unknown' or effect.get('recovery') != 'reconcile':
        return message
    from lib.tools.result_envelope import typed_tool_error
    return typed_tool_error(
        'tool_effect_unknown', retryable=False,
        next_action=(effect_inspection_action(effect) or
                     'Do not repeat this operation automatically. Query the owning service or '
                     'inspect its durable result, then reconcile the effect before another write.'),
        message=f"Operation {effect['operationId']} may have taken effect. {message}",
    ).to_envelope_text()


def effect_inspection_action(effect):
    """Model-facing read instruction from server-bound producer identity only."""
    import json
    reference = effect.get('inspection') if isinstance(effect, dict) else None
    child = isinstance(effect, dict) and effect.get('scope') == 'invocation'
    if child:
        reference = effect.get('invocationInspection')
    if not isinstance(reference, dict):
        return ''
    arguments = {('invocation_id' if child else 'turn_id'): reference['invocationId' if child else 'turnId'],
                 'operation_id': reference['operationId']}
    return ('Do not repeat the write automatically. Call inspect_tool_effect(' +
            json.dumps(arguments, separators=(',', ':')) +
            ') to inspect file-publication evidence. This read does not authorize another write '
            'or prove the current file or the entire task is correct.')
