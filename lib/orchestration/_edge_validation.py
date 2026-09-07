"""Edge-level structural validation for orchestration definitions."""

from __future__ import annotations

from lib.orchestration.validation_issues import (
    json_pointer_path,
    report_validation_issue,
)


def validate_edges(
    edges: list,
    ids: set[str],
    id_to_node: dict,
    errors: list,
    warnings: list,
) -> None:
    """Validate graph edges against the node indexes from the first pass."""
    seen: set[tuple[str, str]] = set()
    issue = report_validation_issue
    for index, edge in enumerate(edges):
        edge_path = f'/edges/{index}'
        if not isinstance(edge, dict):
            issue(errors, f'edge[{index}] must be an object',
                  code='edge.type.object', path=edge_path)
            continue
        source, target = edge.get('from'), edge.get('to')
        if source not in ids:
            issue(errors,
                  f'edge[{index}] from {source!r} references unknown node',
                  code='edge.from.unknown_node',
                  path=json_pointer_path(edge_path, 'from'))
        if target not in ids:
            issue(errors,
                  f'edge[{index}] to {target!r} references unknown node',
                  code='edge.to.unknown_node',
                  path=json_pointer_path(edge_path, 'to'))
        if source == target:
            issue(errors, f'edge[{index}] self-loop on {source!r}',
                  code='edge.self_loop', path=edge_path)
        if (source, target) in seen:
            issue(warnings, f'duplicate edge {source!r}→{target!r}',
                  code='edge.duplicate', path=edge_path)
        seen.add((source, target))
        source_node = id_to_node.get(source)
        target_node = id_to_node.get(target)
        if target_node and target_node.get('kind') == 'start':
            issue(errors,
                  f'edge[{index}] targets a start node (start has no input)',
                  code='edge.target.start',
                  path=json_pointer_path(edge_path, 'to'))
        if source_node and source_node.get('kind') == 'stop':
            issue(errors,
                  f'edge[{index}] leaves a stop node (stop has no output)',
                  code='edge.source.stop',
                  path=json_pointer_path(edge_path, 'from'))


__all__ = ['validate_edges']
