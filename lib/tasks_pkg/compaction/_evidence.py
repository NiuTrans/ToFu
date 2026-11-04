"""Source-derived observations shared by summary input and continuation state."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from lib.log import get_logger
from lib.tool_results.tool_history_pairing import adjacent_tool_call_result_pairs


logger = get_logger(__name__)


_MAX_PREVIEW = 600
_TEST_RE = re.compile(r'\b(pytest|unittest|npm test|pnpm test|cargo test|go test|'
                      r'mvn test|gradle test|ctest)\b', re.IGNORECASE)
_ERROR_RE = re.compile(r'^(?:error|failed|failure|traceback|exception|fatal)(?::|\b)',
                       re.IGNORECASE)
_COMMAND_EXIT = re.compile(r'\[exit code: (-?\d+)\]\s*\Z')
_QUERY_TOOLS = frozenset({
    'web_search', 'fetch_url', 'search_files', 'grep_search', 'find_files',
    'read_files', 'list_dir', 'inspect_image', 'inspect_video',
    'search_mcp_tools', 'search_tools',
})
_MUTATION_TOOLS = frozenset({
    'write_file', 'apply_diff', 'apply_diffs', 'insert_content',
    'insert_contents', 'edit_file', 'delete_file',
})
_AGENT_TOOLS = frozenset({
    'get_agent_result', 'await_agents', 'spawn_agents',
})
_BATCH_TOOLS = frozenset({'execute_tools', 'execute_program'})


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        logger.debug('[CompactionEvidence] JSON text fallback: %s', exc)
        return str(value)


def _preview(value: Any) -> str:
    text = _text(value).strip()
    if len(text) <= _MAX_PREVIEW:
        return text
    return text[:_MAX_PREVIEW // 2] + ' … ' + text[-_MAX_PREVIEW // 3:]


def _result_preview(value: Any) -> str:
    # Share the summary-input media/opaque-data projection. Persisting a new
    # continuity packet must not reintroduce ciphertext stripped by that owner.
    from ._layer2._summary_evidence import EVIDENCE_PARSE_CHARS, _preview as safe_preview
    if isinstance(value, str) and len(value) <= EVIDENCE_PARSE_CHARS:
        try:
            decoded = json.loads(value)
            if isinstance(decoded, (dict, list)):
                value = decoded
        except (ValueError, RecursionError):
            pass
    return safe_preview(value, _MAX_PREVIEW)


def _evidence_id(kind: str, source: str, value: Any) -> str:
    digest = hashlib.sha256(
        f'{kind}\x00{source}\x00{_text(value)}'.encode('utf-8')).hexdigest()[:12]
    return f'ev-{digest}'


def _walk_artifact_ids(value: Any, out: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).replace('_', '').lower()
            if normalized in ('artifactid', 'archiveid') and child is not None:
                out.add(str(child))
            else:
                _walk_artifact_ids(child, out)
    elif isinstance(value, list):
        for child in value:
            _walk_artifact_ids(child, out)


def build_evidence_ledger(messages: list, task: dict | None = None) -> dict:
    """Extract chronological, addressable observations from paired receipts."""
    task = task if isinstance(task, dict) else {}
    observations: dict[str, dict] = {}

    def add(kind: str, source: str, value: Any, *,
            identity_source: str | None = None, preview_value: str | None = None,
            **fields: Any) -> None:
        if value in (None, '', [], {}):
            return
        # Tool-call IDs differ even when the model repeats the exact same
        # observation.  Let callers supply a stable identity source so those
        # repetitions consume one bounded ledger entry, while retaining the
        # latest concrete call ID as a recovery handle.
        evidence_id = _evidence_id(kind, identity_source or source, value)
        preview = _preview(value) if preview_value is None else preview_value
        entry = {
            'id': evidence_id,
            'type': kind,
            'source': source,
            'value': preview,
            'sourceDigest': hashlib.sha256(_text(value).encode('utf-8')).hexdigest(),
            'previewOnly': preview != _text(value).strip(),
        }
        entry.update({key: child for key, child in fields.items()
                      if child not in (None, '', [], {})})
        observations.pop(evidence_id, None)
        observations[evidence_id] = entry

    modified = task.get('modifiedFileList') or task.get('modifiedFiles') or []
    if isinstance(modified, dict):
        modified = list(modified)
    for item in modified if isinstance(modified, list) else [modified]:
        path = item.get('path') if isinstance(item, dict) else item
        add('modified_file', 'task.modifiedFiles', path or item)

    # Canonical result references come only from producer-owned evidence, not
    # parsed model previews. They survive L2 summarization as recovery anchors.
    for row in task.get('toolRounds') or ():
        if not isinstance(row, dict):
            continue
        evidence = row.get('toolResultEvidence')
        if not isinstance(evidence, dict) or evidence.get('resultContractVersion') != 'tofu.tool-result/v2':
            continue
        ref = evidence.get('artifactRef')
        if isinstance(ref, str) and re.fullmatch(r'tool-result:[0-9a-f]{64}', ref):
            add('tool_result_archive', 'task.canonical', ref,
                artifactRef=ref, resultId=evidence.get('resultId'),
                toolCallId=row.get('toolCallId'), toolName=row.get('toolName'),
                retentionCoverage=evidence.get('retentionCoverage'))

    artifact_ids: set[str] = set()
    _walk_artifact_ids(task.get('toolRounds') or [], artifact_ids)
    _walk_artifact_ids(task.get('_artifacts') or [], artifact_ids)
    for artifact_id in sorted(artifact_ids):
        add('artifact', 'task', artifact_id, artifactId=artifact_id)

    for archive in task.get('_contextEvidenceArchives') or []:
        if not isinstance(archive, dict):
            continue
        add('tool_result_archive', 'task.compaction',
            archive.get('reference'),
            toolCallId=archive.get('toolCallId'),
            toolName=archive.get('toolName'))

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        for call in message.get('tool_calls') or []:
            if not isinstance(call, dict):
                continue
            fn = call.get('function') or {}
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.debug(
                    '[CompactionEvidence] malformed tool arguments ignored: %s',
                    exc)
                args = {}
            call_id = str(call.get('id') or '')
            def _add_paths(value: Any) -> None:
                if isinstance(value, dict):
                    for key, child in value.items():
                        if str(key).replace('_', '').lower() in {
                                'path', 'file', 'filepath', 'target'}:
                            add('file_reference', f"tool:{fn.get('name')}",
                                child, toolCallId=call_id)
                        else:
                            _add_paths(child)
                elif isinstance(value, list):
                    for child in value:
                        _add_paths(child)

            _add_paths(args)

    paired_call_by_result_object = {
        id(result): call
        for call, result in adjacent_tool_call_result_pairs(messages or [])
    }
    for message in messages or []:
        if not isinstance(message, dict) or message.get('role') != 'tool':
            continue
        call_id = str(message.get('tool_call_id') or '')
        paired_call = paired_call_by_result_object.get(id(message))
        if isinstance(paired_call, Mapping):
            fn = paired_call.get('function') or {}
            tool_name = str(fn.get('name') or '') \
                if isinstance(fn, dict) else ''
            try:
                args = json.loads(fn.get('arguments') or '{}') \
                    if isinstance(fn, dict) else {}
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.debug(
                    '[CompactionEvidence] malformed paired arguments ignored: %s',
                    exc)
                args = {}
            if not isinstance(args, dict):
                args = {}
        else:
            continue
        content = _text(message.get('content') or '')
        command = _text(args.get('command') or args.get('cmd') or '')
        source = f'tool:{tool_name or "unknown"}:{call_id}'
        stable_source = f'tool:{tool_name or "unknown"}'
        call_identity = f'{stable_source}:{_text(args)}'
        if not args:
            # Unknown invocation identity cannot supersede another occurrence.
            call_identity += ':' + call_id
        observation_key = hashlib.sha256(
            (_text((task.get('config') or {}).get('projectPath') or '')
             + '\x00' + call_identity).encode('utf-8')).hexdigest()
        # Keep invocation identity independent of outcome class: a successful
        # repeat must supersede that invocation's earlier error.
        observation_fields = dict(observationKey=observation_key,
                                  toolName=tool_name,
                                  preview_value=_result_preview(message.get('content')))
        if command:
            observation_fields['command'] = _preview(command)
        if _TEST_RE.search(command) or (tool_name == 'run_command'
                                        and _TEST_RE.search(content[:200])):
            add('test_result', source, content,
                identity_source=call_identity,
                toolCallId=call_id, **observation_fields)
        elif (message.get('isError') is True
              or (tool_name == 'run_command' and _COMMAND_EXIT.search(content)
                  and int(_COMMAND_EXIT.search(content).group(1)) != 0)
              or (not _COMMAND_EXIT.search(content)
                  and _ERROR_RE.search(content[:2000]))):
            add('error', source, content, identity_source=call_identity,
                toolCallId=call_id, **observation_fields)
        elif tool_name in _MUTATION_TOOLS:
            add('mutation_result', source, content,
                identity_source=call_identity, toolCallId=call_id, **observation_fields)
        elif tool_name == 'run_command':
            add('command_result', source, content,
                identity_source=call_identity,
                toolCallId=call_id, **observation_fields)
        elif tool_name in _AGENT_TOOLS:
            add('agent_result', source, content,
                identity_source=call_identity, toolCallId=call_id, **observation_fields)
        elif tool_name in _BATCH_TOOLS:
            add('batch_result', source, content,
                identity_source=call_identity, toolCallId=call_id, **observation_fields)
        elif tool_name in _QUERY_TOOLS:
            add('query_result', source, content,
                identity_source=call_identity, toolCallId=call_id, **observation_fields)

    unfinished = (task.get('_todos') or task.get('todos') or task.get('todo')
                  or task.get('_unfinishedItems') or [])
    if isinstance(unfinished, list):
        unfinished = [item for item in unfinished
                      if not isinstance(item, dict)
                      or item.get('status') != 'completed']
    if unfinished:
        add('unfinished', 'task', unfinished)

    bounded = list(observations.values())
    return {
        'version': 1,
        'entries': bounded,
        'evidenceIds': [entry['id'] for entry in bounded],
        'truncated': False,
    }


def format_evidence_ledger(ledger: dict) -> str:
    if not ledger.get('entries'):
        return ''
    return (
        '## Structured Evidence Ledger\n'
        'These entries are grounded records of past observations, not proof '
        'that mutable repository or test state is still current. Revalidate '
        'when freshness matters. Preserve the `[EVIDENCE ev-…]` identifier '
        'for every entry used in the summary so retention can be audited '
        'after compaction. File/archive paths are recovery handles.\n\n'
        + '\n'.join(
            f"[EVIDENCE {entry['id']}] "
            + json.dumps(entry, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'))
            for entry in ledger['entries'])
    )


def bound_evidence_ledger(ledger: dict, max_chars: int) -> dict:
    """Keep whole addressable entries within a summary-prompt char budget."""
    limit = max(1_000, int(max_chars or 0))
    selected: list[dict] = []
    for entry in ledger.get('entries') or []:
        candidate = {
            **ledger,
            'entries': selected + [entry],
            'evidenceIds': [row['id'] for row in selected + [entry]],
        }
        if len(format_evidence_ledger(candidate)) > limit:
            break
        selected.append(entry)
    return {
        **ledger,
        'entries': selected,
        'evidenceIds': [entry['id'] for entry in selected],
        'truncated': bool(ledger.get('truncated')
                          or len(selected) < len(ledger.get('entries') or [])),
    }


def evidence_retention(summary: str, ledger: dict) -> tuple[list[str], list[str]]:
    ids = [str(value) for value in ledger.get('evidenceIds') or []]
    retained = [value for value in ids if value in (summary or '')]
    lost = [value for value in ids if value not in (summary or '')]
    return retained, lost


__all__ = [
    'bound_evidence_ledger', 'build_evidence_ledger', 'evidence_retention',
    'format_evidence_ledger',
]
