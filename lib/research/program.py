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

PROGRAM_CONTRACT_VERSION = 'tofu.research-program/v1'
MAX_PROGRAM_BYTES = 640 * 1024
MAX_SOURCE_FILES = 24
MAX_SOURCE_FILE_CHARS = 65_536
MAX_SOURCE_TOTAL_CHARS = 384 * 1024
MAX_BINDINGS = 24
MAX_RUNS = 32
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
        )
    } | {'random_seeds': seeds, 'stop_conditions': stops}


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
    receipts = []
    for row in _records(value, 64):
        status = _text(row.get('status'), 20)
        if status not in {'done', 'rejected', 'error'}:
            status = 'error'
        receipts.append({
            'tool': _text(row.get('tool'), 240),
            'call_id': _text(row.get('call_id'), 240),
            'status': status,
            'artifact_ref': _text(row.get('artifact_ref'), 1_000),
            'result_digest': _text(row.get('result_digest'), 128),
            'result_excerpt': _text(row.get('result_excerpt'), 4_000),
            'observed_at': _integer(row.get('observed_at')),
        })
    return receipts


def _normalize_runs(value: Any) -> list[dict[str, Any]]:
    result = []
    for index, row in enumerate(_records(value, MAX_RUNS)):
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
            'id': _text(row.get('id'), 96) or f'run-{index + 1}',
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
            'task_id': _text(row.get('task_id'), 160),
            'notes': _text(row.get('notes')),
            'tool_receipts': _normalize_tool_receipts(row.get('tool_receipts')),
            'started_at': _integer(row.get('started_at')),
            'finished_at': _integer(row.get('finished_at')),
            'updated_at': _integer(row.get('updated_at')),
        })
    return result


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
            'status': status,
            'evidence_refs': refs,
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
    }


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
        'manuscript': _normalize_manuscript({}),
        'source_files': [],
        'figures': [],
        'tables': [],
        'compilation': {
            'mode': 'unconfigured', 'status': 'not_run', 'detail': '',
            'source_digest': '', 'engine': '', 'compiled_at': 0,
        },
        'publication': {
            'provider': '', 'status': 'not_started', 'project_ref': '',
            'project_url': '', 'source_digest': '', 'published_at': 0,
            'detail': '',
        },
    }


def normalize_program_fields(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize the program-owned portion of a persisted workspace."""
    raw = raw if isinstance(raw, Mapping) else {}
    compilation = raw.get('compilation') if isinstance(raw.get('compilation'), Mapping) else {}
    compile_mode = _text(compilation.get('mode'), 40)
    compile_status = _text(compilation.get('status'), 40)
    publication = raw.get('publication') if isinstance(raw.get('publication'), Mapping) else {}
    publish_status = _text(publication.get('status'), 40)
    source_files = _normalize_source_files(raw.get('source_files'))
    compiled_digest = _text(compilation.get('source_digest'), 128)
    current_digest = source_tree_digest(source_files)
    if compile_status == 'passing' and compiled_digest != current_digest:
        compile_status = 'not_run'
    return {
        'protocol': _normalize_protocol(raw.get('protocol')),
        'capability_bindings': _normalize_bindings(raw.get('capability_bindings')),
        'runs': _normalize_runs(raw.get('runs')),
        'claims': _normalize_claims(raw.get('claims')),
        'manuscript': _normalize_manuscript(raw.get('manuscript')),
        'source_files': source_files,
        'figures': _normalize_visuals(raw.get('figures')),
        'tables': _normalize_visuals(raw.get('tables')),
        'compilation': {
            'mode': compile_mode if compile_mode in {
                'unconfigured', 'local_tectonic', 'bound_tool',
                'overleaf_mcp'} else 'unconfigured',
            'status': compile_status if compile_status in {
                'not_run', 'passing', 'failing'} else 'not_run',
            'detail': _text(compilation.get('detail'), 4_000),
            'source_digest': compiled_digest,
            'engine': _text(compilation.get('engine'), 240),
            'compiled_at': _integer(compilation.get('compiled_at')),
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
    runs = list(program.get('runs') or [])
    claims = list(program.get('claims') or [])
    source_files = list(program.get('source_files') or [])
    digest = source_tree_digest(source_files)
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
            'ok': any(
                isinstance(row, Mapping) and row.get('status') == 'passed'
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
                and bool(source_files),
        },
        {
            'id': 'compile',
            'ok': compilation.get('status') == 'passing'
                and bool(digest)
                and compilation.get('source_digest') == digest,
        },
    ]
    return {
        'ready': all(row['ok'] for row in gates),
        'gates': gates,
        'source_digest': digest,
        'published_current': bool(source_files)
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
]
