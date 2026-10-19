"""Canonical tool provenance at existing execution and settlement boundaries.

The bounded bank keeps metadata only. Inline text must still match its original
canonical digest in the existing task round; spilled results use the existing
owner-scoped artifact reader. No replay, new archive, result-body cache or timer.
"""
from dataclasses import dataclass
import hashlib
import time

from lib.tools.resource_policy import tool_result_cache_capacity
from lib.tools.result_value import CapturedToolResult, INLINE_LIMIT
from .working_source_scope import source_scope, source_receipt_lock, source_task_is_live

_SKIP = {'read_working_facts', 'record_working_fact'}


@dataclass(frozen=True)
class _Started:
    scope: tuple
    task_id: str
    name: str


@dataclass(frozen=True)
class _Receipt:
    started: _Started
    identifier: str
    version: str
    call_id: str
    result_id: str
    observed_at: int
    source_coverage: str
    artifact_ref: str

    def descriptor(self):
        return {'kind': 'tool_result', 'id': self.identifier, 'version': self.version,
                'toolName': self.started.name, 'toolCallId': self.call_id,
                'observedAt': self.observed_at, 'sourceCoverage': self.source_coverage,
                'temporality': 'historical_execution'}


def _bounded_put(task, key, identifier, value):
    bank = task.get(key)
    if not isinstance(bank, dict):
        bank = {}; task[key] = bank
    bank.pop(identifier, None); bank[identifier] = value
    capacity = tool_result_cache_capacity()
    while len(bank) > capacity:
        bank.pop(next(iter(bank)))


def begin_tool_source(task, name, call_id):
    if (name in _SKIP or not source_task_is_live(task) or not task.get('id')
            or not task.get('convId') or task.get('_transientRuntime')):
        return
    try:
        scope = source_scope(task)
    except (ValueError, PermissionError):
        return  # Scope-less compatibility callers cannot issue source authority.
    started = _Started(scope, str(task.get('id') or ''), name)
    with source_receipt_lock:
        _bounded_put(task, '_working_tool_source_starts', call_id, started)


def settle_tool_source(task, name, call_id, row, canonical):
    with source_receipt_lock:
        started = (task.get('_working_tool_source_starts') or {}).pop(call_id, None)
    if (not isinstance(started, _Started) or not source_task_is_live(task)
            or not isinstance(row, dict) or row.get('status') != 'done'
            or row.get('cacheSource') == 'cache' or not isinstance(canonical, CapturedToolResult)
            or canonical.execution_status not in {'ok', 'success'}
            or canonical.retention_coverage != 'complete'):
        return
    if started.scope != source_scope(task) or started.task_id != str(task.get('id') or '') or started.name != name:
        return
    from lib.memory.working_facts.repository import digest
    reference = canonical.artifact_ref if canonical.artifact_owner == started.scope[0] else ''
    if reference:
        # The canonical owner issued this exact retained tree; an advisory
        # artifact-origin row or JSON-shaped reference is never consulted.
        version = digest(['canonical_tool_result', reference])
    elif len(canonical.parts) == 1 and not canonical.children and canonical.parts[0].kind in {'text', 'json'}:
        text = canonical.parts[0].text
        if not isinstance(text, str) or len(text) > INLINE_LIMIT or row.get('toolContent') != text:
            return  # A preview/compacted carrier cannot stand in for original bytes.
        version = hashlib.sha256(text.encode('utf-8')).hexdigest()
    else:
        return
    identifier = 'tool_' + digest([*started.scope, started.task_id, call_id, canonical.result_id])
    receipt = _Receipt(started, identifier, version, call_id, canonical.result_id,
                       int(time.time() * 1000), canonical.source_coverage, reference)
    with source_receipt_lock:
        if source_task_is_live(task) and started.scope == source_scope(task):
            _bounded_put(task, '_working_tool_sources', identifier, receipt)


def available_tool_sources(task, cursor=''):
    from lib.memory.working_facts.contract import limit
    scope = source_scope(task)
    with source_receipt_lock:
        receipts = list((task.get('_working_tool_sources') or {}).values())
    rows = [row.descriptor() for row in reversed(receipts) if isinstance(row, _Receipt)
            and row.started.scope == scope and row.started.task_id == str(task.get('id') or '')]
    if cursor:
        index = next((i for i, row in enumerate(rows) if row['id'] == cursor), None)
        if index is None:
            raise ValueError('Tool source cursor expired; request the latest source page')
        rows = rows[index + 1:]
    count = limit('toolSourcePageSize')
    return {'toolSources': rows[:count], 'toolSourcesNextCursor': rows[count - 1]['id'] if len(rows) > count else ''}


def tool_quote_resolver(task):
    scope = source_scope(task)
    from lib.memory.working_facts.repository import digest
    from lib.memory.working_facts.tool_sources import VerifiedToolQuote
    def resolve(source, check):
        check()
        if not source_task_is_live(task) or source_scope(task) != scope:
            return None
        with source_receipt_lock:
            receipt = (task.get('_working_tool_sources') or {}).get(source['id'])
        if (not isinstance(receipt, _Receipt) or receipt.started.scope != scope
                or receipt.started.task_id != str(task.get('id') or '')
                or receipt.descriptor() != {key: value for key, value in source.items() if key != 'quote'}):
            return None
        if receipt.artifact_ref:
            if not _retained_quote(scope[0], receipt.artifact_ref, source['quote'], check):
                return None
        else:
            row = next((row for row in reversed(task.get('toolRounds') or [])
                        if isinstance(row, dict) and row.get('toolCallId') == receipt.call_id), None)
            text = row.get('toolContent') if row and row.get('status') == 'done' else None
            if (not isinstance(text, str) or len(text) > INLINE_LIMIT
                    or hashlib.sha256(text.encode('utf-8')).hexdigest() != receipt.version):
                return None
            if source['quote'] not in text:
                raise ValueError('Tool quote was not found in the canonical result')
        check()
        if not source_task_is_live(task) or source_scope(task) != scope:
            return None
        return VerifiedToolQuote(scope, digest(source), receipt.started.task_id, receipt.result_id)
    return resolve


def _retained_quote(owner, reference, quote, check):
    from lib.memory.working_facts.repository import digest
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    repository = ToolResultArtifactRepository()
    cursor, seen = 0, set()
    previous, previous_end, tail = None, 0, ''
    while True:
        check()
        result = repository.read_range(user_id=owner, artifact_ref=reference, offset=cursor, limit=65536)
        if not result:
            return False
        encoded, consumed = result.get('content', '').encode('utf-8'), 0
        for span in result.get('provenance', []):
            # A root-relative segment identity prevents combining independent
            # outputs, even when repeated child calls share a name/call ID.
            segment = span.get('segment')
            start, end = span.get('start'), span.get('end')
            if (not isinstance(segment, list) or any(type(i) is not int or i < 0 for i in segment)
                    or type(start) is not int or type(end) is not int or end <= start
                    or consumed + end - start > len(encoded)):
                return False
            key = (span.get('artifactRef'), tuple(segment), digest(span.get('path')))
            piece = encoded[consumed:consumed + end - start].decode('utf-8')
            if key != previous or start != previous_end:
                tail = ''
            candidate = tail + piece
            if quote in candidate:
                return True
            tail = candidate[-(len(quote) - 1):] if len(quote) > 1 else ''
            previous, previous_end = key, end
            consumed += end - start
        if consumed != len(encoded):
            return False
        cursor = result.get('nextCursor')
        if not cursor or cursor in seen:
            return False
        seen.add(cursor)
