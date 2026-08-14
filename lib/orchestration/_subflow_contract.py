"""Subflow authoring and recursive-validation contract.

Owns the nesting cap and subflow-node parameter rules. The recursive child
validator is injected so this module remains pure and does not import the
whole-definition validator back into itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.orchestration._role_axes import VALID_EMITS, VALID_SCOPES
from lib.orchestration.validation_issues import (
    json_pointer_path, report_nested_validation_verdict,
    report_validation_issue)


#: Shared nesting cap consumed by authoring, validation and execution.
MAX_SUBFLOW_DEPTH = 5

ChildValidator = Callable[[Any, int, frozenset[str]], dict[str, Any]]


def validate_subflow_node(
    node: dict, where: str, params: dict, errors: list, warnings: list,
    depth: int, seen_refs: frozenset[str], validate_child: ChildValidator,
    path: str = '',
) -> None:
    """Validate one embedded/referenced subflow node.

    ``validate_child`` receives ``(definition, depth, seen_refs)`` and returns
    the standard validation verdict. Injecting it avoids a module cycle while
    keeping recursive errors and warnings byte-compatible.
    """
    del node  # reserved for future node-level subflow rules

    emits = params.get('emits')
    params_path = f'{path}/params' if path else '/params'
    if emits is not None and emits not in VALID_EMITS:
        report_validation_issue(
            errors,
            f'{where} invalid emits {emits!r} '
            f'(expected one of {sorted(VALID_EMITS)})',
            code='subflow.emits.invalid',
            path=json_pointer_path(params_path, 'emits'))

    scope = params.get('scope')
    if scope is not None and scope not in VALID_SCOPES:
        report_validation_issue(
            errors,
            f'{where} invalid scope {scope!r} '
            f'(expected one of {sorted(VALID_SCOPES)})',
            code='subflow.scope.invalid',
            path=json_pointer_path(params_path, 'scope'))

    child = params.get('definition')
    ref = params.get('ref')
    if child is None and ref is None:
        report_validation_issue(
            errors,
            f'{where} subflow needs params.definition (embedded) '
            'or params.ref (stored id)',
            code='subflow.source.required', path=params_path)
        return

    if ref is not None:
        if not isinstance(ref, str) or not ref:
            report_validation_issue(
                errors,
                f'{where} subflow ref must be a non-empty string',
                code='subflow.ref.required',
                path=json_pointer_path(params_path, 'ref'))
        elif ref in seen_refs:
            report_validation_issue(
                errors,
                f'{where} subflow ref {ref!r} is recursive '
                '(references an ancestor flow)',
                code='subflow.ref.recursive',
                path=json_pointer_path(params_path, 'ref'))
        return

    child_depth = depth + 1
    if child_depth > MAX_SUBFLOW_DEPTH:
        report_validation_issue(
            errors,
            f'{where} subflow nesting exceeds MAX_SUBFLOW_DEPTH '
            f'({MAX_SUBFLOW_DEPTH})',
            code='subflow.depth.exceeded',
            path=json_pointer_path(params_path, 'definition'))
        return

    verdict = validate_child(child, child_depth, seen_refs)
    report_nested_validation_verdict(
        errors, warnings, verdict,
        message_prefix=f'{where} subflow: ',
        path_prefix=json_pointer_path(params_path, 'definition'),
        fallback_code_prefix='subflow.child',
    )
