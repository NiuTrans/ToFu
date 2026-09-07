"""Whole-definition orchestration validator.

This composition root owns schema/collection checks and delegates node, edge,
field, I/O, subflow and topology rules to their focused modules.
"""

from __future__ import annotations

from typing import Any

from lib.orchestration._definition_contract import (
    MAX_NAME_LEN,
    MAX_NODES,
    SCHEMA_ID,
)
from lib.orchestration._edge_validation import validate_edges
from lib.orchestration._node_validation import validate_nodes
from lib.orchestration._subflow_contract import validate_subflow_node
from lib.orchestration._topology_diagnostics import (
    parallel_verdict_channel_issues,
)
from lib.orchestration.validation_issues import (
    ValidationIssueList,
    report_validation_issue,
    validation_diagnostics,
)


def _validate_subflow_node(node: dict, where: str, params: dict,
                           errors: list, warnings: list,
                           depth: int, seen_refs: frozenset[str],
                           path: str = '') -> None:
    """Compatibility wrapper around the focused subflow contract."""
    validate_subflow_node(
        node, where, params, errors, warnings, depth, seen_refs,
        lambda child, child_depth, child_seen_refs: validate_definition(
            child, _depth=child_depth, _seen_refs=child_seen_refs),
        path=path,
    )


def _verdict(errors: list, warnings: list) -> dict[str, Any]:
    return {
        'ok': not errors,
        'errors': errors,
        'warnings': warnings,
        'diagnostics': validation_diagnostics(errors, warnings),
    }


def validate_definition(defn: Any, *, _depth: int = 0,
                        _seen_refs: frozenset[str] = frozenset()) \
        -> dict[str, Any]:
    """Validate a definition without I/O or input mutation.

    Rolling clients receive the same string ``errors``/``warnings`` lists;
    inspection-aware clients additionally receive structured diagnostics with
    stable codes and RFC 6901 JSON Pointer paths.
    """
    errors = ValidationIssueList('error')
    warnings = ValidationIssueList('warning')
    issue = report_validation_issue

    if not isinstance(defn, dict):
        issue(errors, 'definition must be a JSON object',
              code='definition.type.object', path='')
        return _verdict(errors, warnings)

    schema = defn.get('schema')
    if schema != SCHEMA_ID:
        issue(warnings,
              f'unexpected schema {schema!r} (expected {SCHEMA_ID!r})',
              code='definition.schema.unexpected', path='/schema')

    name = defn.get('name', '')
    if not isinstance(name, str) or not name.strip():
        issue(errors, 'name is required and must be a non-empty string',
              code='definition.name.required', path='/name')
    elif len(name) > MAX_NAME_LEN:
        issue(errors, f'name exceeds {MAX_NAME_LEN} chars',
              code='definition.name.max_length', path='/name')

    nodes = defn.get('nodes')
    edges = defn.get('edges')
    if not isinstance(nodes, list):
        issue(errors, 'nodes must be an array',
              code='definition.nodes.type.array', path='/nodes')
        nodes = []
    if not isinstance(edges, list):
        issue(errors, 'edges must be an array',
              code='definition.edges.type.array', path='/edges')
        edges = []
    if len(nodes) > MAX_NODES:
        issue(errors, f'too many nodes ({len(nodes)} > {MAX_NODES})',
              code='definition.nodes.max_items', path='/nodes')

    ids, kind_counts, role_count, id_to_node = validate_nodes(
        nodes, errors, warnings,
        depth=_depth,
        seen_refs=_seen_refs,
        validate_subflow=_validate_subflow_node,
    )
    validate_edges(edges, ids, id_to_node, errors, warnings)

    if nodes:
        if kind_counts.get('start', 0) == 0:
            issue(warnings,
                  'no start node — the engine will not know where to begin',
                  code='topology.start.missing', path='/nodes')
        if kind_counts.get('stop', 0) == 0:
            issue(warnings, 'no stop node — the flow has no defined terminal',
                  code='topology.stop.missing', path='/nodes')
        if role_count == 0:
            issue(warnings, 'no agent nodes — the flow does no work',
                  code='topology.agent.missing', path='/nodes')
        for diagnostic in parallel_verdict_channel_issues(
                nodes, edges, id_to_node):
            issue(warnings, diagnostic['message'],
                  code=diagnostic['code'], path=diagnostic['path'])

    return _verdict(errors, warnings)


__all__ = ['_validate_subflow_node', 'validate_definition']
