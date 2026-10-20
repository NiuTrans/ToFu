"""Task-turn original inputs and explicit computed JSON working values.

Entry points: retain_program_result after post-hook capture; read_program_result
for explicit guest reuse; keep_program_value freezes computed JSON;
list_program_inputs discovers owned handles without reading source bodies.
CanonicalReceipt holds a runtime-only descriptor of
original or computed data; serialized metadata carries no native authority.
Dependencies: canonical captures, program contracts and launch resource policy.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
import threading

from lib.tools.program_contract import MAX_OUTPUT_BYTES, MAX_SOURCE_BYTES, ProgramExecutionError
from lib.tools.resource_policy import tool_result_cache_capacity
from lib.tools.result_value import CanonicalReceipt, CapturedToolResult, ResultPart

_LARGE_RESULT_BYTES = 4096
_BYTES_PER_RECEIPT = 64 * 1024


class _ProgramInputReceipt(CanonicalReceipt):
    """Original arguments stay runtime-only alongside the canonical capture."""
    def __init__(self, *args, binding_json, input_ref, task_id, json_type, **kwargs):
        super().__init__(*args, **kwargs)
        self.json_type = json_type
        self.binding_json = binding_json
        self.input_ref = input_ref
        self.task_id = task_id


class _ProgramValueReceipt(CanonicalReceipt):
    """A program-authored value never impersonates original tool evidence."""
    def __init__(self, *args, input_ref, task_id, catalog_fingerprint, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_ref = input_ref
        self.task_id = task_id
        self.catalog_fingerprint = catalog_fingerprint


def _catalog_fingerprint(names):
    return hashlib.sha256(json.dumps(sorted(names), ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _active_owner(task, user_id):
    latch = task.get('_ptc_local')
    return (isinstance(latch, dict) and latch.get('tier') == 'program'
            and not task.get('aborted') and int(user_id or 0) > 0
            and task.get('status') not in {'done', 'error', 'aborted', 'interrupted'})


def _admit(task, key, entry):
    with task.setdefault('_programResultLock', threading.RLock()):
        if task.get('aborted') or task.get('status') in {'done', 'error', 'aborted', 'interrupted'}:
            return False
        capacity = tool_result_cache_capacity()
        if entry['retainedBytes'] > capacity * _BYTES_PER_RECEIPT:
            return False
        index = task.setdefault('_programResultRefs', {})
        if not isinstance(index, dict):
            return False
        index.pop(key, None)
        index[key] = entry
        resident_bytes = sum(row['retainedBytes'] for row in index.values())
        while len(index) > capacity or resident_bytes > capacity * _BYTES_PER_RECEIPT:
            evicted = index.pop(next(iter(index)), None)
            if evicted is not None:
                resident_bytes -= evicted['retainedBytes']
        if task.get('id') and not task.get('_programResultCleanupBound'):
            from lib.agent_core.admission import on_terminal
            task['_programResultCleanupBound'] = True
            def cleanup(_task_id):
                with task['_programResultLock']:
                    task.pop('_programResultRefs', None)
                    task.pop('_programResultCleanupBound', None)
            on_terminal(task['id'], cleanup)
        if task.get('aborted'):
            task.pop('_programResultRefs', None)
            return False
        return task.get('_programResultRefs') is index and index.get(key) is entry


def keep_program_value(task, value, *, user_id, catalog_names, call_id):
    """Freeze explicit JSON working state; reuse shares the existing turn bank.

    This is untrusted computed data with unknown source coverage. Catalog changes
    revoke it conservatively because no exact source dependency is asserted.
    """
    if task.get('aborted'):
        raise ProgramExecutionError('cancelled', 'Program value retention cancelled')
    if not _active_owner(task, user_id):
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ProgramExecutionError('type_error', 'tools.keep expects a JSON value') from exc
    size = len(encoded.encode('utf-8'))
    if size > MAX_OUTPUT_BYTES:
        raise ProgramExecutionError('input_limit', 'Program value exceeds 1 MiB', limit=MAX_OUTPUT_BYTES)
    canonical = CapturedToolResult(parts=(ResultPart('json', encoded),))
    input_ref = 'input_' + uuid.uuid4().hex
    entry = _ProgramValueReceipt(ownerId=int(user_id), callId=str(call_id) + ':value:' + canonical.result_id,
        bytes=size, resultId=canonical.result_id,
        retainedBytes=canonical.retained_size()+len(input_ref)+64+1024,
        canonical=canonical, input_ref=input_ref, task_id=task.get('id'),
        catalog_fingerprint=_catalog_fingerprint(catalog_names))
    if not _admit(task, 'value:' + canonical.result_id, entry):
        return None
    return {'kind':'program_value', 'ref':input_ref, 'bytes':size, 'sourceCoverage':'unknown'}


def _accessible(entry, task, user_id, catalog_names):
    if (not isinstance(entry, (_ProgramInputReceipt, _ProgramValueReceipt))
            or entry.task_id != task.get('id') or entry.get('ownerId') != int(user_id)):
        return False
    if isinstance(entry, _ProgramValueReceipt):
        return entry.catalog_fingerprint == _catalog_fingerprint(catalog_names)
    return entry.get('toolName') in catalog_names


def _binding(name, arguments):
    if not isinstance(name, str) or not name or len(name) > 200 or not isinstance(arguments, dict):
        return None
    try:
        encoded = json.dumps([name, arguments], sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError):
        return None
    return (hashlib.sha256(encoded).hexdigest(), encoded) if len(encoded) <= MAX_SOURCE_BYTES else None


def _key(name, arguments):
    binding = _binding(name, arguments)
    return binding[0] if binding is not None else None


def _complete_inline(canonical):
    return (canonical.execution_status == 'ok'
            and canonical.retention_coverage == 'complete'
            and canonical.source_coverage not in {'partial', 'incomplete', 'unavailable'}
            and not canonical.children and not canonical.requires_reference())


def _snapshot_json_type(source):
    """Classify bounded original JSON once; never retain a second decoded tree."""
    try:
        def reject_constant(token):
            raise ValueError(token)
        value = json.loads(source, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return None
    if value is None:
        return 'null'
    return {list: 'array', dict: 'object', str: 'string', int: 'number',
            float: 'number', bool: 'boolean'}.get(type(value))


def retain_program_result(task, name, arguments, call_id, canonical, *, user_id):
    """Keep the original complete capture in a finite turn-owned working set.

    No model preview is recaptured and no extra storage write occurs. Admission
    applies equally to application and storage-free hosts.
    """
    from lib.tools.gateway import GATEWAY_TOOL_NAMES
    latch = task.get('_ptc_local') if isinstance(task, dict) else None
    if (not isinstance(latch, dict) or latch.get('tier') != 'program'
            or task.get('aborted') or task.get('status') in {'done', 'error', 'aborted', 'interrupted'}
            or int(user_id or 0) <= 0 or name in GATEWAY_TOOL_NAMES
            or name in {'read_tool_artifact', 'search_tool_artifact'}
            or not _complete_inline(canonical)):
        return
    source = canonical.text()
    size = len(source.encode('utf-8'))
    is_structured = (len(canonical.parts) == 1 and canonical.parts[0].kind == 'json'
                     and source.lstrip().startswith(('[', '{')))
    if size > MAX_OUTPUT_BYTES or (size < _LARGE_RESULT_BYTES and not is_structured):
        return
    binding = _binding(name, arguments)
    if binding is None:
        return
    json_type = _snapshot_json_type(source)
    if size < _LARGE_RESULT_BYTES and json_type not in {'array', 'object'}:
        return
    key, binding_json = binding
    input_ref = 'input_' + uuid.uuid4().hex
    entry = _ProgramInputReceipt(ownerId=int(user_id),toolName=name,
        callId=str(call_id),bytes=size,resultId=canonical.result_id,
        retainedBytes=canonical.retained_size()+len(binding_json)+len(input_ref)+1024,
        canonical=canonical,binding_json=binding_json,input_ref=input_ref,task_id=task.get('id'),
        json_type=json_type)
    if _admit(task, key, entry):
        return entry


def describe_program_input(task, entry, *, user_id, catalog_names):
    """Publish original snapshot metadata without reading source or arguments."""
    index = task.get('_programResultRefs')
    if (not _active_owner(task, user_id) or not isinstance(entry, _ProgramInputReceipt)
            or not isinstance(index, dict) or not any(row is entry for row in index.values())
            or not _accessible(entry, task, user_id, catalog_names)
            or entry.canonical is None or not _complete_inline(entry.canonical)):
        return None
    from lib.tools.result_envelope import ToolResultProgramInputV2
    try:
        return ToolResultProgramInputV2(entry.input_ref, entry['bytes'], entry.canonical.source_coverage, entry.json_type)
    except ValueError:
        return None


def validate_program_input_query(query):
    """The shared guest/callback contract for private input directory queries."""
    if (not isinstance(query, dict) or set(query) - {'name', 'offset', 'limit'}
            or ('name' in query and (not isinstance(query['name'], str)
                                    or not 0 < len(query['name']) <= 200))
            or type(query.get('offset', 0)) is not int or query.get('offset', 0) < 0
            or type(query.get('limit', 16)) is not int or not 1 <= query.get('limit', 16) <= 16):
        raise ProgramExecutionError('type_error', 'tools.inputs expects {name?, offset?:nonnegative integer, limit?:1..16}')
    return query


def list_program_inputs(task, query, *, user_id, catalog_names, include_arguments=True):
    """List owned exact-call bindings; no source bodies, executions or storage.

    Pages describe the current working set, which can change after fresh calls.
    Original argument bytes never become durable task or conversation metadata.
    """
    if task.get('aborted'):
        raise ProgramExecutionError('cancelled', 'Program input listing cancelled')
    query = validate_program_input_query(query)
    entries = []
    index = task.get('_programResultRefs')
    if _active_owner(task, user_id) and isinstance(index, dict):
        for entry in tuple(index.values()):
            if (not _accessible(entry, task, user_id, catalog_names)
                    or entry.canonical is None or not _complete_inline(entry.canonical)
                    or ('name' in query and entry.get('toolName') != query['name'])):
                continue
            entries.append(entry)
    offset, limit = query.get('offset', 0), query.get('limit', 16)
    page = []
    for entry in entries[offset:offset + limit]:
        metadata = {'ref':entry.input_ref, 'bytes':entry['bytes'],
                    'sourceCoverage':entry.canonical.source_coverage}
        if isinstance(entry, _ProgramValueReceipt):
            metadata['kind'] = 'program_value'
        else:
            if entry.json_type is not None:
                metadata['jsonType'] = entry.json_type
            metadata['name'] = entry['toolName']
            if include_arguments:
                metadata['args'] = json.loads(entry.binding_json)[1]
        page.append(metadata)
    end = offset + len(page)
    return {'inputs': page, 'total': len(entries),
            'nextOffset': end if end < len(entries) else None}


def validate_program_reference_selector(selector, arguments):
    """A reference is a bounded selector, never an execution capability."""
    if (set(selector) != {'ref'} or not isinstance(selector['ref'], str)
            or re.fullmatch(r'input_[0-9a-f]{32}', selector['ref']) is None
            or arguments != {}):
        artifact = isinstance(selector.get('ref'), str) and selector['ref'].startswith('tool-result:')
        raise ProgramExecutionError('type_error',
            ('tools.result received an artifact reference, not a retained input handle'
             if artifact else 'tools.result expects one {ref:input_reference} selector'),
            reason='artifact_reference' if artifact else 'invalid_input_selector',
            retry_hint='Use programInput.read or tools.inputs({name:toolName}), then '
                       'tools.result({ref:input.ref}). For artifactRef, use visible '
                       'read_tool_artifact({artifact_ref:ref}) and process its pages. '
                       'Do not repeat completed tool effects.')


def read_program_result(task, name, arguments, *, user_id, budget, catalog_names):
    """Read owned canonical evidence; never execute tools or recover previews."""
    def unavailable(reason):
        next_action = (
            'Do not re-execute the source tool to recover historical output. '
            'This lookup made no call; absence does not prove no earlier operation ran. '
            'Use tools.inputs to locate an accessible retained input or read '
            'an existing artifact for historical output; never repeat completed '
            'or uncertain effects to recover it. To obtain new data with an '
            'authorized read tool, explicitly use tools.invoke(name,args) in a '
            'new program. That is a new observation, not historical recovery. '
            'Otherwise report the evidence gap and continue independent work.')
        if reason == 'input_byte_budget':
            next_action = (
                'Do not re-execute the source tool. Read retained inputs in '
                'smaller groups across programs, or page an existing artifact '
                'through an available tool. Return only the needed findings.')
        return {'status':'error','error':{'code':'retained_result_unavailable',
            'reason':reason,'tool':name, 'retryable':False,
            'next_action':next_action},'output_truncated':False}

    if task.get('aborted'):
        raise ProgramExecutionError('cancelled', 'Program result read cancelled')
    index = task.get('_programResultRefs')
    if isinstance(name, dict):
        validate_program_reference_selector(name, arguments)
        entry = next((row for row in tuple(index.values())
                      if isinstance(row, (_ProgramInputReceipt, _ProgramValueReceipt)) and row.input_ref == name['ref']), None) if isinstance(index, dict) else None
        name = entry.get('toolName') if entry is not None else None
    else:
        key = _key(name, arguments)
        if key is None:
            raise ProgramExecutionError('type_error', 'tools.result expects {ref} or an exact tool name and JSON arguments object within 32 KiB')
        entry = index.get(key) if isinstance(index, dict) else None
    if (not _active_owner(task, user_id)
            or not _accessible(entry, task, user_id, catalog_names)
            or (isinstance(entry, _ProgramInputReceipt) and entry.get('toolName') != name)):
        return unavailable('not_retained_in_this_turn')
    canonical = entry.canonical
    if canonical is None or not _complete_inline(canonical):
        return unavailable('incomplete_source')
    if (type(entry.get('bytes')) is not int or not 0 <= entry['bytes'] <= MAX_OUTPUT_BYTES
            or entry['bytes'] > budget.stats()['remainingBytes']):
        return unavailable('input_byte_budget')
    source = canonical.text()
    if len(source.encode('utf-8')) != entry['bytes']:
        return unavailable('source_size_changed_or_too_large')
    delivery = budget.begin_batch([entry['callId']])
    delivery.capture(entry['callId'], canonical)
    result = delivery.result(entry['callId'])
    delivery.finish()
    return CanonicalReceipt(status='done',output=result['content'],
        output_truncated=result['truncated'],name=name,call_id=entry['callId'],
        canonical=canonical)
