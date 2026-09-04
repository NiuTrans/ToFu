"""Typed value object for one orchestration HTTP endpoint."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OrchestrationHttpEndpoint:
    """Stable public identity of one orchestration HTTP operation."""

    route: str
    method: str
    response_contract: str
    path_args: tuple[tuple[str, int], ...] = ()
    query_args: tuple[tuple[str, int], ...] = ()
    body_args: tuple[tuple[str, int], ...] = ()
    body_arg: int | None = None
    request_options_arg: int | None = None
    write_operation: str = ''
    write_version_arg: int | None = None
    write_contract_arg: int | None = None

    @property
    def query_fields(self) -> tuple[str, ...]:
        return tuple(field for field, _index in self.query_args)

    @property
    def path_fields(self) -> tuple[str, ...]:
        return tuple(field for field, _index in self.path_args)

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            'route': self.route,
            'method': self.method,
            'responseContract': self.response_contract,
        }
        optional_mappings = (
            ('pathArgs', self.path_args),
            ('queryArgs', self.query_args),
            ('bodyArgs', self.body_args),
        )
        for field, value in optional_mappings:
            if value:
                result[field] = dict(value)
        optional_scalars = (
            ('bodyArg', self.body_arg),
            ('requestOptionsArg', self.request_options_arg),
        )
        for field, value in optional_scalars:
            if value is not None:
                result[field] = value
        if self.write_operation:
            result['writeOperation'] = self.write_operation
        if self.write_version_arg is not None:
            result['writeVersionArg'] = self.write_version_arg
        if self.write_contract_arg is not None:
            result['writeContractArg'] = self.write_contract_arg
        return result


__all__ = ['OrchestrationHttpEndpoint']
