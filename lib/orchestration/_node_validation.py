"""Node-level structural validation for orchestration definitions."""

from __future__ import annotations

from collections.abc import Callable

from lib.orchestration._control_specs import (
    CONTROL_KINDS,
    _validate_control_params,
)
from lib.orchestration.io_validation import _validate_node_io
from lib.orchestration._role_axes import (
    KNOWN_ROLES,
    VALID_EMITS,
    VALID_ISOLATION,
    VALID_TIERS,
)
from lib.orchestration._role_specs import (
    MAX_OBJECTIVE_LEN,
    _validate_role_params,
)
from lib.orchestration.validation_issues import (
    json_pointer_path,
    report_validation_issue,
)


SubflowValidator = Callable[..., None]


def validate_nodes(
    nodes: list,
    errors: list,
    warnings: list,
    *,
    depth: int,
    seen_refs: frozenset[str],
    validate_subflow: SubflowValidator,
) -> tuple[set[str], dict[str, int], int, dict]:
    """Validate all nodes and return graph indexes for later passes."""
    ids: set[str] = set()
    kind_counts: dict[str, int] = {}
    role_count = 0
    issue = report_validation_issue

    for index, node in enumerate(nodes):
        where = f'node[{index}]'
        node_path = f'/nodes/{index}'
        if not isinstance(node, dict):
            issue(errors, f'{where} must be an object',
                  code='node.type.object', path=node_path)
            continue
        node_id = node.get('id')
        if not isinstance(node_id, str) or not node_id:
            issue(errors, f'{where} missing string id',
                  code='node.id.required',
                  path=json_pointer_path(node_path, 'id'))
            continue
        where = f'node {node_id!r}'
        if node_id in ids:
            issue(errors, f'duplicate node id {node_id!r}',
                  code='node.id.duplicate',
                  path=json_pointer_path(node_path, 'id'))
        ids.add(node_id)

        node_type = node.get('type')
        params = node.get('params') or {}
        if not isinstance(params, dict):
            issue(errors, f'{where} params must be an object',
                  code='node.params.type.object',
                  path=json_pointer_path(node_path, 'params'))
            params = {}

        if node_type == 'role':
            role_count += 1
            role = node.get('role')
            if not isinstance(role, str) or not role:
                issue(errors, f'{where} role node missing role',
                      code='role.required',
                      path=json_pointer_path(node_path, 'role'))
            elif role not in KNOWN_ROLES:
                issue(warnings,
                      f'{where} unknown role {role!r} (engine may '
                      'not map it until registered)',
                      code='role.unknown',
                      path=json_pointer_path(node_path, 'role'))
            tier = params.get('tier')
            if tier is not None and tier not in VALID_TIERS:
                issue(errors, f'{where} invalid tier {tier!r}',
                      code='role.tier.invalid',
                      path=json_pointer_path(node_path, 'params', 'tier'))
            isolation = params.get('isolation')
            if isolation is not None and isolation not in VALID_ISOLATION:
                issue(errors, f'{where} invalid isolation {isolation!r}',
                      code='role.isolation.invalid',
                      path=json_pointer_path(node_path, 'params', 'isolation'))
            objective = params.get('objective')
            if (isinstance(objective, str)
                    and len(objective) > MAX_OBJECTIVE_LEN):
                issue(errors,
                      f'{where} objective exceeds {MAX_OBJECTIVE_LEN} chars',
                      code='role.objective.max_length',
                      path=json_pointer_path(
                          node_path, 'params', 'objective'))
            emits = params.get('emits')
            if emits is not None and emits not in VALID_EMITS:
                issue(errors,
                      f'{where} invalid emits {emits!r} '
                      f'(expected one of {sorted(VALID_EMITS)})',
                      code='role.emits.invalid',
                      path=json_pointer_path(node_path, 'params', 'emits'))
            _validate_role_params(
                role if isinstance(role, str) else '', where, params,
                errors, warnings, path=node_path)
        elif node_type == 'subflow':
            role_count += 1
            validate_subflow(
                node, where, params, errors, warnings, depth, seen_refs,
                path=node_path)
        elif node_type == 'control':
            kind = node.get('kind')
            if kind not in CONTROL_KINDS:
                issue(errors, f'{where} invalid control kind {kind!r}',
                      code='control.kind.invalid',
                      path=json_pointer_path(node_path, 'kind'))
            else:
                kind_counts[kind] = kind_counts.get(kind, 0) + 1
                _validate_control_params(
                    kind, where, params, errors, warnings, path=node_path)
                artifact_path = params.get('path')
                if (kind == 'artifact'
                        and not (isinstance(artifact_path, str)
                                 and artifact_path.strip())):
                    issue(warnings,
                          f'{where} artifact has no path — it will be '
                          'recorded but unnamed',
                          code='control.artifact.path.missing',
                          path=json_pointer_path(
                              node_path, 'params', 'path'))
        else:
            issue(errors,
                  f'{where} invalid type {node_type!r} (expected '
                  "'role', 'subflow' or 'control')",
                  code='node.type.invalid',
                  path=json_pointer_path(node_path, 'type'))

    for kind, config in CONTROL_KINDS.items():
        if config['single'] and kind_counts.get(kind, 0) > 1:
            issue(errors,
                  f'at most one {kind!r} node allowed '
                  f'(found {kind_counts[kind]})',
                  code='control.kind.max_instances', path='/nodes')

    id_to_node = {
        node.get('id'): node for node in nodes if isinstance(node, dict)
    }
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        node_id = node.get('id')
        if not isinstance(node_id, str) or not node_id:
            continue
        params = node.get('params') or {}
        if isinstance(params, dict):
            _validate_node_io(
                node, f'node {node_id!r}', params, ids, id_to_node,
                errors, warnings, path=f'/nodes/{index}')

    return ids, kind_counts, role_count, id_to_node


__all__ = ['validate_nodes']
