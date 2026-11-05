"""Conversation-scoped cache affinity and evidence context for rebuilt requests.

A new physical attempt never inherits transport, credential, retry or admission
state. The request-local tool/orchestration owner is re-attached separately.
"""
import copy


def preserve_rebuilt_request_context(source, destination, *, task=None):
    """Retain stable identity across a recovery-only body reconstruction."""
    from lib.llm.cache_affinity import CACHE_AFFINITY_SCOPE_KEY
    if isinstance(task, dict):
        from lib.tasks_pkg.invocation import invocation_affinity_id
        destination.setdefault('_task_id', task.get('id', ''))
        destination.setdefault('_conv_id', task.get('convId') or '')
        destination.setdefault(CACHE_AFFINITY_SCOPE_KEY,
                               invocation_affinity_id(task))
    for key in ('_task_id', '_conv_id', CACHE_AFFINITY_SCOPE_KEY,
                '_thinking_depth', '_safety_identifier',
                '_raw_archive_context', '_working_set_tokens'):
        if key in source:
            destination[key] = copy.deepcopy(source[key])
    if isinstance(destination.get('_raw_archive_context'), dict):
        destination['_raw_archive_context']['model'] = destination.get('model') or ''
    return destination


def attach_rebuilt_request_metadata(task, source, destination, messages,
        tool_list, *, round_num, tid):
    preserve_rebuilt_request_context(source, destination, task=task)
    from lib.tasks_pkg.orchestrator._tool_request_metadata import (
        attach_tool_request_metadata)
    attach_tool_request_metadata(task, destination, messages, tool_list,
                                 model=destination.get('model') or '',
                                 round_num=round_num, tid=tid)
    return destination
