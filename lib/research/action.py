"""Agentic experiment, analysis, manuscript, compile, and publish actions.

The workflow reuses Tofu's guarded Paper agent loop and shared tool dispatcher.
Its authority is narrower than an ordinary report: native tools are selected
per action, while MCP writes exist only when the owner saved an exact
capability binding and explicitly confirmed that action.  Model output is a
proposal; deterministic code derives run, compile, and publication receipts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any

import lib as _lib
from lib.agent_loop import AbortSignal
from lib.identity import require_user_id
from lib.llm_json import extract_json
from lib.llm_errors import AbortedError
from lib.log import audit_log, get_logger
from lib.mcp.types import parse_namespaced_name
from lib.paper.agent_loop_policy import run_guarded_paper_agent_loop
from lib.paper.agent_usage import PaperAgentUsageMeter
from lib.paper.tools import (
    PaperToolEpochV2,
    PaperToolResultBudgetV2,
    build_paper_full_tool_epoch,
    execute_paper_tool,
    make_paper_exec_shim,
    paper_effective_tool_name,
)
from lib.tool_input_repair import parse_and_repair_tool_args

from .capabilities import build_capability_catalog, validate_bindings
from .manuscript import scaffold_source_files
from .program import (
    normalize_program_fields,
    normalize_source_path,
    readiness,
    source_tree_digest,
)

logger = get_logger(__name__)

ACTIONS = frozenset({'experiment', 'analyze', 'manuscript', 'compile', 'publish'})
WRITE_ACTIONS = frozenset({'experiment', 'analyze', 'compile', 'publish'})

_NATIVE_TOOLS = {
    'experiment': frozenset({
        'web_search', 'fetch_url', 'run_command',
        'read_tool_artifact', 'search_tool_artifact',
    }),
    'analyze': frozenset({
        'web_search', 'fetch_url', 'run_command',
        'read_tool_artifact', 'search_tool_artifact',
    }),
    'manuscript': frozenset({
        'web_search', 'fetch_url', 'read_tool_artifact',
        'search_tool_artifact',
    }),
    'compile': frozenset(),
    'publish': frozenset(),
}

_CAPABILITY_PREFIXES = {
    'experiment': ('experiment.', 'compute.', 'evaluation.', 'literature.'),
    'analyze': ('experiment.status', 'experiment.artifacts', 'evaluation.',
                'figure.', 'literature.'),
    'manuscript': ('literature.',),
    'compile': ('manuscript.compile',),
    'publish': ('publication.push',),
}


def _schema_name(schema: Mapping[str, Any]) -> str:
    function = schema.get('function')
    return str(function.get('name') or '') if isinstance(function, Mapping) else ''


def _binding_applies(action: str, capability: str) -> bool:
    return any(
        capability == prefix or capability.startswith(prefix)
        for prefix in _CAPABILITY_PREFIXES[action]
    )


def build_action_tool_epoch(
    *,
    action: str,
    user_id: int,
    workspace: Mapping[str, Any],
    confirm_external_writes: bool,
    model: str = '',
    cfg: Mapping[str, Any] | None = None,
    catalog: Mapping[str, Any] | None = None,
) -> tuple[PaperToolEpochV2, list[dict[str, Any]], list[dict[str, Any]]]:
    """Freeze one least-authority epoch and return bindings/problems."""
    if action not in ACTIONS:
        raise ValueError(f'unsupported research action: {action!r}')
    if action in WRITE_ACTIONS and not confirm_external_writes:
        raise ValueError(
            f'action={action!r} requires confirm_external_writes=true')
    caller_catalog = catalog is not None
    live_catalog = dict(catalog or build_capability_catalog(user_id=user_id))
    validation = validate_bindings(
        workspace.get('capability_bindings') or [], catalog=live_catalog)
    relevant = [
        row for row in validation['resolved']
        if _binding_applies(action, row['capability'])
    ]
    if action == 'compile' and not any(
            row['capability'] == 'manuscript.compile' for row in relevant):
        raise ValueError('compile requires an enabled manuscript.compile binding')
    if action == 'publish' and not any(
            row['capability'] == 'publication.push' for row in relevant):
        raise ValueError('publish requires an enabled publication.push binding')
    if not confirm_external_writes:
        relevant = [row for row in relevant if not row['write']]

    allowed_names = set(_NATIVE_TOOLS[action])
    allowed_names.update(row['tool'] for row in relevant)
    full = build_paper_full_tool_epoch(
        owner_user_id=user_id, model=model, cfg=dict(cfg or {}))
    if not caller_catalog:
        post_epoch_catalog = build_capability_catalog(user_id=user_id)
        post_validation = validate_bindings(
            workspace.get('capability_bindings') or [],
            catalog=post_epoch_catalog)
        post_relevant = [
            row for row in post_validation['resolved']
            if _binding_applies(action, row['capability'])
        ]
        before_fingerprint = sorted(
            (row['capability'], row['tool'], row['schema_hash'])
            for row in relevant)
        after_fingerprint = sorted(
            (row['capability'], row['tool'], row['schema_hash'])
            for row in post_relevant)
        if before_fingerprint != after_fingerprint:
            raise ValueError(
                'research capability catalog changed while freezing the tool epoch; '
                'review bindings and retry')
    executable = tuple(
        copy.deepcopy(schema) for schema in full.executable_schemas
        if _schema_name(schema) in allowed_names
    )
    found_names = {_schema_name(schema) for schema in executable}
    missing = sorted(allowed_names - found_names)
    if action in {'compile', 'publish'} and missing:
        raise ValueError('bound tool is unavailable in the executable epoch: '
                         + ', '.join(missing))
    documents = {
        name: copy.deepcopy(document)
        for name, document in full.contract_documents_by_name.items()
        if name in found_names
    }
    epoch_payload = json.dumps({
        'action': action,
        'tools': sorted(found_names),
        'contracts': {
            name: str(documents[name].get('schema_hash') or '')
            for name in sorted(documents)
        },
        'confirmed': bool(confirm_external_writes),
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    epoch = PaperToolEpochV2(
        wire_schemas=executable,
        executable_schemas=executable,
        contract_documents_by_name=documents,
        discovery_policy_by_name={name: 'eager' for name in found_names},
        namespace_by_name={
            name: full.namespace_by_name.get(name, 'research')
            for name in found_names
        },
        search_text_by_name={
            name: full.search_text_by_name.get(name, '') for name in found_names
        },
        script_safe_by_name={
            name: bool(full.script_safe_by_name.get(name)) for name in found_names
        },
        schema_tokens=0,
        gateway_schema_tokens=0,
        schema_budget_tokens=full.schema_budget_tokens,
        result_envelope=full.result_envelope,
        epoch_hash=hashlib.sha256(epoch_payload.encode('utf-8')).hexdigest(),
        degraded_reason=full.degraded_reason,
    )
    return epoch, relevant, validation['problems']


def _bounded_workspace_context(workspace: Mapping[str, Any], action: str) -> dict:
    context = copy.deepcopy(dict(workspace))
    context.pop('publication', None)
    if action not in {'manuscript', 'compile', 'publish'}:
        context.pop('source_files', None)
    elif action == 'manuscript':
        remaining = 160_000
        files = []
        for row in list(context.get('source_files') or []):
            if remaining <= 0 or not isinstance(row, Mapping):
                break
            content = str(row.get('content') or '')[:remaining]
            remaining -= len(content)
            files.append({'path': row.get('path'), 'content': content})
        context['source_files'] = files
    return context


def _research_context(direction: str, lang: str, user_id: int) -> dict:
    try:
        from .persistence import load_research_artifacts
        artifacts = load_research_artifacts(direction, lang, user_id=user_id)
    except AbortedError:
        raise
    except Exception as exc:
        logger.warning('[ResearchAction] artifact context unavailable: %s', exc)
        return {}
    return {
        'survey_markdown': str(artifacts.get('survey_markdown') or '')[:60_000],
        'gap_map': list(artifacts.get('gap_map') or [])[:24],
        'accepted': list(artifacts.get('accepted') or [])[:12],
        'corpus_size': artifacts.get('corpus_size'),
        'updated_at': artifacts.get('updated_at'),
    }


def _action_instructions(action: str) -> str:
    common = (
        'Return one JSON object only, without markdown fences. Never invent a '
        'paper, citation, metric, artifact, tool call, or project URL. Keep '
        'claims falsifiable and distinguish observed evidence from inference. '
        'Use only tools visible in this request. A tool call is not evidence '
        'until its result returns successfully. '
    )
    instructions = {
        'experiment': (
            'Execute the smallest decisive experiment allowed by the frozen '
            'protocol. Return {"summary":str,"protocol":object,"run":'
            '{"id":str,"label":str,"status":"passed|failed|inconclusive",'
            '"metric":str,"baseline":str,"delta":str,"backend":str,'
            '"remote_job_id":str,"artifact_refs":[str],"notes":str},'
            '"claims":[{"id":str,"text":str,"status":str,'
            '"evidence_refs":[str]}]}.'),
        'analyze': (
            'Analyze existing run evidence, compute uncertainty/ablations when '
            'possible, and design reproducible figures/tables. Return '
            '{"summary":str,"claims":[...],"figures":[{"id":str,'
            '"title":str,"caption":str,"data_ref":str,"script_ref":str,'
            '"output_ref":str,"status":str}],"tables":[same shape]}.'),
        'manuscript': (
            'Write at top-conference standard: precise novelty positioning, '
            'reproducible method, quantitative results, honest limitations. '
            'Return {"summary":str,"manuscript":{"title":str,"venue":str,'
            '"abstract":str,"keywords":str,"introduction":str,'
            '"related_work":str,"method":str,"experiments":str,'
            '"results":str,"limitations":str,"conclusion":str,'
            '"ethics":str},"source_updates":[{"path":str,"content":str}]}.'),
        'compile': (
            'Compile the exact current source tree using the bound compiler. '
            'Return {"summary":str,"status":"passing|failing",'
            '"engine":str,"detail":str}. Passing is accepted only when the '
            'bound tool completed in this action.'),
        'publish': (
            'Create or synchronize the exact current source tree using the '
            'bound publication tool. Return {"summary":str,"status":'
            '"published|conflict|failed","provider":str,"project_ref":str,'
            '"project_url":str,"detail":str}. Published is accepted only '
            'when the bound tool completed in this action.'),
    }
    return common + instructions[action]


def _messages(task: Mapping[str, Any], workspace: Mapping[str, Any],
              bindings: list[dict[str, Any]]) -> list[dict[str, str]]:
    action = str(task['action'])
    payload = {
        'action': action,
        'direction': task['direction'],
        'language': task['lang'],
        'workspace': _bounded_workspace_context(workspace, action),
        'research_artifacts': _research_context(
            task['direction'], task['lang'], int(task['user_id'])),
        'capability_bindings': bindings,
    }
    return [
        {'role': 'system', 'content': (
            'You are Tofu Research Foundry, an evidence-first autonomous '
            'research agent. ' + _action_instructions(action))},
        {'role': 'user', 'content': json.dumps(
            payload, ensure_ascii=False, separators=(',', ':'), default=str)},
    ]


def _tool_receipts(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    receipts = []
    observed_at = int(time.time())
    for row in rounds[:64]:
        excerpt = str(row.get('toolContent') or '')[:4_000]
        status = str(row.get('status') or '')
        try:
            structured = json.loads(excerpt)
        except (TypeError, ValueError):
            structured = None
        structured_error = isinstance(structured, Mapping) and (
            structured.get('isError') is True
            or structured.get('ok') is False
            or structured.get('success') is False
            or bool(structured.get('error'))
            or str(structured.get('status') or '').lower()
                in {'error', 'failed', 'rejected'}
        )
        if status == 'rejected':
            normalized_status = 'rejected'
        elif structured_error or excerpt.lower().startswith((
                'error:', 'error ', 'mcp tool error:', 'unknown tool:',
                'failed:', 'failure:')):
            normalized_status = 'error'
        else:
            normalized_status = 'done'
        digest = hashlib.sha256(excerpt.encode('utf-8')).hexdigest()
        output_artifact = row.get('outputArtifact')
        output_artifact = output_artifact if isinstance(output_artifact, Mapping) else {}
        artifact_ref = str(
            row.get('toolResultArtifactRef')
            or output_artifact.get('artifactRef') or '')
        if not artifact_ref and normalized_status == 'done':
            artifact_ref = f'evidence:{digest[:24]}'
        receipts.append({
            'tool': str(row.get('toolName') or ''),
            'call_id': str(row.get('toolCallId') or ''),
            'status': normalized_status,
            'artifact_ref': artifact_ref,
            'result_digest': digest,
            'result_excerpt': excerpt,
            'observed_at': observed_at,
        })
    return receipts


def _merge_by_id(existing: Any, proposed: Any, *, prefix: str) -> list[dict]:
    merged = {
        str(row.get('id') or f'{prefix}-{index + 1}'): dict(row)
        for index, row in enumerate(list(existing or []))
        if isinstance(row, Mapping)
    }
    for index, row in enumerate(list(proposed or [])):
        if not isinstance(row, Mapping):
            continue
        key = str(row.get('id') or f'{prefix}-{len(merged) + index + 1}')
        merged[key] = dict(row) | {'id': key}
    return list(merged.values())


def _merge_bound_arguments(
    name: str,
    arguments: Mapping[str, Any],
    bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply owner-saved provider defaults before schema validation."""
    effective_name = paper_effective_tool_name(name)
    defaults: Mapping[str, Any] = {}
    for binding in bindings:
        if binding.get('tool') in {name, effective_name}:
            candidate = binding.get('argument_defaults')
            defaults = candidate if isinstance(candidate, Mapping) else {}
            break
    return copy.deepcopy(dict(defaults)) | dict(arguments)


