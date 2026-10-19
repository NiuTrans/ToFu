"""Refresh version-bound working facts through the common evidence renderer.

Called before ordinary ROOT/Flow/native prompt admission. Acquisition borrows
the existing provider executor, captures immutable identity only, and cannot
mutate a task after cancellation/deadline. No timer, new pool or message loop.
"""
from concurrent.futures import TimeoutError as FutureTimeout
from functools import partial
import json
import time

from lib.identity import PrincipalContext
from lib.memory.working_facts.contract import limit
from lib.agent_core.storage_errors import StorageError
from ._provider_executor import context_provider_executor


def _text(view, model):
    from lib.token_counter import count_text
    # Newest first preserves recent corrections under the common token budget.
    selected = []
    value = {'meaning': 'Source-bound observations and owner assessments, not automatic truth or execution authorization. '
             'Use the latest version of each fact. Stale/unavailable records require fresh evidence; derived records need review.',
             'revision': view['revision'], 'facts': selected, 'omitted': len(view['facts'])}
    def encode():
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    budget = limit('contextTokens')
    estimated = count_text(encode(), model=model)
    for fact in sorted(view['facts'], key=lambda row: (-row['updatedAt'], row['id'])):
        candidate = {key: fact[key] for key in ('id', 'revision', 'text', 'kind', 'status', 'reasons')}
        candidate['sources'] = [{key: child for key, child in source.items() if key != 'quote'}
                                for source in fact['sources']]
        cost = count_text(json.dumps(candidate, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')), model=model) + 1
        if estimated + cost > budget:
            break
        selected.append(candidate)
        value['omitted'] -= 1
        estimated += cost
    # Token counts need not be additive across JSON boundaries. Estimate once
    # per whole fact, then enforce the exact canonical count before publication.
    while selected and count_text(encode(), model=model) > budget:
        selected.pop()
        value['omitted'] += 1
    return encode(), [{'id': row['id'], 'revision': row['revision'], 'status': row['status']} for row in selected]


def refresh_working_facts(messages, *, task, model):
    from lib.tasks_pkg.context_composer import (
        ComposeRequest, append_context_blocks, can_append_injection, context_block, disabled_context_blocks,
    )
    from lib.tasks_pkg.manager import task_user_id
    if (task.get('aborted') or task.get('_transientRuntime') or not task.get('convId')
            or not task.get('_userId') or not can_append_injection(messages)):
        return None
    disabled = disabled_context_blocks(task.get('config') or {})
    owner = task_user_id(task)
    from lib.context_experiment_flags import normalize_context_experiment_flags
    from lib.token_counter import count_text
    flags = normalize_context_experiment_flags(task.get('config') or {})
    budget = int(flags['context']['globalBudgetTokens'] or 0)
    base_tokens = (count_text(json.dumps(messages, ensure_ascii=False, separators=(',', ':'),
                                       default=str), model=model) if budget else 0)
    request = ComposeRequest(user_id=owner, conv_id=task['convId'], model=model, task=task,
                             global_budget_tokens=budget or None, base_context_tokens=base_tokens,
                             disabled_blocks=disabled)
    if 'working_facts' in disabled:
        return append_context_blocks(messages, [context_block('working_facts', '', reason='disabled')], request)
    from lib.memory.working_facts import service
    from lib.memory.working_facts.repository import FactsUnavailable
    principal = PrincipalContext.from_payload(task['_principalContext'])
    future = context_provider_executor.submit(partial(service.query_facts, str(task['convId']), principal=principal))
    deadline = time.monotonic() + limit('acquisitionSeconds')
    content, reason, provenance = '', 'not_applicable', {}
    try:
        while True:
            if task.get('aborted') or (task.get('abort_event') and task['abort_event'].is_set()):
                future.cancel()
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Working facts deadline expired')
            try:
                view = future.result(timeout=min(.05, remaining))
                break
            except FutureTimeout:
                if future.done():
                    raise
        if view['facts']:
            content, dependencies = _text(view, model)
            provenance = {'conversationId': task['convId'], 'taskId': task.get('id', ''),
                          'revision': view['revision'], 'facts': dependencies}
            reason = ''
    except FactsUnavailable:
        reason = 'not_applicable'
    except (StorageError, TimeoutError):
        future.cancel()
        content = ('Working facts are currently unavailable. Prior fact source bindings have not been '
                   'revalidated; do not treat old assessments as current evidence. Retry read_working_facts.')
        provenance = {'availability': 'unknown'}
        reason = ''
    if task.get('aborted') or (task.get('abort_event') and task['abort_event'].is_set()):
        return None
    return append_context_blocks(messages, [context_block('working_facts', content, reason=reason,
        provenance=provenance, recovery_handle='conversation-working-facts:' + task['convId'])], request)
