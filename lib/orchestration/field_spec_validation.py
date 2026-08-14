"""Shared value validation for backend-authored role/control FieldSpecs."""

from __future__ import annotations

from lib.orchestration.field_issue_codes import (
    FIELD_CHOICE,
    FIELD_MAXIMUM,
    FIELD_MAX_ITEMS,
    FIELD_MAX_ITEM_LENGTH,
    FIELD_MAX_LENGTH,
    FIELD_MINIMUM,
    FIELD_RUNTIME_MAX,
    FIELD_TYPE_BOOLEAN,
    FIELD_TYPE_INTEGER,
    FIELD_TYPE_LIST,
    FIELD_TYPE_STRING,
    FIELD_UNKNOWN,
)
from lib.orchestration.io_values import _coerce_list
from lib.orchestration.validation_issues import (
    json_pointer_path,
    report_validation_issue,
)


def validate_field_specs(
    schema: list[dict],
    *,
    where: str,
    params: dict,
    owner_kind: str,
    owner_name: str,
    infra_keys: frozenset[str] = frozenset(),
    errors: list,
    warnings: list,
    max_text_length: int,
    max_list_items: int | None = None,
    max_list_item_length: int | None = None,
    path_prefix: str = '',
) -> None:
    """Validate ``params`` against one shared backend ``FieldSpec`` list."""
    by_key = {spec['key']: spec for spec in schema}

    for key, value in params.items():
        field_path = json_pointer_path(path_prefix, key)
        spec = by_key.get(key)
        if spec is None:
            if key not in infra_keys:
                report_validation_issue(
                    warnings,
                    f'{where} unknown param {key!r} for {owner_kind} '
                    f'{owner_name!r} (ignored by the engine)',
                    code=FIELD_UNKNOWN,
                    path=field_path,
                )
            continue
        if value is None:
            continue

        error_name = spec.get('errorName') or f'param {key!r}'
        target = warnings if spec.get('severity') == 'warning' else errors
        field_kind = spec['kind']

        if field_kind == 'list':
            if not isinstance(value, (list, tuple, str)):
                report_validation_issue(
                    target, f'{where} {error_name} must be a list',
                    code=FIELD_TYPE_LIST, path=field_path)
                continue
            items = _coerce_list(value)
            item_cap = spec.get('maxItems', max_list_items)
            if item_cap is not None and len(items) > item_cap:
                report_validation_issue(
                    target, f'{where} {error_name} exceeds {item_cap} items',
                    code=FIELD_MAX_ITEMS, path=field_path)
            item_len = spec.get('maxItemLength', max_list_item_length)
            if item_len is not None:
                for item in items:
                    if len(item) > item_len:
                        report_validation_issue(
                            target,
                            f'{where} {error_name} item exceeds '
                            f'{item_len} chars',
                            code=FIELD_MAX_ITEM_LENGTH, path=field_path,
                        )
                        break
        elif field_kind == 'bool':
            if not isinstance(value, bool):
                report_validation_issue(
                    target, f'{where} {error_name} must be a boolean',
                    code=FIELD_TYPE_BOOLEAN, path=field_path)
        elif field_kind == 'int':
            valid_integer = isinstance(value, int) and not isinstance(value, bool)
            if not valid_integer:
                report_validation_issue(
                    target, f'{where} {error_name} must be an integer',
                    code=FIELD_TYPE_INTEGER, path=field_path)
            elif spec.get('min') is not None and value < spec['min']:
                report_validation_issue(
                    target,
                    f'{where} {error_name} must be >= {spec["min"]}',
                    code=FIELD_MINIMUM, path=field_path)
            elif spec.get('max') is not None and value > spec['max']:
                report_validation_issue(
                    target,
                    f'{where} {error_name} must be <= {spec["max"]}',
                    code=FIELD_MAXIMUM, path=field_path)
            runtime_max = spec.get('runtimeMax')
            if (valid_integer and runtime_max is not None
                    and value > runtime_max):
                report_validation_issue(
                    warnings,
                    f'{where} {error_name} exceeds the default runtime '
                    f'ceiling {runtime_max} and will be capped',
                    code=FIELD_RUNTIME_MAX, path=field_path,
                )
        elif field_kind == 'select':
            choices = {option['value'] for option in spec.get('options', [])}
            if (not isinstance(value, str)
                    or (value not in choices and not spec.get('allowUnknown'))):
                report_validation_issue(
                    target,
                    f'{where} {error_name} must be one of {sorted(choices)}',
                    code=FIELD_CHOICE, path=field_path,
                )
        else:  # text / textarea
            if not isinstance(value, str):
                report_validation_issue(
                    target, f'{where} {error_name} must be a string',
                    code=FIELD_TYPE_STRING, path=field_path)
            else:
                limit = spec.get('maxLength', max_text_length)
                if len(value) > limit:
                    report_validation_issue(
                        target,
                        f'{where} {error_name} exceeds {limit} chars',
                        code=FIELD_MAX_LENGTH, path=field_path)


__all__ = ['validate_field_specs']
