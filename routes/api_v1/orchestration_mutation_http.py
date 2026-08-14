"""Shared request and response HTTP contract for orchestration mutations."""

from __future__ import annotations

from dataclasses import dataclass
from lib.api_response import api_bad_request, api_payload
from lib.orchestration.application_result_ports import (
    OrchestrationMutationResultPort,
)
from lib.orchestration.human_gate_request_contract import (
    MAX_HUMAN_INPUT_LENGTH,
    human_approval_request_schema,
    human_gate_request_contract,
    human_input_request_schema,
)
from lib.orchestration.mutation_response import mutation_response
from lib.request_parser import optional_bool, optional_str
from .orchestration_request_http import OrchestrationHttpPreparation


_HUMAN_GATE = human_gate_request_contract()


@dataclass(frozen=True)
class PreparedHumanApprovalRequest:
    request_id: str
    approved: bool


@dataclass(frozen=True)
class PreparedHumanInputRequest:
    request_id: str
    response_text: str


def _required_request_id(
    body: dict,
) -> OrchestrationHttpPreparation[str]:
    field = _HUMAN_GATE['requestIdField']
    request_id = optional_str(body, field, default='').strip()
    if not request_id:
        return OrchestrationHttpPreparation.reject(api_bad_request(
            f'{field} is required', field=field,
        ))
    return OrchestrationHttpPreparation.accept(request_id)


def prepare_human_approval_request(
    body: dict,
) -> OrchestrationHttpPreparation[PreparedHumanApprovalRequest]:
    request_id_result = _required_request_id(body)
    if not request_id_result.accepted:
        return OrchestrationHttpPreparation.reject(
            request_id_result.failure)
    request_id = request_id_result.require()
    return OrchestrationHttpPreparation.accept(PreparedHumanApprovalRequest(
        request_id=request_id,
        approved=optional_bool(
            body,
            _HUMAN_GATE['approvalField'],
            default=_HUMAN_GATE['approvalDefault'],
        ),
    ))


def prepare_human_input_request(
    body: dict,
) -> OrchestrationHttpPreparation[PreparedHumanInputRequest]:
    request_id_result = _required_request_id(body)
    if not request_id_result.accepted:
        return OrchestrationHttpPreparation.reject(
            request_id_result.failure)
    request_id = request_id_result.require()
    field = _HUMAN_GATE['inputField']
    response_text = optional_str(
        body, field, default='', max_len=_HUMAN_GATE['inputMaxLength'],
    )
    if not response_text:
        return OrchestrationHttpPreparation.reject(api_bad_request(
            f'{field} is required', field=field,
        ))
    return OrchestrationHttpPreparation.accept(PreparedHumanInputRequest(
        request_id=request_id,
        response_text=response_text,
    ))


def mutation_http_response(
    result: OrchestrationMutationResultPort,
    *,
    compatibility: dict | None = None,
):
    """Project every mutation result through one wire/status boundary."""
    payload, status = mutation_response(
        result, compatibility=compatibility,
    )
    return api_payload(payload, status)


__all__ = [
    'MAX_HUMAN_INPUT_LENGTH',
    'PreparedHumanApprovalRequest',
    'PreparedHumanInputRequest',
    'human_approval_request_schema',
    'human_input_request_schema',
    'prepare_human_approval_request',
    'prepare_human_input_request',
    'mutation_http_response',
]