def _observed_reference(
    value: Any,
    receipts: list[dict[str, Any]],
    allowed_refs: set[str] | None = None,
) -> str:
    """Keep a model-proposed reference only when a receipt or ledger saw it."""
    reference = str(value or '').strip()[:1_000]
    if not reference:
        return ''
    known = set(allowed_refs or ())
    known.update(
        str(row.get('artifact_ref') or '') for row in receipts
        if row.get('status') == 'done' and row.get('artifact_ref')
    )
    if reference in known:
        return reference
    if any(
        reference in str(row.get('result_excerpt') or '')
        for row in receipts if row.get('status') == 'done'
    ):
        return reference
    return ''


def _sanitize_claims(
    proposed: Any,
    *,
    receipts: list[dict[str, Any]],
    allowed_refs: set[str],
) -> list[dict[str, Any]]:
    claims = []
    for row in list(proposed or [])[:32]:
        if not isinstance(row, Mapping):
            continue
        claim = dict(row)
        claim['evidence_refs'] = list(dict.fromkeys(
            observed for observed in (
                _observed_reference(ref, receipts, allowed_refs)
                for ref in list(row.get('evidence_refs') or [])[:24]
            ) if observed
        ))
        if claim.get('status') == 'supported' and not claim['evidence_refs']:
            claim['status'] = 'draft'
        claims.append(claim)
    return claims


