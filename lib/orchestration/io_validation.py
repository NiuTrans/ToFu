"""Definition validation for optional Typed-I/O port declarations."""

from __future__ import annotations

from lib.orchestration.io_contract import (
    IO_PORT_NAME_CONTRACT,
    IO_START_REF,
    MAX_IO_PORTS,
    VALID_IO_TYPES,
)
from lib.orchestration.io_issue_codes import (
    IO_PORT_NAME_DUPLICATE,
    IO_PORT_NAME_REQUIRED,
    IO_SIDE_MAX_PORTS,
)
from lib.orchestration.io_values import node_output_names, parse_io_ref
from lib.orchestration.validation_issues import (
    json_pointer_path,
    report_validation_issue,
)


def _validate_node_io(node: dict, where: str, params: dict, ids: set,
                      id_to_node: dict, errors: list, warnings: list,
                      path: str = '') -> None:
    """Validate one node's optional ``params.io`` contract."""
    io = params.get('io')
    io_path = f'{path}/params/io' if path else '/params/io'
    if io is None:
        return
    if not isinstance(io, dict):
        report_validation_issue(
            errors, f'{where} io must be an object',
            code='io.type.object', path=io_path)
        return

    for side in ('inputs', 'outputs'):
        ports = io.get(side)
        side_path = json_pointer_path(io_path, side)
        if ports is None:
            continue
        if not isinstance(ports, list):
            report_validation_issue(
                errors, f'{where} io.{side} must be an array',
                code='io.side.type.array', path=side_path)
            continue
        if len(ports) > MAX_IO_PORTS:
            report_validation_issue(
                errors, f'{where} io.{side} exceeds {MAX_IO_PORTS} ports',
                code=IO_SIDE_MAX_PORTS, path=side_path)
        seen_names: set[str] = set()
        for index, port in enumerate(ports):
            port_where = f'{where} io.{side}[{index}]'
            port_path = json_pointer_path(side_path, index)
            if not isinstance(port, dict):
                report_validation_issue(
                    errors, f'{port_where} must be an object',
                    code='io.port.type.object', path=port_path)
                continue
            port_name = port.get('name')
            if not isinstance(port_name, str) or not port_name.strip():
                if IO_PORT_NAME_CONTRACT['required']:
                    report_validation_issue(
                        errors, f'{port_where} missing string name',
                        code=IO_PORT_NAME_REQUIRED,
                        path=json_pointer_path(port_path, 'name'))
            elif (IO_PORT_NAME_CONTRACT['uniqueWithinSide']
                  and port_name in seen_names):
                report_validation_issue(
                    errors,
                    f'{port_where} duplicate port name {port_name!r}',
                    code=IO_PORT_NAME_DUPLICATE,
                    path=json_pointer_path(port_path, 'name'))
            else:
                seen_names.add(port_name)
            port_type = port.get('type')
            if port_type is not None and port_type not in VALID_IO_TYPES:
                report_validation_issue(
                    errors,
                    f'{port_where} invalid type {port_type!r} '
                    f'(expected one of {sorted(VALID_IO_TYPES)})',
                    code='io.port.type.invalid',
                    path=json_pointer_path(port_path, 'type'))
            if side != 'inputs':
                continue
            source = port.get('from')
            if source is None or source == '':
                continue
            if not isinstance(source, str):
                report_validation_issue(
                    errors, f'{port_where} from must be a string',
                    code='io.input.from.type.string',
                    path=json_pointer_path(port_path, 'from'))
                continue
            source_id, source_output = parse_io_ref(source)
            if source_id == IO_START_REF:
                continue
            if source_id not in ids:
                report_validation_issue(
                    errors,
                    f'{port_where} from {source!r} references unknown node',
                    code='io.input.from.unknown_node',
                    path=json_pointer_path(port_path, 'from'))
                continue
            if source_output is None:
                continue
            available = node_output_names(id_to_node.get(source_id) or {})
            if source_output not in available:
                report_validation_issue(
                    warnings,
                    f'{port_where} from {source!r}: node {source_id!r} does '
                    f'not declare an output named {source_output!r} '
                    f'(has {available})',
                    code='io.input.from.unknown_output',
                    path=json_pointer_path(port_path, 'from'))


__all__ = ['_validate_node_io']
