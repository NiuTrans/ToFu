"""Research production actions and deterministic workspace compilation.

The workflow reuses Tofu's guarded Paper agent loop and shared tool dispatcher.
Its authority is narrower than an ordinary report: native tools are selected
per action, while MCP writes exist only when the owner saved an exact
capability binding and explicitly confirmed that action.  Model output is a
proposal; deterministic code derives run and publication receipts. Compilation
uses the shared paper-build service without a model or arbitrary tool binding.
"""

from __future__ import annotations

from lib.research.observation_audit import decode_observation_json

import copy
import hashlib
import json
import time
from contextlib import ExitStack
from collections.abc import Mapping
from typing import Any

import lib as _lib
from lib.agent_loop import AbortSignal
from lib.agent_core.events import Phase, build_phase
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
    MAX_RUNS,
    normalize_program_fields,
    run_result_identity,
    normalize_run_evidence,
    dependency_digests,
    claim_evidence_digest,
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
    'publish': frozenset(),
}

_CAPABILITY_PREFIXES = {
    'experiment': ('experiment.', 'compute.', 'evaluation.', 'literature.'),
    'analyze': ('experiment.status', 'experiment.artifacts', 'evaluation.',
                'figure.', 'literature.'),
    'manuscript': ('literature.',),
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
    if action == 'compile':
        raise ValueError('Research compilation is owned by the paper build service, not an agent tool epoch')
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
    if action == 'publish' and missing:
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
    from .context import project_workspace
    return project_workspace(workspace, action)


def _research_context(direction: str, lang: str, user_id: int, workspace=None) -> dict:
    source_task_id = (workspace or {}).get('selected_idea_source_task_id', '')
    if source_task_id:
        from .records import load_idea_source
        idea_id = workspace.get('selected_idea_id', '')
        artifacts = load_idea_source(source_task_id, user_id=user_id,
            direction=direction, lang=lang, idea_id=idea_id)
        accepted = [idea for idea in artifacts['accepted']
                    if isinstance(idea, Mapping) and str(idea.get('id') or '') == idea_id]
    else:
        try:
            from .persistence import load_research_artifacts
            artifacts = load_research_artifacts(direction, lang, user_id=user_id)
        except AbortedError:
            raise
        except Exception as exc:
            logger.warning('[ResearchAction] artifact context unavailable: %s', exc)
            return {}
        accepted = list(artifacts.get('accepted') or [])[:12]
    survey = str(artifacts.get('survey_md') or '')
    gaps = list((artifacts.get('open_gaps') or {}).get('open_gaps') or [])
    # Bound execution needs its complete original evidence. The final wire
    # admission uses the selected model's capacity, including tools/output.
    survey_limit = len(survey) if source_task_id else 16_000
    projected_gaps = gaps if source_task_id else gaps[:20]
    return {
        **({'source_task_id':source_task_id} if source_task_id else {}),
        'survey_markdown': survey[:survey_limit],
        'gap_map': projected_gaps,
        'context_projection': {
            'survey_total_chars': len(survey),
            'survey_omitted_chars': max(0, len(survey) - survey_limit),
            'gap_total_count': len(gaps),
            'gap_source_range': {'start': 0, 'end': len(projected_gaps)},
            'gap_omitted_count': len(gaps) - len(projected_gaps),
            'instruction': 'Omitted evidence is not observed. Do not infer full-source coverage from this excerpt.',
        },
        'coverage': (artifacts.get('open_gaps') or {}).get('coverage') or {},
        'research_cycle': {
            key: (artifacts.get('research_cycle') or {}).get(key)
            for key in ('status', 'issues', 'next_action')
        },
        'accepted': accepted,
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
        'A frozen protocol decision_rule governs scientific_outcome; do not invent or change its direction, threshold, or uncertainty multiplier after measuring. '
        'Execution completion is not scientific support. Report scientific_outcome '
        'as supported, refuted, or inconclusive only from measured results. '

        'Claims must declare outcome_scope (supported/refuted/inconclusive): a '
        'negative result supports only a scoped negative claim, not the original '
        'hypothesis. Follow the current decision and prioritized next_actions; '
        'never execute a follow-up without owner confirmation. Structured result '
        'scientific_audit checks baseline_fairness, uncertainty, seeds and '
        'validation_split require evidence_refs and computable scientific_observations; '
        'a claimed verified status is not proof. Emit measurements in the tool result, '
        'not only the final model proposal. For run_command, emit one complete JSON '
        'object on stdout and send diagnostic logs to stderr; only an observed '
        'zero-exit result can supply measurements. Use the supplied scientific observation '
        'contract, actual paired metric values, the frozen protocol seeds and named '
        'baseline, and complete disjoint half-open dataset partition intervals. '
        'Report full-precision mean differences and standard errors; the server '
        'recomputes them. Consistency does not establish independent reproduction. '
        'When Tofu Python modules are available, lib.research.measurements.build_paired_observation '
        'aligns measured {seed,value} records and validates the payload. Supply observed '
        'comparison identities and partitions; it does not discover them or certify execution. '
        'When measurement_scope freezes candidate_digest and baseline_digest, report '
        'the inspected executed generations in comparison.method.system_digest and '
        'comparison.baseline.system_digest. Compare them with the expected identities; '
        'do not copy expected digests without inspecting the actual generation or hide '
        'a mismatch. Unknown generation identities remain unverified. '
        'Compare a baseline and a falsifier under matched budgets including '
        'detector, tool, token, compute and wall-time costs. Separate exploratory '
        'tuning from held-out validation; do not tune on validation results. '
    )
    instructions = {
        'experiment': (
            'Execute the smallest decisive experiment allowed by the frozen '
            'protocol and central hypothesis. Do not return revisions to them; a changed plan requires a new confirmed workspace revision. Return {"summary":str,"run":'
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
            task['direction'], task['lang'], int(task['user_id']), workspace),
        'capability_bindings': bindings,
    }
    if action in {'experiment', 'analyze'}:
        from .observation_audit import observation_contract
        payload['scientific_observation_contract'] = observation_contract(workspace.get('protocol'))
    return [
        {'role': 'system', 'content': (
            'You are Tofu Research Foundry, an evidence-first autonomous '
            'research agent. ' + _action_instructions(action))},
        {'role': 'user', 'content': json.dumps(
            payload, ensure_ascii=False, separators=(',', ':'), default=str)},
    ]


def _observed_tool_payload(row: Mapping[str, Any], content: str):
    """Decode complete JSON evidence, respecting the command producer envelope."""
    command_state = None
    body = content
    if (row.get('toolName') in {'run_command', paper_effective_tool_name('run_command')}
            and content.startswith('$ ')):
        from lib.background_command_projection import command_result_status
        outcome = command_result_status(content)
        if not outcome:
            return None, 'pending'
        if outcome['exitCode'] != 0 or outcome.get('timedOut') or outcome.get('interrupted'):
            return None, 'error'
        command_state = 'done'
        arguments = row.get('toolArgs')
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                arguments = None
        if isinstance(arguments, Mapping) and isinstance(arguments.get('command'), str):
            from lib.project_mod.run_command import _display_command
            header = '$ ' + _display_command(arguments['command']) + '\n'
            if not content.startswith(header):
                return None, 'error'
            body = content[len(header):]
        else:
            _, separator, body = content.partition('\n')
            if not separator:
                return None, 'error'
        body = body[:body.rfind('\n[exit code: ')]
        body = body.partition('\n[stderr]\n')[0]
    try:
        return decode_observation_json(body), command_state
    except (TypeError, ValueError, RecursionError):
        # A log prefix, multiple JSON values or incomplete body is not repaired.
        return None, command_state


def _tool_receipts(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    receipts = []
    observed_at = int(time.time())
    from .program import MAX_TOOL_RECEIPTS
    if len(rounds) > MAX_TOOL_RECEIPTS:
        raise ValueError('research_tool_receipts_overflow: complete evidence requires paged receipt storage')
    for row in rounds:
        captured = row.get('_researchReceipt')
        if isinstance(captured, Mapping):
            receipt = dict(captured)
            artifact = row.get('outputArtifact')
            artifact = artifact if isinstance(artifact, Mapping) else {}
            reference = row.get('toolResultArtifactRef') or artifact.get('artifactRef')
            if reference:
                receipt['artifact_ref'] = str(reference)
            receipts.append(receipt)
            continue
        content = str(row.get('toolContent') or '')
        excerpt = content[:4_000]
        status = str(row.get('status') or '').strip().lower()
        structured, command_state = _observed_tool_payload(row, content)
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
        elif command_state == 'error' or status in {'error', 'failed', 'aborted', 'cancelled', 'canceled'} or structured_error or excerpt.lower().startswith((
                'error:', 'error ', 'mcp tool error:', 'unknown tool:',
                'failed:', 'failure:')):
            normalized_status = 'error'
        elif command_state == 'pending':
            normalized_status = 'pending'
        elif status == 'done':
            normalized_status = 'done'
        else:
            normalized_status = 'pending'
        # Fingerprint the complete observation without allocating a full UTF-8 copy.
        hasher = hashlib.sha256()
        for offset in range(0, len(content), 65_536):
            hasher.update(content[offset:offset + 65_536].encode('utf-8'))
        digest = hasher.hexdigest()
        output_artifact = row.get('outputArtifact')
        output_artifact = output_artifact if isinstance(output_artifact, Mapping) else {}
        artifact_ref = str(
            row.get('toolResultArtifactRef')
            or output_artifact.get('artifactRef') or '')
        if not artifact_ref and normalized_status == 'done':
            artifact_ref = f'evidence:{digest[:24]}'
        receipt = {
            'tool': str(row.get('toolName') or ''),
            'call_id': str(row.get('toolCallId') or ''),
            'status': normalized_status,
            'artifact_ref': artifact_ref,
            'result_digest': digest,
            'result_excerpt': excerpt,
            'observed_at': observed_at,
        }
        from .execution_observation import extract_execution_observation
        execution = extract_execution_observation(structured)
        if execution is None and command_state is not None:
            execution = {'state': {'done': 'completed', 'error': 'failed', 'pending': 'planned'}[command_state]}
        if execution is not None:
            receipt['execution_observation'] = execution
        scientific_keys = ('scientific_observations', 'scientific_audit', 'scientific_outcome')
        if isinstance(structured, Mapping) and any(key in structured for key in scientific_keys):
            measurement = {key: structured[key] for key in scientific_keys if key in structured}
            compact = json.dumps(measurement, ensure_ascii=False, separators=(',', ':'))
            if len(compact) <= 4_000:
                receipt['observation_excerpt'] = compact
            else:
                receipt['observation_incomplete'] = True
        receipts.append(receipt)
    return receipts


def capture_research_result(round_entry: dict, result: Any) -> None:
    """Freeze observed evidence before model-visible result budgets rewrite text."""
    raw = dict(round_entry, toolContent=str(result))
    raw.pop('_researchReceipt', None)
    receipt = _tool_receipts([raw])[0]
    round_entry['_researchReceipt'] = receipt
    round_entry['toolContent'] = receipt['result_excerpt']



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
    for row in receipts:
        if row.get('status') != 'done':
            continue
        execution = row.get('execution_observation')
        if isinstance(execution, Mapping) and execution.get('job_id') == reference:
            return reference
        excerpt = str(row.get('result_excerpt') or '')
        try:
            observed = decode_observation_json(excerpt)
        except (ValueError, TypeError, RecursionError):
            continue
        pending = [observed]
        while pending:
            value = pending.pop()
            if isinstance(value, str) and value == reference:
                return reference
            if isinstance(value, Mapping):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
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



def _retain_completed_run(workspace: Mapping[str, Any], run: dict) -> dict:
    """Model-proposed IDs never replace a different completed observation."""
    existing = {str(row.get('id')): row for row in workspace.get('runs') or []
                if isinstance(row, Mapping)}
    prior = existing.get(run['id'])
    if prior is None:
        return run
    completed = (prior.get('execution_status') == 'completed'
                 or (not prior.get('execution_status')
                     and prior.get('status') in {'passed', 'inconclusive'}))
    identity = run_result_identity(run)
    if not completed:
        return run
    if run_result_identity(prior) == identity:
        return copy.deepcopy(dict(prior))
    witness = json.dumps([run['id'], identity], separators=(',', ':')).encode('utf-8')
    retained = dict(run, id='run-' + hashlib.sha256(witness).hexdigest()[:40])
    collision = existing.get(retained['id'])
    if collision is not None:
        if run_result_identity(collision) != identity:
            raise ValueError('Research result identity conflicts with retained evidence')
        return copy.deepcopy(dict(collision))
    return retained


def _receipt_facts(row):
    return {key: value for key, value in row.items() if key != 'observed_at'}


def _completed_receipt_replay(runs, receipts):
    """Recognize consumed explicit sources independently of model-proposed IDs."""
    if not receipts:
        return False
    consumed = {}
    for run in runs:
        if run.get('execution_status') != 'completed':
            continue
        for row in run.get('tool_receipts') or []:
            identity = (row.get('source_task_id'), row.get('call_id'))
            if identity[0] and identity[1]:
                consumed.setdefault(identity, []).append(row)
    all_consumed = True
    for incoming in receipts:
        identity = (incoming.get('source_task_id'), incoming.get('call_id'))
        matches = consumed.get(identity, []) if identity[0] and identity[1] else []
        if not matches:
            all_consumed = False
        elif any(_receipt_facts(row) != _receipt_facts(incoming) for row in matches):
            raise ValueError('Research receipt source identity conflicts with retained observation')
    return all_consumed


def _continue_pending_receipts(prior, receipts, workspace, history_reader=None, history_observer=None, indexed_progress=None):
    """Carry immutable unfinished-run observations across action boundaries."""
    if prior is None or prior.get('execution_status') not in {'planned', 'queued', 'running'}:
        return receipts
    paged = prior.get('receipt_history', {}).get('head')
    if paged and history_reader is None and indexed_progress is None:
        raise ValueError('Research paged receipt continuation requires a complete streamed evidence reducer')
    basis = dependency_digests(workspace)
    if any(prior.get(key) and prior[key] != value for key, value in basis.items()):
        raise ValueError('Research pending run cannot be rebound to a different scientific basis')
    if paged:
        from .program import _normalize_tool_receipts
        receipts = _normalize_tool_receipts(receipts)
        wanted = {}
        for row in receipts:
            if row.get('source_task_id') and row.get('call_id'):
                wanted.setdefault((row['source_task_id'], row['call_id']), []).append(row)
        seen = set()
        if indexed_progress is not None:
            from .source_index import receipt_digest
            repository, checkpoint = indexed_progress
            for identity, incoming_rows in wanted.items():
                found = repository.indexes.lookup_source(checkpoint['index'], incoming_rows[0])
                if found is not None:
                    if any(receipt_digest(row) != found for row in incoming_rows):
                        raise ValueError('Research receipt source identity conflicts with retained observation')
                    seen.add(identity)
        else:
            with history_reader(prior) as historical:
                for old in historical:
                    identity = (old.get('source_task_id'), old.get('call_id'))
                    matches = wanted.get(identity, [])
                    if matches:
                        if any(_receipt_facts(old) != _receipt_facts(incoming) for incoming in matches):
                            raise ValueError('Research receipt source identity conflicts with retained observation')
                        seen.add(identity)
                    if history_observer is not None:
                        history_observer(old)
        receipts = [row for row in receipts if (row.get('source_task_id'), row.get('call_id')) not in seen]
    merged = copy.deepcopy(prior.get('tool_receipts') or [])
    for incoming in receipts:
        identity = (incoming.get('source_task_id'), incoming.get('call_id'))
        matches = [row for row in merged if identity[0] and identity[1]
                   and (row.get('source_task_id'), row.get('call_id')) == identity]
        if matches:
            if any(_receipt_facts(row) != _receipt_facts(incoming) for row in matches):
                raise ValueError('Research receipt source identity conflicts with retained observation')
            continue
        merged.append(copy.deepcopy(incoming))
    from .program import MAX_TOOL_RECEIPTS
    if len(merged) > MAX_TOOL_RECEIPTS:
        raise ValueError('research_tool_receipts_overflow: complete evidence requires paged receipt storage')
    return merged


def apply_action_result(
    *,
    action: str,
    workspace: Mapping[str, Any],
    payload: Mapping[str, Any],
    receipts: list[dict[str, Any]],
    task_id: str,
    bindings: list[dict[str, Any]],
    consumed_source_lookup=None,
    pending_receipt_reader=None,
    pending_progress_reader=None,
) -> dict[str, Any]:
    """Apply model proposals under deterministic evidence gates."""
    draft = copy.deepcopy(dict(workspace))
    now = int(time.time())
    done = [row for row in receipts if row.get('status') == 'done']
    if action == 'experiment':
        draft['runs'] = normalize_run_evidence(draft.get('runs'))
        # Execution cannot silently revise the owner-confirmed frozen protocol.
        proposed = payload.get('run') if isinstance(payload.get('run'), Mapping) else {}
        run_id = str(proposed.get('id') or (
            'run-' + hashlib.sha256(str(task_id).encode('utf-8')).hexdigest()[:40]))[:96]
        from .execution_observation import provider_scope_for_tool, normalize_execution_observation
        receipts = copy.deepcopy(receipts)
        for receipt in receipts:
            receipt['source_task_id'] = task_id
            observation = receipt.get('execution_observation')
            if isinstance(observation, Mapping):
                observation = dict(observation)
                scope = provider_scope_for_tool(receipt.get('tool') or '', bindings)
                if observation.get('provider_scope') not in (None, scope):
                    raise ValueError('Research execution observation cannot replace its provider scope')
                observation['provider_scope'] = scope
                receipt['execution_observation'] = normalize_execution_observation(observation)
        prior = next((row for row in draft['runs'] if row['id'] == run_id), None)
        paged_pending = bool(prior and prior.get('receipt_history', {}).get('head')
                             and prior.get('execution_status') in {'planned', 'queued', 'running'})
        if (_completed_receipt_replay(draft['runs'], receipts)
                or (consumed_source_lookup is not None and consumed_source_lookup(receipts))):
            draft.update(normalize_program_fields(draft))
            return draft
        from .execution_observation import (iter_execution_states, reduce_execution_state,
                                             execution_state_parser, ExecutionStateReducer)
        indexed_progress = pending_progress_reader(prior) if paged_pending and prior.get('receipt_progress') and pending_progress_reader else None
        historical_state = ExecutionStateReducer() if paged_pending else None
        parse_history = execution_state_parser(bindings)
        def observe_history(row):
            state = parse_history(row)
            if state is not None:
                historical_state.observe(row, state)
        receipts = _continue_pending_receipts(prior, receipts, workspace, pending_receipt_reader,
            history_observer=observe_history if paged_pending else None, indexed_progress=indexed_progress)

        parsed_states = list(iter_execution_states(receipts, bindings))
        execution_receipts = [row for row, _ in parsed_states]
        execution_states = [state for _, state in parsed_states]
        execution_status = reduce_execution_state(iter(parsed_states))
        if indexed_progress is not None:
            repository, checkpoint = indexed_progress
            execution_status = repository.indexes.preview_status(checkpoint['index'], receipts, bindings)
        elif paged_pending:
            for row, state in parsed_states:
                historical_state.observe(row, state)
            execution_status = historical_state.status
        evidence_receipts = [
            row for row, state in zip(execution_receipts, execution_states)
            if state == 'completed' and row.get('status') == 'done'
        ]
        if execution_status != 'completed':
            evidence_receipts = []
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
        if execution_status == 'failed' and prior and prior.get('receipt_history', {}).get('head'):
            requested_status = 'failed'
        spec_digest = hashlib.sha256(json.dumps({
            'hypothesis': draft.get('hypothesis'),
            'protocol': draft.get('protocol'),
            'label': proposed.get('label'),
        }, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()
        remote_job_id = _observed_reference(
            proposed.get('remote_job_id'), execution_receipts)
        backend = next((
            str(row.get('tool') or '') for row in execution_receipts
            if row.get('tool')), '')
        outcomes = set()
        for receipt in evidence_receipts:
            try:
                observed = decode_observation_json(str(receipt.get('observation_excerpt') or receipt.get('result_excerpt') or ''))
            except (ValueError, TypeError):
                continue
            if (isinstance(observed, Mapping) and isinstance(observed.get('scientific_outcome'), str)
                    and observed['scientific_outcome'] in {'supported', 'refuted', 'inconclusive'}):
                outcomes.add(observed['scientific_outcome'])
        outcome = next(iter(outcomes)) if len(outcomes) == 1 else 'inconclusive'
        if any(row.get('observation_incomplete') for row in evidence_receipts):
            outcome = 'inconclusive'
        run = dict(proposed) | {
            'id': run_id,
            'status': requested_status,
            **dependency_digests(workspace),
            'execution_status': execution_status,
            'scientific_outcome': outcome,
            'artifact_ref': evidence_refs[0] if evidence_refs else '',
            'artifact_refs': evidence_refs,
            'task_id': task_id,
            'backend': backend,
            'remote_job_id': remote_job_id,
            'spec_digest': spec_digest,
            'tool_receipts': receipts,
            **({'receipt_history': copy.deepcopy(prior['receipt_history'])}
               if paged_pending else {}),
            'finished_at': now if execution_status in {'completed', 'failed'} else 0,
            'updated_at': now,
        }
        run.pop('receipt_evidence', None)
        run.pop('receipt_progress', None)
        if paged_pending and prior.get('receipt_progress'):
            run['receipt_progress'] = prior['receipt_progress']
        if not paged_pending:
            run.pop('receipt_history', None)
        run = _retain_completed_run(draft, run)
        draft['runs'] = _merge_by_id(draft.get('runs'), [run], prefix='run')
        proposed_claims = copy.deepcopy(payload.get('claims') or [])
        if run['id'] != run_id and isinstance(proposed_claims, list):
            for claim in proposed_claims:
                if isinstance(claim, dict):
                    claim['evidence_refs'] = [run['id'] if ref == run_id else ref
                                             for ref in list(claim.get('evidence_refs') or [])]
        claims = _sanitize_claims(
            proposed_claims, receipts=evidence_receipts,
            allowed_refs=set(evidence_refs) | {run['id'], task_id})
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
        from .program import scientific_dependency_digest

        reviewed = isinstance(proposed, Mapping) and any(
            key != 'scientific_dependency_digest' for key in proposed)
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
                reviewed = True
                current[path] = {
                    'path': path, 'content': str(row.get('content') or ''),
                    'updated_at': now,
                }
        draft['source_files'] = list(current.values())
        draft['source_files'] = scaffold_source_files(draft)

        # Snapshot is application-owned, never accepted from model output.
        draft['manuscript']['scientific_dependency_digest'] = (
            scientific_dependency_digest(draft) if reviewed else
            (workspace.get('manuscript') or {}).get('scientific_dependency_digest', ''))
        draft['stage'] = 'writing'
    elif action == 'compile':
        raise ValueError('Model output cannot create a Research compilation receipt')
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
    if action in {'experiment', 'analyze'}:
        # Stamp only newly reviewed proposals, never rebind existing claims on edit.
        proposed_ids = {str(c.get('id') or '') for c in claims}
        normalized_runs = normalize_program_fields(draft)['runs']
        for claim in draft.get('claims') or []:
            if claim.get('id') in proposed_ids:
                claim.update(dependency_digests(draft))
                claim['evidence_digest'] = claim_evidence_digest(claim, normalized_runs)
    normalized = normalize_program_fields(draft)
    for key, value in normalized.items():
        draft[key] = value
    return draft


def _run_agent(task: dict, workspace: Mapping[str, Any], epoch: PaperToolEpochV2,
               bindings: list[dict[str, Any]], *, continuation=None) -> tuple[dict, list[dict]]:
    from lib.llm_dispatch.api import dispatch_stream
    from lib.llm.stream_result import ensure_provider_stream_result
    from lib.research.action_runtime import append_action_event
    from lib.storage.errors import StorageError
    from lib.storage.retry import DEFAULT_TRANSIENT_DELAYS_S, TRANSIENT_STORAGE_CODES

    if continuation is not None:
        continuation.validate_task(task)
        if epoch.epoch_hash != continuation.epoch.epoch_hash:
            raise StorageError('database_conflict', 'Research continuation tool epoch changed')
        from .loop_checkpoint import LoopCheckpoints, NAMESPACE as CHECKPOINT_NAMESPACE
        checkpoints = LoopCheckpoints.open(continuation.claim.repository.client,
            user_id=task['user_id'], task_id=task['task_id'])
        restored, restored_meter = checkpoints.restore_checkpoint(continuation.ordinal,
            fallback_model=continuation.model_name)
        manifest = checkpoints._get(CHECKPOINT_NAMESPACE,
            checkpoints.prefix + str(continuation.ordinal), 'loopCheckpoint')
        if manifest['material_digest'] != continuation.checkpoint_digest:
            raise StorageError('database_integrity', 'Research continuation checkpoint changed')
        task['tool_rounds'][:] = restored['tool_rounds']
        messages = restored['messages']
    else:
        messages = _messages(task, workspace, bindings)
    abort = AbortSignal.from_event(task['abort_event'])
    model_name = continuation.model_name if continuation is not None else str(task.get('model') or _lib.LLM_MODEL)
    rounds: list[dict[str, Any]] = task['tool_rounds']
    terminal = {'content': ''}
    result_budget = PaperToolResultBudgetV2(
        owner_user_id=int(task['user_id']), model=model_name,
        result_envelope=epoch.result_envelope, conv_id=task['task_id'])
    shim = make_paper_exec_shim(
        task_id=task['task_id'], conv_id=task['task_id'], abort=abort.is_set,
        owner_user_id=int(task['user_id']), tool_epoch=epoch, model=model_name)
    from .action_budget import normalize_action_logical_budget
    budget = normalize_action_logical_budget(task.get('logical_budget'))
    meter = restored_meter if continuation is not None else PaperAgentUsageMeter(
        'research-action', token_budget=budget['token_budget'],
        dispatch_budget=budget['dispatch_budget'], repeat_limit=0, fallback_model=model_name)
    model_admissions = {}
    if continuation is not None:
        task['agentUsageV1'] = meter.snapshot()
    execution_claim = continuation.claim if continuation is not None else None
    messages_prepared = continuation is not None
    latest_checkpoint_ordinal = continuation.ordinal if continuation is not None else 0
    recovery_frontier = 0
    recovery_attempt = 0
    recovery_pending = continuation is not None
    before_admission = False
    resume_chassis = restored['chassis'] if continuation is not None else None
    if continuation is not None:
        del restored

    def dispatch(round_index, tools):
        nonlocal execution_claim, messages_prepared, before_admission, recovery_pending
        before_admission = True
        from .context import output_budget, prepare_action_messages
        max_output = output_budget(model_name)
        if not messages_prepared:
            messages[:] = prepare_action_messages(messages, model=model_name,
                tools=tools, user_id=int(task['user_id']))
            messages_prepared = True
        else:
            from .history_context import prepare_history_messages
            def load_acknowledged_history(ordinal):
                execution_claim.assert_current()
                from .loop_checkpoint import LoopCheckpoints
                checkpoints = LoopCheckpoints.open(execution_claim.repository.client,
                    user_id=task['user_id'], task_id=task['task_id'])
                if checkpoints is None:
                    raise ValueError('research history frozen basis is unavailable')
                return checkpoints.load_checkpoint(ordinal)
            messages[:] = prepare_history_messages(messages, model=model_name, tools=tools,
                max_output=max_output, user_id=task['user_id'], task_id=task['task_id'],
                checkpoint_ordinal=meter.agent_dispatches - 1, load_checkpoint=load_acknowledged_history)
        from .action_observations import freeze_action_observation_basis
        from .model_accounting import ModelCalls
        from .request_context import RequestContexts
        frozen = freeze_action_observation_basis(task, workspace)
        if execution_claim is None:
            from .execution_owner import ActionExecutionOwners
            owners = ActionExecutionOwners.open(frozen.client, user_id=task['user_id'], task_id=task['task_id'])
            execution_claim = owners.claim_initial()
        execution_claim.assert_current()
        repository = ModelCalls.open(frozen.client, user_id=task['user_id'], task_id=task['task_id'])
        contexts = RequestContexts.open(frozen.client, user_id=task['user_id'], task_id=task['task_id'])
        definition = {'direction':task['direction'], 'lang':task.get('lang', frozen.binding['lang']),
            'action':task['action'], 'expected_revision':task.get('expected_revision', frozen.binding['revision']),
            'confirm_external_writes':task.get('confirm_external_writes', False),
            'model_override':str(task.get('model') or ''), 'logical_budget':copy.deepcopy(budget),
            'tool_epoch_hash':str(getattr(epoch, 'epoch_hash', ''))}
        request_digest = contexts.save_request({'messages': messages, 'tools': tools,
            'max_output': max_output, 'action_definition':definition})
        intent = repository.admit_model_call(ordinal=meter.agent_dispatches, round_index=round_index,
            requested_model=model_name, max_output=max_output, usage_before=meter.snapshot(),
            request_digest=request_digest, request_snapshot_version=1, response_snapshot_version=1, loop_checkpoint_version=1)
        before_admission = False
        if recovery_pending:
            recovery_pending = False
            append_action_event(task, build_phase(Phase.AGENT, action=task['action'],
                roundNum=round_index + 1,
                detail='Continuing from the saved step after a temporary interruption.'))
        model_admissions[round_index] = (repository, intent)
        execution_claim.assert_current()
        return ensure_provider_stream_result(dispatch_stream(
            messages,
            on_content=lambda text: append_action_event(task, {
                'type': 'delta', 'delta': text, 'llmRound': round_index}),
            on_thinking=lambda text: append_action_event(task, {
                'type': 'thinking', 'delta': text, 'llmRound': round_index}),
            abort_check=abort.is_set,
            prefer_model=model_name,
            strict_model=bool(definition['model_override']),
            tools=tools, max_tokens=max_output, temperature=0,
            thinking_enabled=False, log_prefix='[ResearchAction]',
        ))

    def on_result(_round, message, _finish, _usage):
        if isinstance(message, Mapping) and not message.get('tool_calls'):
            terminal['content'] = str(message.get('content') or '')
        task['agentUsageV1'] = meter.snapshot()
        if _round in model_admissions:
            repository, intent = model_admissions[_round]
            from .model_response import ModelResponses
            responses = ModelResponses.open(repository.client, user_id=task['user_id'], task_id=task['task_id'])
            response_digest = responses.save_response(intent, message, _finish)
            repository.record_model_usage(intent, task['agentUsageV1'],
                usage_mapping_received=isinstance(_usage, Mapping), response_digest=response_digest)

    def on_checkpoint(chassis):
        nonlocal latest_checkpoint_ordinal
        if execution_claim is not None:
            execution_claim.assert_current()
        from .loop_checkpoint import LoopCheckpoints
        repository, intent = model_admissions[chassis['next_round'] - 1]
        checkpoints = LoopCheckpoints.open(repository.client, user_id=task['user_id'], task_id=task['task_id'])
        checkpoints.save_checkpoint(intent, {'messages': messages, 'tool_rounds': rounds,
            'usage_snapshot': meter.snapshot(), 'chassis': chassis})
        latest_checkpoint_ordinal = intent['ordinal']
        # The acknowledged journal owns completed admissions. Keep only the
        # current in-flight call in memory; recovery reads the durable checkpoint.
        del model_admissions[chassis['next_round'] - 1]

    def on_tool_round(_round, message):
        messages.append(message)

    def execute_tool(round_index, tool_call):
        if execution_claim is not None:
            execution_claim.assert_current()
        function = tool_call.get('function') or {}
        name = str(function.get('name') or '')
        raw_args = function.get('arguments') or '{}'
        call_id = str(tool_call.get('id') or '')
        args, _repair = parse_and_repair_tool_args(name, raw_args)
        args = _merge_bound_arguments(name, args, bindings)
        from lib.tasks_pkg.tool_display import tool_round_label
        round_entry = {
            'query': tool_round_label(name, args),
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
        observation_repository = None
        if task['action'] == 'experiment':
            from .action_observations import freeze_action_observation_basis
            observation_repository = freeze_action_observation_basis(task, workspace)
            observation_repository.record_intent(ordinal=round_entry['roundNum'],
                call_id=round_entry['toolCallId'], tool=round_entry['toolName'], arguments=args)
        started = time.time()
        if name in {'read_tool_artifact', 'search_tool_artifact'}:
            from .context import restore_source_reference
            from .history_context import restore_history_reference
            def load_history_checkpoint(ordinal):
                if abort.is_set():
                    raise AbortedError('research action aborted during history restoration')
                execution_claim.assert_current()
                from .loop_checkpoint import LoopCheckpoints
                checkpoints = LoopCheckpoints.open(execution_claim.repository.client,
                    user_id=task['user_id'], task_id=task['task_id'])
                if checkpoints is None:
                    raise ValueError('research history frozen basis is unavailable')
                return checkpoints.load_checkpoint(ordinal)
            restored = restore_history_reference(messages, user_id=int(task['user_id']),
                task_id=task['task_id'], artifact_ref=args.get('artifact_ref'),
                load_checkpoint=load_history_checkpoint)
            if not restored:
                restored = restore_source_reference(messages, user_id=int(task['user_id']),
                    artifact_ref=args.get('artifact_ref'), load_source=lambda: _research_context(
                        task['direction'], task['lang'], int(task['user_id']), workspace))
            if restored:
                round_entry['sourceReferenceRestored'] = True
                note = ('Research source cache was restored from its digest-checked durable original. '
                        'Previous continuation cursors were not renewed. If an old cursor is rejected, '
                        'start read_tool_artifact again at cursor 0 with the same artifact_ref and follow '
                        'the new cursors. This does not rerun the source producer or verify its scientific claims.')
                if note not in messages[0]['content']:
                    messages[0]['content'] += '\n' + note
            if execution_claim is not None:
                execution_claim.assert_current()
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
        capture_research_result(round_entry, result)
        if observation_repository is not None:
            observation_repository.capture(ordinal=round_entry['roundNum'],
                call_id=round_entry['toolCallId'], tool=round_entry['toolName'],
                content=str(result), receipt=round_entry['_researchReceipt'])
        round_entry['_elapsed'] = f'{elapsed:.1f}s'
        visible_result = result_budget.append(
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
        return visible_result

    try:
        while True:
            try:
                outcome = run_guarded_paper_agent_loop(
                    context=f'Research {task["action"]} agent', usage_meter=meter,
                    abort=abort, round_tools=list(epoch.wire_schemas), dispatch=dispatch,
                    execute_tool=execute_tool, on_round_result=on_result,
                    on_tool_round=on_tool_round, on_round_end=result_budget.finish_round,
                    on_checkpoint=on_checkpoint, resume_checkpoint=resume_chassis,
                )
                break
            except StorageError as exc:
                # Only this caller's unpaid frontier can be retried. Every paid
                # admission, tool and checkpoint failure retains its original error.
                if (not before_admission or not exc.retryable
                        or exc.code not in TRANSIENT_STORAGE_CODES
                        or not latest_checkpoint_ordinal
                        or execution_claim is None or execution_claim.value['process'] is None):
                    raise
                if abort.is_set():
                    raise AbortedError('research action aborted during checkpoint recovery') from exc
                if recovery_frontier != latest_checkpoint_ordinal:
                    recovery_frontier = latest_checkpoint_ordinal
                    recovery_attempt = 0
                    append_action_event(task, build_phase(Phase.AGENT, action=task['action'],
                        detail='Waiting for storage before continuing from the saved step.'))
                recovery_error = exc
                while True:
                    material = None
                    recovery_attempt += 1
                    delay = DEFAULT_TRANSIENT_DELAYS_S[min(recovery_attempt - 1, len(DEFAULT_TRANSIENT_DELAYS_S) - 1)]
                    if type(recovery_error.retry_after_ms) is int:
                        delay = max(delay, recovery_error.retry_after_ms / 1000)
                    if task['abort_event'].wait(delay):
                        raise AbortedError('research action aborted during checkpoint recovery') from exc
                    try:
                        from lib.storage import get_storage_client
                        from .execution_owner import ActionExecutionOwners, ExecutionClaim
                        from .loop_checkpoint import LoopCheckpoints
                        client = get_storage_client(write=True)
                        owners = ActionExecutionOwners.open(client, user_id=task['user_id'], task_id=task['task_id'])
                        if owners is None:
                            raise StorageError('database_integrity', 'Research recovery binding is missing')
                        current_claim = ExecutionClaim(owners, execution_claim.value)
                        current_claim.assert_current()
                        checkpoints = LoopCheckpoints.open(client, user_id=task['user_id'], task_id=task['task_id'])
                        if checkpoints is None:
                            raise StorageError('database_integrity', 'Research recovery checkpoint binding is missing')
                        material, restored_meter = checkpoints.restore_checkpoint(
                            latest_checkpoint_ordinal, fallback_model=model_name)
                        saved_usage = restored_meter.snapshot()
                        if (material['tool_rounds'] != rounds
                                or saved_usage['agent_token_budget'] != budget['token_budget']
                                or saved_usage['agent_dispatch_budget'] != budget['dispatch_budget']):
                            raise StorageError('database_integrity', 'Research recovery differs from the live execution')
                        current_claim.assert_current()
                    except StorageError as probe_error:
                        if probe_error.retryable and probe_error.code in TRANSIENT_STORAGE_CODES:
                            recovery_error = probe_error
                            continue
                        raise exc
                    except ValueError:
                        raise exc
                    break
                # The restore port returns detached material; retain only the
                # resumed history/chassis, not a second full checkpoint copy.
                messages[:] = material['messages']
                meter = restored_meter
                execution_claim = current_claim
                resume_chassis = material['chassis']
                del material
                recovery_pending = True
                messages_prepared = True
    finally:
        task['_researchUsageSnapshot'] = meter.snapshot()
    if not outcome.completed:
        raise RuntimeError(f'research action did not complete: {outcome.exit_reason}')
    payload = extract_json(terminal['content'], repair=True)
    if not isinstance(payload, Mapping):
        raise ValueError('research action returned no valid JSON object')
    return dict(payload), _tool_receipts(rounds)


def _frozen_basis_identity(value, *, kind="protocol"):
    try:
        encoded=json.dumps(value,sort_keys=True,ensure_ascii=False,
            separators=(',',':'),allow_nan=False).encode('utf-8')
    except (TypeError,ValueError) as exc:
        raise ValueError(f'research_{kind}_nonportable') from exc
    return hashlib.sha256(encoded).hexdigest()


def _frozen_hypothesis_identity(workspace):
    from .program import hypothesis_identity_payload
    return _frozen_basis_identity(hypothesis_identity_payload(workspace),kind='hypothesis')


def require_experiment_decision_rule(workspace: Mapping[str, Any]) -> None:
    """Admission before effects; existing observations remain readable."""
    from .observation_audit import normalize_decision_rule
    protocol = workspace.get('protocol')
    if not isinstance(protocol, Mapping) or 'decision_rule' not in protocol:
        raise ValueError('research_decision_rule_required: freeze metric direction, '
                         'effect threshold and SE multiplier before execution')
    normalize_decision_rule(protocol['decision_rule'])
    seeds=protocol.get('random_seeds')
    if (not isinstance(seeds,list) or not 2<=len(seeds)<=32
            or any(type(seed) is not int for seed in seeds) or len(set(seeds))!=len(seeds)):
        raise ValueError('research_paired_seeds_required: freeze 2..32 distinct integer seeds before execution')
    missing=[key for key in ('dataset','primary_metric','baseline')
             if not isinstance(protocol.get(key),str) or not protocol[key].strip()]
    if missing:
        raise ValueError('research_measurement_context_required: freeze '+', '.join(missing)+' before execution')


def run_research_action(task: dict, *, continuation=None) -> None:
    from lib.research.action_runtime import (
        _research_action_runtime,
        append_action_event,
    )
    from lib.research.workspace import load_workspace, save_workspace

    task_id = task['task_id']
    try:
        if continuation is not None:
            continuation.validate_task(task)
        _research_action_runtime.mark_running(task_id)
        append_action_event(
            task, build_phase(Phase.START, action=task['action']))
        workspace = load_workspace(
            task['direction'], task['lang'], user_id=int(task['user_id']))
        if int(workspace.get('revision') or 0) != int(task['expected_revision']):
            raise ValueError('research_workspace_stale')
        if task['action'] == 'experiment':
            if task['abort_event'].is_set():
                raise AbortedError('research action aborted before experiment admission')
            from .workspace import validate_selection_source
            validate_selection_source(workspace, user_id=int(task['user_id']))
            require_experiment_decision_rule(workspace)
            frozen_protocol=_frozen_basis_identity(workspace['protocol'])
            frozen_hypothesis=_frozen_hypothesis_identity(workspace)
            from .history import compact_observations
            _, archive_candidates = compact_observations(workspace)
            if archive_candidates and continuation is None:
                if task['abort_event'].is_set():
                    raise AbortedError('research action aborted before history preparation')
                workspace = save_workspace(task['direction'], task['lang'], workspace,
                    expected_revision=int(task['expected_revision']), user_id=int(task['user_id']))
                task['expected_revision'] = workspace['revision']
            if (len(workspace.get('runs') or []) >= MAX_RUNS
                    and all(row.get('execution_status') == 'completed' for row in workspace['runs'])):
                raise ValueError('The current completed evidence basis is fully cited; revise current claims before dispatching another experiment')
        if task['abort_event'].is_set():
            raise AbortedError('research action aborted after history preparation')
        if task['action'] in {'compile', 'publish'} and not workspace.get('source_files'):
            raise ValueError(f'action={task["action"]!r} requires manuscript source files')
        with ExitStack() as resources:
            compilation_result = None
            if task['action'] == 'compile':
                from .compilation import compile_workspace
                compilation_result = resources.enter_context(compile_workspace(
                    workspace, direction=task['direction'], lang=task['lang'],
                    owner_user_id=int(task['user_id']), task_id=task_id,
                    cancel=task['abort_event']))
                updated = copy.deepcopy(workspace)
                updated['stage'] = 'writing'
                payload = {'summary': compilation_result.projection['detail'] or 'Compiled manuscript PDF'}
                receipts, binding_problems = [], []
            else:
                if continuation is not None:
                    epoch, bindings, binding_problems = (
                        continuation.epoch, continuation.bindings, continuation.binding_problems)
                else:
                    epoch, bindings, binding_problems = build_action_tool_epoch(
                        action=task['action'], user_id=int(task['user_id']),
                        workspace=workspace,
                        confirm_external_writes=bool(task['confirm_external_writes']),
                        model=str(task.get('model') or ''),
                    )
                if task['abort_event'].is_set():
                    raise AbortedError('research action aborted before agent dispatch')
                if task['action']=='experiment' and _frozen_basis_identity(workspace.get('protocol'))!=frozen_protocol:
                    raise ValueError('research_protocol_changed_during_execution')
                if task['action']=='experiment' and _frozen_hypothesis_identity(workspace)!=frozen_hypothesis:
                    raise ValueError('research_hypothesis_changed_during_execution')
                task['toolEpochV2'] = epoch.telemetry()
                task['bindingProblems'] = binding_problems
                append_action_event(
                    task, build_phase(Phase.AGENT, action=task['action'],
                                      toolCount=len(epoch.executable_schemas)))
                if continuation is None:
                    payload, receipts = _run_agent(task, workspace, epoch, bindings)
                else:
                    payload, receipts = _run_agent(task, workspace, epoch, bindings, continuation=continuation)
                if task['abort_event'].is_set():
                    raise AbortedError('research action aborted before applying result')
                if task['action']=='experiment' and _frozen_basis_identity(workspace.get('protocol'))!=frozen_protocol:
                    raise ValueError('research_protocol_changed_during_execution')
                if task['action']=='experiment' and _frozen_hypothesis_identity(workspace)!=frozen_hypothesis:
                    raise ValueError('research_hypothesis_changed_during_execution')
                from .source_index import ReceiptSourceIndex, empty_reference, legacy_archive_replay
                source_reference = workspace.get('history', {}).get('source_index', empty_reference(0))
                lookup = None
                if task['action'] == 'experiment' and (source_reference['root'] or
                        (source_reference['version'] == 0 and workspace.get('history', {}).get('head'))):
                    from .workspace import _storage
                    from .persistence import research_direction_hash
                    from .history import ResearchHistoryRepository
                    arguments = {'user_id': int(task['user_id']),
                                 'paper_hash': research_direction_hash(task['direction']), 'lang': task['lang']}
                    if source_reference['version'] == 1:
                        source_index = ReceiptSourceIndex(_storage(), **arguments)
                        lookup = lambda rows: source_index.replay(source_reference, rows)
                    else:
                        from .receipt_pages import receipt_reader
                        archive = ResearchHistoryRepository(_storage(), **arguments)
                        complete_reader = receipt_reader(_storage(), **arguments)
                        lookup = lambda rows: legacy_archive_replay(archive, workspace['history'], rows,
                                                                   receipt_reader=complete_reader)
                def pending_reader(run):
                    from .workspace import _storage
                    from .receipt_pages import ReceiptPages
                    from .persistence import research_direction_hash
                    return ReceiptPages(_storage(), user_id=int(task['user_id']),
                        paper_hash=research_direction_hash(task['direction']), lang=task['lang'],
                        run_id=run['id'], hypothesis_digest=run['hypothesis_digest'],
                        protocol_digest=run['protocol_digest']).open_receipts(run['receipt_history'])
                def pending_progress(run):
                    from .workspace import _storage
                    from .progress_checkpoint import ProgressCheckpointRepository
                    from .persistence import research_direction_hash
                    repository = ProgressCheckpointRepository(_storage(), user_id=int(task['user_id']),
                        paper_hash=research_direction_hash(task['direction']),lang=task['lang'],run=run)
                    return repository, repository.read(run)
                updated = apply_action_result(
                    action=task['action'], workspace=workspace, payload=payload,
                    receipts=receipts, task_id=task_id, bindings=bindings, consumed_source_lookup=lookup,
                    pending_receipt_reader=pending_reader if task['action'] == 'experiment' else None,
                    pending_progress_reader=pending_progress if task['action'] == 'experiment' else None)
            if task['abort_event'].is_set():
                raise AbortedError('research action aborted before saving workspace')
            if task['action']=='experiment' and _frozen_basis_identity(updated.get('protocol'))!=frozen_protocol:
                raise ValueError('research_protocol_changed_before_commit')
            if task['action']=='experiment' and _frozen_hypothesis_identity(updated)!=frozen_hypothesis:
                raise ValueError('research_hypothesis_changed_before_commit')
            success_settlement = None
            completed_run = next((run for run in updated.get('runs', [])
                if task['action'] == 'experiment' and run.get('task_id') == task_id
                and run.get('execution_status') == 'completed'
                and run.get('receipt_history', {}).get('head')), None)
            if completed_run is not None:
                from .receipt_settlement import prepare_success_settlement
                from .workspace import _storage
                from .persistence import research_direction_hash
                success_settlement = prepare_success_settlement(_storage(), updated, workspace,
                    user_id=int(task['user_id']), paper_hash=research_direction_hash(task['direction']),
                    lang=task['lang'], run_id=completed_run['id'], revision=int(task['expected_revision']))
            failure_settlement = None
            failed_run = next((run for run in updated.get('runs', [])
                if task['action'] == 'experiment' and run.get('task_id') == task_id
                and run.get('execution_status') == 'failed'
                and run.get('receipt_history', {}).get('head')), None)
            if failed_run is not None:
                from .receipt_settlement import prepare_failure_settlement
                from .workspace import _storage
                from .persistence import research_direction_hash
                failure_settlement = prepare_failure_settlement(_storage(), updated, workspace,
                    user_id=int(task['user_id']), paper_hash=research_direction_hash(task['direction']),
                    lang=task['lang'], run_id=failed_run['id'], revision=int(task['expected_revision']))
            saved = save_workspace(
                task['direction'], task['lang'], updated,
                expected_revision=int(task['expected_revision']),
                user_id=int(task['user_id']), compilation_result=compilation_result,
                failure_settlement=failure_settlement, success_settlement=success_settlement,
                paginate_pending_run_id=next((run['id'] for run in updated.get('runs', [])
                    if task['action'] == 'experiment' and run.get('task_id') == task_id
                    and run.get('execution_status') in {'planned', 'queued', 'running'}), None),
            )
            task['_committedWorkspaceRevision'] = saved['revision']
        result = {
            'action': task['action'], 'summary': str(payload.get('summary') or ''),
            'workspace': saved, 'readiness': readiness(saved),
            'tool_receipts': receipts, 'binding_problems': binding_problems,
            **({'usage_snapshot': copy.deepcopy(task['_researchUsageSnapshot'])}
               if '_researchUsageSnapshot' in task else {}),
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
    logical_budget: dict | None = None,
    user_id: int,
) -> dict[str, Any]:
    """Validate, create, and spawn one explicit research action."""
    del conv_id
    from lib.research.action_runtime import (
        _production,
        _research_action_runtime,
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
    from .action_budget import normalize_action_logical_budget
    budget = normalize_action_logical_budget(logical_budget)
    _production.cleanup_stale()
    from .persistence import research_direction_hash
    requested_model = str(model or '')[:240]
    key = (owner, research_direction_hash(clean_direction), 'zh' if lang == 'zh' else 'en',
           clean_action, expected_revision, requested_model if clean_action != 'compile' else '',
           tuple(sorted(budget.items())) if clean_action != 'compile' else ())
    existing = _production.index_get(key)
    if existing:
        return {'task_id': existing, 'deduped': True}
    from .workspace import load_workspace
    workspace = load_workspace(clean_direction, 'zh' if lang == 'zh' else 'en', user_id=owner)
    if workspace['revision'] != expected_revision:
        raise ValueError('research_workspace_stale')
    if clean_action == 'experiment':
        from .workspace import validate_selection_source
        validate_selection_source(workspace, user_id=owner)
        require_experiment_decision_rule(workspace)
    task_id = _production.new_task_id()
    task, existing = _production.claim_task(key, task_id, user_id=owner,
        meta={'direction': clean_direction, 'action': clean_action,
              'lang': 'zh' if lang == 'zh' else 'en'}, fields={
        'direction': clean_direction,
        'lang': 'zh' if lang == 'zh' else 'en',
        'action': clean_action,
        'expected_revision': expected_revision,
        'confirm_external_writes': bool(confirm_external_writes),
        'model': requested_model,
        'logical_budget': budget,
        'tool_rounds': [],
    })
    if existing:
        return {'task_id': existing, 'deduped': True}
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
