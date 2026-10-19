"""Common-gateway adapter for conversation facts; owner decisions stay HTTP-only."""
import json

from lib.identity import PrincipalContext
from lib.memory.working_facts import service
from lib.memory.working_facts.contract import FactInputError
from lib.memory.working_facts.repository import FactConflict, FactNotFound, FactsUnavailable
from lib.storage import StorageError
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.manager import task_user_id
from lib.tools.result_envelope import typed_tool_error
from lib.tools.working_facts import READ_WORKING_FACTS, RECORD_WORKING_FACT


def task_scope(task):
    task_user_id(task)  # Validate/stamp the existing task principal boundary.
    principal = PrincipalContext.from_payload(task['_principalContext'])
    conversation_id = str(task.get('convId') or '')
    if task.get('_transientRuntime') or not conversation_id:
        raise FactsUnavailable('Durable conversation scope is required')
    return conversation_id, principal


def handle_working_facts(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                         cfg, project_path, project_enabled, all_tools=None):
    status = 'done'
    try:
        conversation_id, principal = task_scope(task)
        if fn_name == 'read_working_facts':
            result = service.query_facts(conversation_id, principal=principal, ids=fn_args.get('ids'))
            if fn_args.get('include_tool_sources') is True:
                from lib.tasks_pkg.working_tool_sources import available_tool_sources
                result.update(available_tool_sources(task, fn_args.get('tool_source_cursor', '')))
        elif fn_name == 'record_working_fact':
            from lib.tasks_pkg.working_file_sources import file_witness_resolver
            from lib.tasks_pkg.working_tool_sources import tool_quote_resolver
            result = service.record_fact(conversation_id, {**fn_args, 'action': 'record'},
                                         principal=principal, task_id=str(task.get('id') or ''),
                                         file_sources=file_witness_resolver(task), tool_sources=tool_quote_resolver(task))
        else:
            raise ValueError('Unknown working-facts operation')
        content = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    except (FactConflict, FactsUnavailable, PermissionError, ValueError, StorageError, TimeoutError) as exc:
        code = ('working_fact_not_found' if isinstance(exc, FactNotFound) else
                'working_facts_invalid_request' if isinstance(exc, FactInputError) else
                'working_facts_conflict' if isinstance(exc, FactConflict) else
                'working_facts_forbidden' if isinstance(exc, PermissionError) else
                'working_facts_unavailable')
        contract = READ_WORKING_FACTS if fn_name == 'read_working_facts' else RECORD_WORKING_FACT
        failure = next(error for error in contract.errors if error.code == code)
        content = typed_tool_error(failure.code, retryable=failure.retryable,
            next_action=failure.next_action, message=str(exc)).to_envelope_text()
        status = 'error'
    meta = _build_simple_meta(fn_name, content, source='Working facts', title='Conversation working facts')
    _finalize_tool_round(task, rn, round_entry, [meta], status=status)
    return tc_id, content, False
