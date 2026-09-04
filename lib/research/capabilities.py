"""Provider-neutral research capability discovery and binding validation.

Research programs persist *capabilities* (what a workflow needs) separately
from namespaced MCP tools (how this installation performs it).  The catalog is
derived from the live MCP contract, so a private LLM/HOPE/Overleaf server and a
third-party server follow the same path.  Suggestions are advisory; execution
authority always comes from an exact enabled binding in the workspace.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from lib.mcp.types import parse_namespaced_name


CAPABILITIES: tuple[dict[str, Any], ...] = (
    {'id': 'literature.search', 'write': False,
     'terms': ('search', 'query', 'paper', 'literature', 'scholar', 'arxiv')},
    {'id': 'literature.read', 'write': False,
     'terms': ('read', 'fetch', 'download', 'paper', 'pdf', 'full text')},
    {'id': 'literature.citations', 'write': False,
     'terms': ('citation', 'reference', 'bibliography', 'bibtex', 'cited by')},
    {'id': 'experiment.execute', 'write': True,
     'terms': ('execute', 'run', 'experiment', 'train', 'launch', 'start job')},
    {'id': 'experiment.status', 'write': False,
     'terms': ('status', 'result', 'metrics', 'experiment', 'job')},
    {'id': 'experiment.artifacts', 'write': False,
     'terms': ('artifact', 'output', 'result', 'file', 'download')},
    {'id': 'compute.submit', 'write': True,
     'terms': ('submit', 'launch', 'compute', 'cluster', 'gpu', 'job')},
    {'id': 'compute.status', 'write': False,
     'terms': ('status', 'job', 'queue', 'compute', 'cluster')},
    {'id': 'evaluation.run', 'write': True,
     'terms': ('evaluate', 'evaluation', 'benchmark', 'test', 'score')},
    {'id': 'figure.render', 'write': True,
     'terms': ('figure', 'plot', 'chart', 'visualize', 'render')},
    {'id': 'manuscript.compile', 'write': True,
     'terms': ('compile', 'latex', 'tex', 'pdf', 'build document')},
    {'id': 'publication.push', 'write': True,
     'terms': ('publish', 'upload', 'sync', 'project', 'manuscript', 'latex')},
)

_CAPABILITY_BY_ID = {row['id']: row for row in CAPABILITIES}
_TOKEN_RE = re.compile(r'[a-z0-9_.-]+')


def _tool_text(row: Mapping[str, Any]) -> str:
    function = row.get('openai_def')
    function = function.get('function') if isinstance(function, Mapping) else {}
    meta = row.get('meta') if isinstance(row.get('meta'), Mapping) else {}
    workflow = meta.get('workflow') if isinstance(meta.get('workflow'), Mapping) else {}
    return ' '.join((
        str(row.get('tool_name') or ''),
        str(row.get('namespaced_name') or ''),
        str(row.get('description') or function.get('description') or ''),
        str(meta.get('capability') or ''),
        str(meta.get('capabilities') or ''),
        str(workflow.get('stage') or ''),
        str(workflow.get('tags') or ''),
    )).lower()


def suggest_capabilities(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Rank capability hints using only provider-declared tool metadata."""
    text = _tool_text(row)
    tokens = set(_TOKEN_RE.findall(text))
    suggested = []
    for capability in CAPABILITIES:
        matched = []
        score = 0
        for term in capability['terms']:
            if term in tokens:
                score += 3
                matched.append(term)
            elif ' ' in term and term in text:
                score += 4
                matched.append(term)
            elif len(term) >= 5 and term in text:
                score += 1
                matched.append(term)
        if score:
            suggested.append({
                'id': capability['id'],
                'score': score,
                'matched_terms': matched[:5],
            })
    suggested.sort(key=lambda item: (-item['score'], item['id']))
    return suggested[:5]


def build_capability_catalog(bridge=None, *, user_id: int | None = None) -> dict[str, Any]:
    """Project the live MCP catalog into a bounded, provider-neutral view."""
    if user_id is not None:
        from lib.identity import require_user_id
        require_user_id(user_id, context='research capability catalog owner')
    if bridge is None:
        from lib.mcp import get_bridge
        bridge = get_bridge()
    try:
        rows = bridge.get_tool_catalog_snapshot()
    except Exception:
        rows = []
    tools = []
    for row in rows[:500]:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get('namespaced_name') or '')
        parsed = parse_namespaced_name(name)
        if parsed is None:
            continue
        function = row.get('openai_def')
        function = function.get('function') if isinstance(function, Mapping) else {}
        tools.append({
            'name': name,
            'server': str(row.get('server_name') or parsed[0]),
            'description': str(
                row.get('description') or function.get('description') or '')[:2_000],
            'input_schema': function.get('parameters')
                if isinstance(function.get('parameters'), Mapping)
                else {'type': 'object', 'properties': {}},
            'read_only': bool(row.get('read_only_hint')),
            'schema_hash': str(row.get('schema_hash') or ''),
            'suggested_capabilities': suggest_capabilities(row),
        })
    tools.sort(key=lambda item: (item['server'], item['name']))
    fingerprint = hashlib.sha256(json.dumps(
        [(row['name'], row['schema_hash'], row['read_only']) for row in tools],
        ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8')).hexdigest()
    return {
        'contract_version': 'tofu.research-capabilities/v1',
        'capabilities': [
            {'id': row['id'], 'write': row['write']} for row in CAPABILITIES
        ],
        'tools': tools,
        'fingerprint': fingerprint,
    }


def validate_bindings(
    bindings: Iterable[Mapping[str, Any]],
    *,
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve enabled bindings against the current exact tool contracts."""
    live = {
        str(row.get('name')): row
        for row in list(catalog.get('tools') or [])
        if isinstance(row, Mapping) and row.get('name')
    }
    resolved = []
    problems = []
    for binding in bindings:
        if not isinstance(binding, Mapping) or not binding.get('enabled', True):
            continue
        capability = str(binding.get('capability') or '')
        tool_name = str(binding.get('tool') or '')
        spec = _CAPABILITY_BY_ID.get(capability)
        tool = live.get(tool_name)
        if spec is None:
            problems.append({'capability': capability, 'tool': tool_name,
                             'code': 'unknown_capability'})
            continue
        if tool is None:
            problems.append({'capability': capability, 'tool': tool_name,
                             'code': 'tool_unavailable'})
            continue
        saved_schema_hash = str(binding.get('schema_hash') or '')
        live_schema_hash = str(tool.get('schema_hash') or '')
        if live_schema_hash and not saved_schema_hash:
            problems.append({'capability': capability, 'tool': tool_name,
                             'code': 'binding_schema_hash_missing'})
            continue
        if saved_schema_hash != live_schema_hash:
            problems.append({'capability': capability, 'tool': tool_name,
                             'code': 'tool_schema_changed'})
            continue
        if not spec['write'] and not bool(tool.get('read_only')):
            problems.append({'capability': capability, 'tool': tool_name,
                             'code': 'read_capability_requires_read_only_tool'})
            continue
        resolved.append({
            'capability': capability,
            'tool': tool_name,
            'write': bool(spec['write']),
            'read_only': bool(tool.get('read_only')),
            'schema_hash': live_schema_hash,
            'argument_defaults': dict(binding.get('argument_defaults') or {}),
        })
    return {'resolved': resolved, 'problems': problems}


__all__ = [
    'CAPABILITIES', 'build_capability_catalog', 'suggest_capabilities',
    'validate_bindings',
]