def _sanitize_visuals(
    proposed: Any,
    *,
    receipts: list[dict[str, Any]],
    allowed_refs: set[str],
) -> list[dict[str, Any]]:
    visuals = []
    for row in list(proposed or [])[:24]:
        if not isinstance(row, Mapping):
            continue
        visual = dict(row)
        for field in ('data_ref', 'script_ref', 'output_ref'):
            visual[field] = _observed_reference(
                row.get(field), receipts, allowed_refs)
        if visual.get('status') == 'verified' and not all(
                visual.get(field) for field in (
                    'data_ref', 'script_ref', 'output_ref')):
            visual['status'] = 'generated' if visual.get('output_ref') else 'planned'
        elif visual.get('status') == 'generated' and not visual.get('output_ref'):
            visual['status'] = 'planned'
        visuals.append(visual)
    return visuals


def apply_action_result(
    *,
    action: str,
    workspace: Mapping[str, Any],
    payload: Mapping[str, Any],
    receipts: list[dict[str, Any]],
    task_id: str,
    bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply model proposals under deterministic evidence gates."""
    draft = copy.deepcopy(dict(workspace))
    now = int(time.time())
    done = [row for row in receipts if row.get('status') == 'done']
    if action == 'experiment':
        if isinstance(payload.get('protocol'), Mapping):
            draft['protocol'] = dict(draft.get('protocol') or {}) | dict(payload['protocol'])
        proposed = payload.get('run') if isinstance(payload.get('run'), Mapping) else {}
        evidence_tool_names = {'run_command'} | {
            str(binding.get('tool') or '') for binding in bindings
            if binding.get('capability') in {
                'experiment.execute', 'experiment.status',
                'experiment.artifacts', 'compute.submit', 'compute.status',
                'evaluation.run',
            }
        }
        evidence_receipts = [
            row for row in done if row.get('tool') in evidence_tool_names
        ]
        evidence_refs = [
            str(row.get('artifact_ref')) for row in evidence_receipts
            if row.get('artifact_ref')
        ]
        evidence_refs.extend(
            observed for observed in (
                _observed_reference(ref, evidence_receipts)
                for ref in list(proposed.get('artifact_refs') or [])[:24]
            ) if observed
        )
        evidence_refs = list(dict.fromkeys(evidence_refs))
        requested_status = str(proposed.get('status') or 'inconclusive')
        if requested_status not in {'passed', 'failed', 'inconclusive'}:
            requested_status = 'inconclusive'
        if not evidence_receipts:
            requested_status = 'planned'
        elif requested_status == 'passed' and not evidence_refs:
            requested_status = 'inconclusive'
        run_id = str(proposed.get('id') or f'run-{now}')[:96]
        spec_digest = hashlib.sha256(json.dumps({
            'hypothesis': draft.get('hypothesis'),
            'protocol': draft.get('protocol'),
            'label': proposed.get('label'),
        }, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()
        remote_job_id = _observed_reference(
            proposed.get('remote_job_id'), evidence_receipts)
        backend = next((
            str(row.get('tool') or '') for row in evidence_receipts
            if row.get('tool')), '')
        run = dict(proposed) | {
            'id': run_id,
            'status': requested_status,
            'artifact_ref': evidence_refs[0] if evidence_refs else '',
            'artifact_refs': evidence_refs,
            'task_id': task_id,
            'backend': backend,
            'remote_job_id': remote_job_id,
            'spec_digest': spec_digest,
            'tool_receipts': receipts,
            'finished_at': now if evidence_receipts else 0,
            'updated_at': now,
        }
        draft['runs'] = _merge_by_id(draft.get('runs'), [run], prefix='run')
        claims = _sanitize_claims(
            payload.get('claims'), receipts=evidence_receipts,
            allowed_refs=set(evidence_refs) | {run_id, task_id})
        draft['claims'] = _merge_by_id(
            draft.get('claims'), claims, prefix='claim')
        draft['stage'] = 'evidence' if evidence_receipts else 'experiment'
    elif action == 'analyze':
        allowed_refs = {
            str(ref)
            for run in list(draft.get('runs') or []) if isinstance(run, Mapping)
            for ref in (
                list(run.get('artifact_refs') or [])
                + [run.get('artifact_ref'), run.get('id'), run.get('task_id')]
            )
            if str(ref or '').strip()
        }
        claims = _sanitize_claims(
            payload.get('claims'), receipts=done, allowed_refs=allowed_refs)
        figures = _sanitize_visuals(
            payload.get('figures'), receipts=done, allowed_refs=allowed_refs)
        tables = _sanitize_visuals(
            payload.get('tables'), receipts=done, allowed_refs=allowed_refs)
        draft['claims'] = _merge_by_id(
            draft.get('claims'), claims, prefix='claim')
        draft['figures'] = _merge_by_id(
            draft.get('figures'), figures, prefix='figure')
        draft['tables'] = _merge_by_id(
            draft.get('tables'), tables, prefix='table')
        draft['stage'] = 'evidence'
    elif action == 'manuscript':
        proposed = payload.get('manuscript')
        if isinstance(proposed, Mapping):
            draft['manuscript'] = dict(draft.get('manuscript') or {}) | dict(proposed)
        current = {
            str(row.get('path')): dict(row)
            for row in list(draft.get('source_files') or [])
            if isinstance(row, Mapping) and row.get('path')
        }
        for row in list(payload.get('source_updates') or [])[:24]:
            if not isinstance(row, Mapping):
                continue
            path = normalize_source_path(row.get('path'))
            if path:
                current[path] = {
                    'path': path, 'content': str(row.get('content') or ''),
                    'updated_at': now,
                }
        draft['source_files'] = list(current.values())
        draft['source_files'] = scaffold_source_files(draft)
        draft['stage'] = 'writing'
    elif action == 'compile':
        compile_binding = next((
            binding for binding in bindings
            if binding['capability'] == 'manuscript.compile'), {})
        compiled = any(
            row.get('status') == 'done'
            and compile_binding.get('tool') == row.get('tool')
            for row in receipts)
        status = 'passing' if payload.get('status') == 'passing' and compiled else 'failing'
        draft['compilation'] = {
            'mode': 'bound_tool',
            'status': status,
            'detail': str(payload.get('detail') or payload.get('summary') or ''),
            'source_digest': source_tree_digest(draft.get('source_files')),
            'engine': str(compile_binding.get('tool') or ''),
            'compiled_at': now,
        }
        draft['stage'] = 'writing'
    elif action == 'publish':
        publish_binding = next((
            binding for binding in bindings
            if binding['capability'] == 'publication.push'), {})
        publication_receipts = [
            row for row in receipts
            if row.get('status') == 'done'
            and publish_binding.get('tool') == row.get('tool')
        ]
        published = bool(publication_receipts)
        requested = str(payload.get('status') or 'failed')
        status = 'published' if requested == 'published' and published else (
            requested if requested in {'conflict', 'failed'} else 'failed')
        parsed_provider = parse_namespaced_name(
            str(publish_binding.get('tool') or ''))
        draft['publication'] = {
            'provider': parsed_provider[0] if parsed_provider else str(
                publish_binding.get('tool') or ''),
            'status': status,
            'project_ref': _observed_reference(
                payload.get('project_ref'), publication_receipts),
            'project_url': _observed_reference(
                payload.get('project_url'), publication_receipts),
            'source_digest': source_tree_digest(draft.get('source_files')),
            'published_at': now if status == 'published' else 0,
            'detail': str(payload.get('detail') or payload.get('summary') or ''),
        }
        draft['stage'] = 'submission'
    normalized = normalize_program_fields(draft)
    for key, value in normalized.items():
        draft[key] = value
    return draft


def _run_agent(task: dict, workspace: Mapping[str, Any], epoch: PaperToolEpochV2,
               bindings: list[dict[str, Any]]) -> tuple[dict, list[dict]]:
    from lib.llm_dispatch.api import dispatch_stream
    from lib.llm.stream_result import ensure_provider_stream_result
    from lib.research.action_runtime import append_action_event

    messages = _messages(task, workspace, bindings)
    abort = AbortSignal.from_event(task['abort_event'])
    model_name = str(task.get('model') or _lib.LLM_MODEL)
    rounds: list[dict[str, Any]] = task['tool_rounds']
    terminal = {'content': ''}
    result_budget = PaperToolResultBudgetV2(
        owner_user_id=int(task['user_id']), model=model_name,
        result_envelope=epoch.result_envelope, conv_id=task['task_id'])
    shim = make_paper_exec_shim(
        task_id=task['task_id'], conv_id=task['task_id'], abort=abort.is_set,
        owner_user_id=int(task['user_id']), tool_epoch=epoch, model=model_name)
    meter = PaperAgentUsageMeter(
        'research-action', token_budget=320_000, dispatch_budget=10,
        fallback_model=model_name)

    def dispatch(round_index, tools):
        return ensure_provider_stream_result(dispatch_stream(
            messages,
            on_content=lambda text: append_action_event(task, {
                'type': 'delta', 'delta': text, 'llmRound': round_index}),
            on_thinking=lambda text: append_action_event(task, {
                'type': 'thinking', 'delta': text, 'llmRound': round_index}),
            abort_check=abort.is_set,
            prefer_model=model_name if task.get('model') else None,
            strict_model=bool(task.get('model')),
            tools=tools, max_tokens=64_000, temperature=0,
            thinking_enabled=False, log_prefix='[ResearchAction]',
        ))

    def on_result(_round, message, _finish, _usage):
        if isinstance(message, Mapping) and not message.get('tool_calls'):
            terminal['content'] = str(message.get('content') or '')
        task['agentUsageV1'] = meter.snapshot()

    def on_tool_round(_round, message):
        messages.append(message)

    def execute_tool(round_index, tool_call):
        function = tool_call.get('function') or {}
        name = str(function.get('name') or '')
        raw_args = function.get('arguments') or '{}'
        call_id = str(tool_call.get('id') or '')
        args, _repair = parse_and_repair_tool_args(name, raw_args)
        args = _merge_bound_arguments(name, args, bindings)
        round_entry = {
            'roundNum': len(rounds) + 1,
            'llmRound': round_index,
            'toolName': paper_effective_tool_name(name),
            'toolCallId': call_id,
            'toolArgs': json.dumps(args, ensure_ascii=False),
            'status': 'searching',
            'results': None,
        }
        rounds.append(round_entry)
        append_action_event(task, {'type': 'tool_start', **round_entry})
        started = time.time()
        result, display, _diag, _engines, _verticals = execute_paper_tool(
            name, json.dumps(args, ensure_ascii=False),
            user_question=str(task['direction'])[:300], abort=abort.is_set,
            exec_shim=shim, round_entry=round_entry,
            contract_documents_by_name=epoch.contract_documents_by_name,
        )
        elapsed = time.time() - started
        if round_entry.get('status') != 'rejected':
            round_entry['status'] = 'done'
        round_entry['results'] = display
        round_entry['toolContent'] = str(result)[:4_000]
        round_entry['_elapsed'] = f'{elapsed:.1f}s'
        result_budget.append(
            messages, round_index=round_index, tool_name=name,
            tool_call_id=call_id, content=result, round_entry=round_entry,
            tool_arguments=args)
        append_action_event(task, {
            'type': 'tool_done',
            'roundNum': round_entry['roundNum'], 'llmRound': round_index,
            'toolName': round_entry['toolName'], 'toolCallId': call_id,
            'status': round_entry['status'], 'elapsed': round(elapsed, 1),
            'toolContent': round_entry.get('toolContent', ''),
        })

    outcome = run_guarded_paper_agent_loop(
        context=f'Research {task["action"]} agent', usage_meter=meter,
        abort=abort, round_tools=list(epoch.wire_schemas), dispatch=dispatch,
        execute_tool=execute_tool, on_round_result=on_result,
        on_tool_round=on_tool_round, on_round_end=result_budget.finish_round,
    )
    if not outcome.completed:
        raise RuntimeError(f'research action did not complete: {outcome.exit_reason}')
    payload = extract_json(terminal['content'], repair=True)
    if not isinstance(payload, Mapping):
        raise ValueError('research action returned no valid JSON object')
    return dict(payload), _tool_receipts(rounds)


def run_research_action(task: dict) -> None:
    from lib.research.action_runtime import (
        _research_action_runtime,
        append_action_event,
    )
    from lib.research.workspace import load_workspace, save_workspace

    task_id = task['task_id']
    try:
        _research_action_runtime.mark_running(task_id)
        append_action_event(task, {
            'type': 'phase', 'phase': 'start', 'action': task['action']})
        workspace = load_workspace(
            task['direction'], task['lang'], user_id=int(task['user_id']))
        if int(workspace.get('revision') or 0) != int(task['expected_revision']):
            raise ValueError('research_workspace_stale')
        if task['action'] in {'compile', 'publish'} and not workspace.get('source_files'):
            raise ValueError(f'action={task["action"]!r} requires manuscript source files')
        epoch, bindings, binding_problems = build_action_tool_epoch(
            action=task['action'], user_id=int(task['user_id']),
            workspace=workspace,
            confirm_external_writes=bool(task['confirm_external_writes']),
            model=str(task.get('model') or ''),
        )
        task['toolEpochV2'] = epoch.telemetry()
        task['bindingProblems'] = binding_problems
        append_action_event(task, {
            'type': 'phase', 'phase': 'agent', 'action': task['action'],
            'toolCount': len(epoch.executable_schemas),
        })
        payload, receipts = _run_agent(task, workspace, epoch, bindings)
        updated = apply_action_result(
            action=task['action'], workspace=workspace, payload=payload,
            receipts=receipts, task_id=task_id, bindings=bindings)
        saved = save_workspace(
            task['direction'], task['lang'], updated,
            expected_revision=int(task['expected_revision']),
            user_id=int(task['user_id']),
        )
        result = {
            'action': task['action'], 'summary': str(payload.get('summary') or ''),
            'workspace': saved, 'readiness': readiness(saved),
            'tool_receipts': receipts, 'binding_problems': binding_problems,
        }
        append_action_event(task, {
            'type': 'final', 'action': task['action'],
            'revision': saved['revision'], 'readiness': result['readiness']})
        _research_action_runtime.finish(task_id, result=result)
    except AbortedError:
        logger.info('[ResearchAction] task %s aborted by owner', task_id)
        _research_action_runtime.abort(task_id)
        _research_action_runtime.finish(
            task_id, terminal_event_fields={
                'type': 'aborted', 'action': task.get('action', ''),
            })
    except Exception as exc:
        logger.error('[ResearchAction] task %s failed: %s',
                     task_id, exc, exc_info=True)
        _research_action_runtime.finish(
            task_id, error=exc, error_context='research:action')


def start_research_action(
    direction: str,
    *,
    action: str,
    expected_revision: int,
    lang: str = 'en',
    confirm_external_writes: bool = False,
    model: str = '',
    conv_id: str = '',
    user_id: int,
) -> dict[str, Any]:
    """Validate, create, and spawn one explicit research action."""
    del conv_id
    from lib.research.action_runtime import (
        _production,
        _research_action_runtime,
        create_action_task,
    )

    owner = require_user_id(user_id, context='research action owner')
    clean_direction = str(direction or '').strip()[:2_000]
    clean_action = str(action or '').strip().lower()
    if not clean_direction:
        raise ValueError('direction is required')
    if clean_action not in ACTIONS:
        raise ValueError(f'action must be one of {sorted(ACTIONS)}')
    if (isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0):
        raise ValueError('expected_revision must be a non-negative integer')
    if clean_action in WRITE_ACTIONS and confirm_external_writes is not True:
        raise ValueError(
            f'action={clean_action!r} requires confirm_external_writes=true')
    _production.cleanup_stale()
    key = (owner, clean_direction.casefold(), 'zh' if lang == 'zh' else 'en',
           clean_action, expected_revision)
    existing = _production.index_get(key)
    if existing:
        return {'task_id': existing, 'deduped': True}
    task_id = _production.new_task_id()
    task = create_action_task(task_id, user_id=owner, fields={
        'direction': clean_direction,
        'lang': 'zh' if lang == 'zh' else 'en',
        'action': clean_action,
        'expected_revision': expected_revision,
        'confirm_external_writes': bool(confirm_external_writes),
        'model': str(model or '')[:240],
        'tool_rounds': [],
    })
    _production.index_register(key, task_id)
    audit_log(
        'research_action_start', task_id=task_id, action=clean_action,
        user_id=owner, revision=expected_revision,
        confirmed=bool(confirm_external_writes))
    try:
        _research_action_runtime.spawn(task_id, run_research_action, task)
    except Exception as exc:
        _research_action_runtime.finish(
            task_id, error=exc, error_context='research:action:start')
        raise
    return {'task_id': task_id, 'deduped': False}


__all__ = [
    'ACTIONS', 'WRITE_ACTIONS', 'apply_action_result',
    'build_action_tool_epoch', 'run_research_action',
    'start_research_action',
]
