"""One-use source results for an already announced speculative occurrence.

Entry: capture_prefetch, publish_prefetch, has_prefetch, consume_prefetch,
discard_prefetch, remap_prefetch. The admitted handler consumes source results.
The existing task result cache owns capacity and locking; the ordinary parser,
gateway, executor and domain handler still own admission and final settlement.
These process-local carriers never authorize another call or survive restore.
"""
from dataclasses import dataclass, replace

from lib.tasks_pkg.tool_dispatch._flags import (
    _call_id_signature, _discard_tool_result_cache_entry, _make_cache_key,
    _store_tool_result_cache_entry,
    _tool_receipt_lock,
)


def _identity(task, name, call_id, args, row, project_path):
    documents = task.get('_toolContractDocumentsByName')
    return _call_id_signature(name, {
        'taskObject': id(task), 'rowObject': id(row), 'taskId': task.get('id'),
        'userId': task.get('_userId'), 'principal': task.get('_principalContext'),
        'conversationId': task.get('convId'), 'turnId': task.get('_turnId'),
        'attemptId': task.get('_attemptId') or task.get('attemptId'),
        'worldVersion': task.get('_worldVersion') or task.get('worldVersion'),
        'callId': call_id, 'arguments': args, 'caller': row.get('caller'),
        'roundNum': row.get('roundNum'), 'llmRound': row.get('llmRound'),
        'projectPath': project_path, 'config': task.get('config'),
        'userQuestion': task.get('lastUserQuery'),
        'toolEnvironment': id(task.get('_tool_env')),
        'contract': documents.get(name) if isinstance(documents, dict) else documents,
    })


@dataclass(frozen=True)
class PrefetchedToolResult:
    name: str
    call_id: str
    row_id: int
    identity: str
    generation: object
    previous_cache_entry: object = None
    payload: object = None

    @property
    def cache_key(self):
        # Distinct from argument-addressed reusable observations.
        return _cache_key(self.name, self.row_id)

    def bind(self, payload):
        return replace(self, payload=payload)

    def matches(self, task, name, call_id, args, row, project_path):
        from lib.tasks_pkg.tool_dispatch._flags import _PROJECT_CACHEABLE_TOOLS
        from lib.write_freshness import project_read_cache_generation
        return (self.name == name and self.call_id == call_id
                and self.identity == _identity(task, name, call_id, args, row, project_path)
                and (name not in _PROJECT_CACHEABLE_TOOLS
                     or self.generation is project_read_cache_generation()))


def capture_prefetch(task, name, call_id, args, row, project_path):
    """Capture identity before IO; only the owning domain may bind a payload."""
    from lib.tasks_pkg.manager import task_user_id
    from lib.write_freshness import project_read_cache_generation
    try:
        task_user_id(task)
    except (ValueError, PermissionError):
        return None
    with _tool_receipt_lock(task):
        cache = task.get('_tool_result_cache')
        previous = cache.get(_make_cache_key(name, args)) if isinstance(cache, dict) else None
    return PrefetchedToolResult(name, call_id, id(row),
        _identity(task, name, call_id, args, row, project_path),
        project_read_cache_generation(), previous)


def publish_prefetch(task, receipt, args, row, project_path):
    if receipt.payload is None or not receipt.matches(
            task, receipt.name, receipt.call_id, args, row, project_path):
        return False
    # The completed read supersedes only the reusable observation it started
    # after; another concurrent producer may already have published newer data.
    with _tool_receipt_lock(task):
        _discard_tool_result_cache_entry(task, _make_cache_key(receipt.name, args),
                                         receipt.previous_cache_entry)
        _store_tool_result_cache_entry(task, receipt.cache_key,
                                       replace(receipt, previous_cache_entry=None))
    return True


def _cache_key(name, row_id):
    return f'prefetch-occurrence:{name}::{row_id}'


def has_prefetch(task, name, call_id, args, row, project_path):
    """Prefer this fresh occurrence to reusable cache; grant no admission."""
    if not isinstance(row, dict):
        return False
    with _tool_receipt_lock(task):
        cache = task.get('_tool_result_cache')
        receipt = cache.get(_cache_key(name, id(row))) if isinstance(cache, dict) else None
    return isinstance(receipt, PrefetchedToolResult) and receipt.matches(
        task, name, call_id, args, row, project_path)


def discard_prefetch(task, name, row):
    if isinstance(row, dict):
        with _tool_receipt_lock(task):
            cache = task.get('_tool_result_cache')
            if isinstance(cache, dict):
                return cache.pop(_cache_key(name, id(row)), None)
    return None


def remap_prefetch(task, name, old_call_id, call_id, args, row, project_path):
    """The identity-repair owner remints only this same physical occurrence."""
    if not isinstance(row, dict):
        return
    key = _cache_key(name, id(row))
    with _tool_receipt_lock(task):
        cache = task.get('_tool_result_cache')
        receipt = cache.get(key) if isinstance(cache, dict) else None
        if isinstance(receipt, PrefetchedToolResult) and receipt.matches(
                task, name, old_call_id, args, row, project_path):
            cache[key] = replace(receipt, call_id=call_id,
                identity=_identity(task, name, call_id, args, row, project_path))


def consume_prefetch(task, name, call_id, args, row, project_path):
    """The admitted domain handler consumes source facts before ordinary formatting."""
    receipt = discard_prefetch(task, name, row)
    if task.get('aborted'):
        from lib.llm.llm_errors import AbortedError
        raise AbortedError('Task cancelled before speculative result adoption')
    if isinstance(row, dict) and row.get('_toolResultPublicationClosed'):
        raise TimeoutError('Tool result collection ended before source-result consumption')
    if not isinstance(receipt, PrefetchedToolResult):
        return None
    if receipt.matches(task, name, call_id, args, row, project_path):
        return receipt.payload
    return None
