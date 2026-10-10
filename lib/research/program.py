"""Scientific research-program contract normalization and readiness.

This module is the application owner for the durable artifact that follows an
auto-research result: experiment protocol, capability bindings, run receipts,
claim/evidence links, a bounded LaTeX source tree, visuals, compilation, and
publication receipts.  The machine-readable authority is
``contracts/research_program_v1.schema.json``.  Storage selection and HTTP
delivery remain in their existing owners.
"""

from __future__ import annotations

import copy
import hashlib
import json
import posixpath
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .observation_audit import normalize_decision_rule, normalize_measurement_scope, normalize_observation_format

from .decisions import (
    normalize_portfolio, research_decision, manuscript_audit,
    authoritative, receipt_outcome_consistent, has_unresolved_evidence,
)

from .history import (summary_for_program, normalize_history, observation_commitment, MODULUS)

from .observation_audit import AUDIT_VERSION as OBSERVATION_AUDIT_VERSION

PROGRAM_CONTRACT_VERSION = 'tofu.research-program/v1'
MAX_PROGRAM_BYTES = 640 * 1024
MAX_SOURCE_FILES = 24
MAX_SOURCE_FILE_CHARS = 65_536
MAX_SOURCE_TOTAL_CHARS = 384 * 1024
MAX_BINDINGS = 24
MAX_RUNS = 32
MAX_TOOL_RECEIPTS = 64
MAX_CLAIMS = 32
MAX_VISUALS = 24
MAX_TEXT = 12_000

_CAPABILITY_RE = re.compile(
    r'^(literature|experiment|compute|evaluation|figure|manuscript|publication)'
    r'\.[a-z0-9_.-]+$')
_SOURCE_PATH_RE = re.compile(r'^[A-Za-z0-9_./-]{1,240}$')


def _text(value: Any, maximum: int = MAX_TEXT) -> str:
    return str(value or '').strip()[:maximum]


def _integer(value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        return minimum
    try:
        return max(minimum, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return minimum


def _public_http_url(value: Any) -> str:
    candidate = _text(value, 2_000)
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ''
    if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
        return ''
    if parsed.username or parsed.password:
        return ''
    return candidate


def _records(value: Any, maximum: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value[:maximum] if isinstance(row, Mapping)]


def normalize_source_path(value: Any) -> str:
    """Return one safe relative manuscript path or ``''``.

    Source paths are logical POSIX paths.  They never carry filesystem
    authority, absolute roots, traversal, backslashes, or hidden parent
    aliases into the compiler/export boundary.
    """
    raw = str(value or '').strip().replace('\\', '/')
    if not raw or raw.startswith('/') or not _SOURCE_PATH_RE.fullmatch(raw):
        return ''
    normalized = posixpath.normpath(raw)
    if normalized in {'.', '..'} or normalized.startswith('../'):
        return ''
    return normalized


def source_tree_digest(source_files: Any) -> str:
    """Hash canonical source paths and bytes for compile/publish receipts."""
    rows = []
    for row in _records(source_files, MAX_SOURCE_FILES):
        path = normalize_source_path(row.get('path'))
        if path:
            rows.append((path, str(row.get('content') or '')))
    payload = json.dumps(
        sorted(rows), ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _normalize_protocol(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    seeds = []
    if 'decision_rule' in raw:
        supplied=raw.get('random_seeds',[])
        if (not isinstance(supplied,list) or len(supplied)>32
                or any(type(seed) is not int for seed in supplied)):
            raise ValueError('Scientific random seeds must be an exact integer list of at most 32 entries')
    for seed in list(raw.get('random_seeds') or [])[:32]:
        if isinstance(seed, bool):
            continue
        try:
            seeds.append(int(seed))
        except (TypeError, ValueError, OverflowError):
            continue
    stops = [
        _text(item, 1_000) for item in list(raw.get('stop_conditions') or [])[:16]
        if _text(item, 1_000)
    ]
    return {
        key: _text(raw.get(key))
        for key in (
            'primary_metric', 'baseline', 'dataset', 'falsifier', 'resources',
            'evaluation_protocol', 'environment',
            'matched_budget', 'exploratory_plan', 'validation_plan',
        )
    } | normalize_portfolio(raw) | {'random_seeds': seeds, 'stop_conditions': stops} | (
        {'decision_rule': normalize_decision_rule(raw['decision_rule'])} if 'decision_rule' in raw else {}) | (
        {'measurement_scope':normalize_measurement_scope(raw['measurement_scope'])} if 'measurement_scope' in raw else {}) | (
        {'observation_format':normalize_observation_format(raw['observation_format'])} if 'observation_format' in raw else {})


def _normalize_bindings(value: Any) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for row in _records(value, MAX_BINDINGS):
        capability = _text(row.get('capability'), 120).lower()
        tool = _text(row.get('tool'), 240)
        if not _CAPABILITY_RE.fullmatch(capability) or not tool:
            continue
        key = (capability, tool)
        if key in seen:
            continue
        seen.add(key)
        defaults = row.get('argument_defaults')
        if not isinstance(defaults, Mapping):
            defaults = {}
        result.append({
            'capability': capability,
            'provider': _text(row.get('provider'), 120) or 'mcp',
            'tool': tool,
            'schema_hash': _text(row.get('schema_hash'), 128),
            'enabled': bool(row.get('enabled', True)),
            'argument_defaults': copy.deepcopy(dict(defaults))
            if len(defaults) <= 48 else {},
            'notes': _text(row.get('notes'), 2_000),
        })
    return result


def _normalize_tool_receipts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list) and len(value) > MAX_TOOL_RECEIPTS:
        raise ValueError('research_tool_receipts_overflow: complete evidence requires paged receipt storage')
    receipts = []
    for row in _records(value, MAX_TOOL_RECEIPTS):
        status = _text(row.get('status'), 20)
        if status not in {'done', 'rejected', 'error', 'pending'}:
            status = 'error'
        receipt = {
            'tool': _text(row.get('tool'), 240),
            'call_id': _text(row.get('call_id'), 240),
            'status': status,
            'artifact_ref': _text(row.get('artifact_ref'), 1_000),
            'result_digest': _text(row.get('result_digest'), 128),
            'result_excerpt': _text(row.get('result_excerpt'), 4_000),
            'observed_at': _integer(row.get('observed_at')),
        }
        if 'source_task_id' in row:
            source = row['source_task_id']
            if not isinstance(source, str) or not source or len(source) > 160:
                raise ValueError('Invalid Research receipt source task')
            receipt['source_task_id'] = source
        if 'execution_observation' in row:
            from .execution_observation import normalize_execution_observation
            receipt['execution_observation'] = normalize_execution_observation(row['execution_observation'])
        if 'observation_incomplete' in row:
            if row['observation_incomplete'] is not True:
                raise ValueError('Invalid Research incomplete observation marker')
            receipt['observation_incomplete'] = True
        measurement = row.get('observation_excerpt')
        if isinstance(measurement, str) and len(measurement) <= 4_000:
            receipt['observation_excerpt'] = measurement
        receipts.append(receipt)
    return receipts


def normalize_run_evidence(value: Any) -> list[dict[str, Any]]:
    """Canonical bounded run collection; ambiguity never becomes partial evidence."""
    if value is not None and not isinstance(value, list):
        raise ValueError('Research run evidence must be an array')
    if isinstance(value, list) and len(value) > MAX_RUNS:
        raise ValueError(f'Research run evidence exceeds the {MAX_RUNS}-record workspace boundary; no results were truncated')
    if isinstance(value, list) and any(not isinstance(row, Mapping) for row in value):
        raise ValueError('Research run evidence contains malformed records')
    result, seen = [], set()
    for index, row in enumerate(_records(value, MAX_RUNS)):
        run_id = _text(row.get('id'), 96) or f'run-{index + 1}'
        if run_id in seen:
            raise ValueError('Research run evidence contains duplicate run identities')
        seen.add(run_id)
        status = _text(row.get('status'), 24)
        if status not in {'planned', 'running', 'passed', 'failed', 'inconclusive'}:
            status = 'planned'
        artifact_refs = [
            _text(item, 1_000)
            for item in list(row.get('artifact_refs') or [])[:32]
            if _text(item, 1_000)
        ]
        artifact_ref = _text(row.get('artifact_ref'), 1_000)
        if artifact_ref and artifact_ref not in artifact_refs:
            artifact_refs.insert(0, artifact_ref)
        result.append({
            'id': run_id,
            'label': _text(row.get('label'), 240),
            'status': status,
            'metric': _text(row.get('metric'), 500),
            'baseline': _text(row.get('baseline'), 500),
            'delta': _text(row.get('delta'), 500),
            'artifact_ref': artifact_ref,
            'artifact_refs': artifact_refs,
            'backend': _text(row.get('backend'), 240),
            'remote_job_id': _text(row.get('remote_job_id'), 500),
            'spec_digest': _text(row.get('spec_digest'), 128),
            'protocol_digest': _text(row.get('protocol_digest'), 64),
            'hypothesis_digest': _text(row.get('hypothesis_digest'), 64),
            'execution_status': (row.get('execution_status') if row.get('execution_status')
                in {'planned', 'queued', 'running', 'completed', 'failed'} else
                {'passed': 'completed', 'inconclusive': 'completed', 'failed': 'failed'}.get(status, status)),
            'scientific_outcome': (row.get('scientific_outcome') if row.get('scientific_outcome')
                in {'supported', 'refuted', 'inconclusive'} else 'inconclusive'),
            'task_id': _text(row.get('task_id'), 160),
            'notes': _text(row.get('notes')),
            'tool_receipts': _normalize_tool_receipts(row.get('tool_receipts')),
            'started_at': _integer(row.get('started_at')),
            'finished_at': _integer(row.get('finished_at')),
            'updated_at': _integer(row.get('updated_at')),
        })
        if 'receipt_progress' in row:
            pointer = row['receipt_progress']
            if not isinstance(pointer, str) or not re.fullmatch('[0-9a-f]{64}', pointer):
                raise ValueError('Invalid Research receipt progress pointer')
            result[-1]['receipt_progress'] = pointer
        if 'receipt_evidence' in row:
            from .science_reduction import normalize_receipt_evidence
            result[-1]['receipt_evidence'] = normalize_receipt_evidence(row['receipt_evidence'])
        if 'receipt_history' in row:
            from .receipt_pages import normalize_reference
            result[-1]['receipt_history'] = normalize_reference(row['receipt_history'])
    return result



def run_result_identity(run: Mapping[str, Any]) -> str:
    """Canonical execution/evidence identity, excluding replay-time timestamps."""
    row = normalize_run_evidence([run])[0]
    evidence = {key: row[key] for key in (
        'task_id', 'protocol_digest', 'hypothesis_digest',
        'execution_status', 'scientific_outcome', 'artifact_refs', 'artifact_ref',
        'remote_job_id', 'backend')}
    evidence['tool_receipts'] = [{key: value for key, value in receipt.items()
                                 if key != 'observed_at'}
                                for receipt in row['tool_receipts']]
    return _digest(evidence)


def _normalize_claims(value: Any) -> list[dict[str, Any]]:
    result = []
    for index, row in enumerate(_records(value, MAX_CLAIMS)):
        status = _text(row.get('status'), 24)
        if status not in {'draft', 'supported', 'contested', 'rejected'}:
            status = 'draft'
        refs = [
            _text(item, 1_000)
            for item in list(row.get('evidence_refs') or [])[:24]
            if _text(item, 1_000)
        ]
        # A supported claim without evidence is structurally impossible.
        if status == 'supported' and not refs:
            status = 'draft'
        result.append({
            'id': _text(row.get('id'), 96) or f'claim-{index + 1}',
            'text': _text(row.get('text')),

            **({'outcome_scope': row['outcome_scope']} if row.get('outcome_scope')
               in {'supported', 'refuted', 'inconclusive'} else {}),
            'status': status,
            'evidence_refs': refs,
            'protocol_digest': _text(row.get('protocol_digest'), 64),
            'hypothesis_digest': _text(row.get('hypothesis_digest'), 64),
            'evidence_digest': _text(row.get('evidence_digest'), 64),
        })
    return result


def _normalize_manuscript(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, Mapping) else {}
    return {
        key: _text(raw.get(key))
        for key in (
            'title', 'venue', 'abstract', 'keywords', 'method', 'results',
            'limitations', 'introduction', 'related_work', 'experiments',
            'conclusion', 'ethics',
        )
    } | {'scientific_dependency_digest': _text(raw.get('scientific_dependency_digest'), 64)}


def _normalize_source_files(value: Any) -> list[dict[str, Any]]:
    result = []
    seen = set()
    remaining = MAX_SOURCE_TOTAL_CHARS
    for row in _records(value, MAX_SOURCE_FILES):
        path = normalize_source_path(row.get('path'))
        if not path or path in seen or remaining <= 0:
            continue
        seen.add(path)
        content = str(row.get('content') or '')[:min(MAX_SOURCE_FILE_CHARS, remaining)]
        remaining -= len(content)
        result.append({
            'path': path,
            'content': content,
            'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
            'updated_at': _integer(row.get('updated_at')),
        })
    return result


def _normalize_visuals(value: Any) -> list[dict[str, Any]]:
    result = []
    for index, row in enumerate(_records(value, MAX_VISUALS)):
        status = _text(row.get('status'), 24)
        if status not in {'planned', 'generated', 'verified', 'rejected'}:
            status = 'planned'
        result.append({
            'id': _text(row.get('id'), 96) or f'visual-{index + 1}',
            'title': _text(row.get('title'), 500),
            'caption': _text(row.get('caption'), 4_000),
            'data_ref': _text(row.get('data_ref'), 1_000),
            'script_ref': _text(row.get('script_ref'), 1_000),
            'output_ref': _text(row.get('output_ref'), 1_000),
            'status': status,
        })
    return result


def default_program_fields() -> dict[str, Any]:
    """Return detached defaults for every research-program field."""
    return {
        'protocol': _normalize_protocol({}),
        'capability_bindings': [],
        'runs': [],
        'claims': [],

        'decision': research_decision({}, []),
        'manuscript_audit': manuscript_audit([], []),
        'manuscript': _normalize_manuscript({}),
        'source_files': [],
        'figures': [],
        'tables': [],
        'compilation': {
            'mode': 'unconfigured', 'status': 'not_run', 'detail': '',
            'source_digest': '', 'engine': '', 'compiled_at': 0, 'build': None,
        },
        'publication': {
            'provider': '', 'status': 'not_started', 'project_ref': '',
            'project_url': '', 'source_digest': '', 'published_at': 0,
            'detail': '',
        },
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def hypothesis_identity_payload(program: Mapping[str, Any]):
    """Preserve legacy text identity; bound selections include original source."""
    hypothesis = _text(program.get('hypothesis'))
    if program.get('selected_idea_source_task_id'):
        return {'hypothesis':hypothesis, 'idea_id':program.get('selected_idea_id', ''),
                'source_task_id':program['selected_idea_source_task_id']}
    return hypothesis


def dependency_digests(program: Mapping[str, Any]) -> dict[str, str]:
    """Canonical dependency identities; missing old snapshots stay unverified."""
    return {'protocol_digest': _digest(_normalize_protocol(program.get('protocol'))),
            'hypothesis_digest': _digest(hypothesis_identity_payload(program))}


def claim_evidence_digest(claim: Mapping[str, Any], runs: list[dict]) -> str:
    refs = set(claim.get('evidence_refs') or [])
    linked = [row for row in runs if refs.intersection(
        [row.get('id'), row.get('task_id'), row.get('artifact_ref')]
        + list(row.get('artifact_refs') or []))]
    return _digest({'text': claim.get('text'), 'refs': sorted(refs), 'runs': linked})


def _validated_evidence(raw: Mapping[str, Any]) -> tuple[list[dict], list[dict]]:
    dependencies = dependency_digests(raw)
    runs = normalize_run_evidence(raw.get('runs'))
    for run in runs:
        run['validity'] = ('unverified' if not all(run.get(k) for k in dependencies)
                           else 'current' if all(run[k] == v for k, v in dependencies.items())
                           else 'stale')
    unresolved = has_unresolved_evidence(runs, summary_for_program(raw), raw.get('protocol'))
    claims = _normalize_claims(raw.get('claims'))
    for claim in claims:
        refs = set(claim['evidence_refs'])
        supported_refs = set()
        for run in runs:
            if (authoritative(run) and receipt_outcome_consistent(run, raw.get('protocol'))
                    and run['scientific_outcome'] == claim.get('outcome_scope', 'supported')):
                supported_refs.update([run['id'], run['task_id'], run['artifact_ref']])
                supported_refs.update(run['artifact_refs'])
        missing = not all(claim.get(k) for k in (*dependencies, 'evidence_digest'))
        current = (not missing and not unresolved
                   and all(claim[k] == v for k, v in dependencies.items())
                   and claim['evidence_digest'] == claim_evidence_digest(claim, runs)
                   and bool(refs) and refs.issubset(supported_refs))
        claim['validity'] = 'current' if current else 'unverified' if missing else 'stale'
        claim['validity_reason'] = '' if current else (
            'Current protocol has conflicting or inconclusive evidence; resolve the tested scope'
            if unresolved else 'Missing dependency snapshot or verified support' if missing else
            'Protocol, hypothesis, or supporting evidence changed or is not supportive')
        if claim['status'] == 'supported' and not current:
            claim['status'] = 'contested'
    return runs, claims


def scientific_dependency_digest(program: Mapping[str, Any]) -> str:
    """Identify normalized science reviewed when a manuscript was generated.

    Never infer this snapshot from source bytes or backfill legacy manuscripts.
    Derived validity is recomputed so normalization does not change the digest.
    """
    runs, claims = _validated_evidence(program)
    reviewed = {
        'observation_audit_version': OBSERVATION_AUDIT_VERSION,
        **dependency_digests(program),
        'claims': [dict(claim, current_evidence_digest=claim_evidence_digest(claim, runs))
                   for claim in claims],
    }
    history = summary_for_program(program)
    if has_unresolved_evidence(runs, history, program.get('protocol')):
        # A multiset commitment remains stable when a completed observation
        # moves from the working document to an immutable historical page.
        eligible = [run for run in runs if authoritative(run)]
        reviewed['unresolved_evidence'] = {
            'count': len(eligible) + sum(history['outcomes'].values()),
            'sum': f"{(sum(observation_commitment(run) for run in eligible) + int(history['evidence_sum'], 16)) % MODULUS:064x}",
        }
    return _digest(reviewed)


def normalized_compiled_build(value: Any) -> dict | None:
    """Normalize a server-written build reference, never a compiler assertion.

    Workspace saves preserve this field from storage or accept a verified
    application compilation result; browser/model payloads cannot write it.
    """
    if not isinstance(value, Mapping):
        return None
    widths = {'document_id': (32, 64), 'build_id': (32, 32),
              'source_digest': (64, 64), 'manifest_digest': (64, 64), 'pdf_sha256': (64, 64)}
    result = {}
    for key, (minimum, maximum) in widths.items():
        text = value.get(key)
        if not isinstance(text, str) or not re.fullmatch(f'[a-f0-9]{{{minimum},{maximum}}}', text):
            return None
        result[key] = text
    return result


def compilation_current(compilation: Mapping, source_digest: str) -> bool:
    build = normalized_compiled_build(compilation.get('build'))
    return bool(compilation.get('mode') == 'paper_build'
                and compilation.get('status') == 'passing'
                and compilation.get('source_digest') == source_digest
                and build and build['source_digest'] == source_digest)


def normalize_program_fields(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize the program-owned portion of a persisted workspace."""
    raw = raw if isinstance(raw, Mapping) else {}
    compilation = raw.get('compilation') if isinstance(raw.get('compilation'), Mapping) else {}
    compile_mode = _text(compilation.get('mode'), 40)
    compile_status = _text(compilation.get('status'), 40)
    publication = raw.get('publication') if isinstance(raw.get('publication'), Mapping) else {}
    publish_status = _text(publication.get('status'), 40)
    source_files = _normalize_source_files(raw.get('source_files'))
    runs, claims = _validated_evidence(raw)
    compiled_digest = _text(compilation.get('source_digest'), 128)
    current_digest = source_tree_digest(source_files)
    if compile_status == 'passing' and not compilation_current(compilation, current_digest):
        compile_status = 'not_run'
    return {
        'protocol': _normalize_protocol(raw.get('protocol')),
        'capability_bindings': _normalize_bindings(raw.get('capability_bindings')),
        'runs': runs,
        'claims': claims,

        **({'history': normalize_history(raw['history'])} if raw.get('history') is not None else {}),
        'decision': research_decision(_normalize_protocol(raw.get('protocol')), runs, summary_for_program(raw)),
        'manuscript_audit': manuscript_audit(runs, claims, summary_for_program(raw), raw.get('protocol')),
        'manuscript': _normalize_manuscript(raw.get('manuscript')),
        'source_files': source_files,
        'figures': _normalize_visuals(raw.get('figures')),
        'tables': _normalize_visuals(raw.get('tables')),
        'compilation': {
            'mode': compile_mode if compile_mode == 'paper_build' else 'unconfigured',
            'status': compile_status if compile_status in {
                'not_run', 'passing', 'failing'} else 'not_run',
            'detail': _text(compilation.get('detail'), 4_000),
            'source_digest': compiled_digest,
            'engine': _text(compilation.get('engine'), 240),
            'compiled_at': _integer(compilation.get('compiled_at')),
            'build': normalized_compiled_build(compilation.get('build')),
        },
        'publication': {
            'provider': _text(publication.get('provider'), 120),
            'status': publish_status if publish_status in {
                'not_started', 'syncing', 'published', 'conflict', 'failed'
            } else 'not_started',
            'project_ref': _text(publication.get('project_ref'), 1_000),
            'project_url': _public_http_url(publication.get('project_url')),
            'source_digest': _text(publication.get('source_digest'), 128),
            'published_at': _integer(publication.get('published_at')),
            'detail': _text(publication.get('detail'), 4_000),
        },
    }


def readiness(program: Mapping[str, Any]) -> dict[str, Any]:
    """Derive submission gates from evidence; never persist a percentage."""
    protocol = program.get('protocol') if isinstance(program.get('protocol'), Mapping) else {}
    manuscript = program.get('manuscript') if isinstance(program.get('manuscript'), Mapping) else {}
    runs, claims = _validated_evidence(program)
    source_files = list(program.get('source_files') or [])
    digest = source_tree_digest(source_files)

    manuscript_current = (manuscript.get('scientific_dependency_digest')
                          == scientific_dependency_digest(program))
    compilation = program.get('compilation') if isinstance(program.get('compilation'), Mapping) else {}
    publication = program.get('publication') if isinstance(program.get('publication'), Mapping) else {}
    gates = [
        {
            'id': 'protocol',
            'ok': all(_text(protocol.get(key)) for key in (
                'primary_metric', 'baseline', 'dataset', 'falsifier')),
        },
        {
            'id': 'run_evidence',
            'ok': bool(summary_for_program(program)['artifact_count']) or any(
                row.get('execution_status') == 'completed'
                and row.get('scientific_outcome') in {'supported', 'refuted', 'inconclusive'}
                and row.get('validity') == 'current'
                and any(r['status'] == 'done' and r['result_digest']
                        for r in row['tool_receipts'])
                and (row.get('artifact_ref') or row.get('artifact_refs'))
                for row in runs),
        },
        {
            'id': 'claims',
            'ok': bool(claims) and all(
                isinstance(row, Mapping) and row.get('status') == 'supported'
                and row.get('evidence_refs') for row in claims),
        },
        {
            'id': 'manuscript',
            'ok': all(_text(manuscript.get(key)) for key in (
                'title', 'abstract', 'method', 'results', 'limitations'))
                and bool(source_files) and manuscript_current,
        },
        {
            'id': 'compile',
            'ok': bool(source_files) and compilation_current(compilation, digest),
        },
    ]
    audit = manuscript_audit(runs, claims, summary_for_program(program), program.get('protocol'))
    gates.extend(audit['checks'])
    return {
        'ready': all(row['ok'] for row in gates),

        'decision': research_decision(protocol, runs, summary_for_program(program)),
        'manuscript_audit': audit,
        'gates': gates,
        'source_digest': digest,
        'published_current': bool(source_files) and manuscript_current
            and publication.get('status') == 'published'
            and bool(digest)
            and publication.get('source_digest') == digest,
    }


def encoded_program_size(program: Mapping[str, Any]) -> int:
    return len(json.dumps(
        program, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


__all__ = [
    'MAX_BINDINGS', 'MAX_CLAIMS', 'MAX_PROGRAM_BYTES', 'MAX_RUNS',
    'MAX_SOURCE_FILES', 'MAX_VISUALS', 'PROGRAM_CONTRACT_VERSION',
    'default_program_fields', 'encoded_program_size', 'normalize_program_fields',
    'normalize_source_path', 'readiness', 'source_tree_digest',
    'scientific_dependency_digest',
]
