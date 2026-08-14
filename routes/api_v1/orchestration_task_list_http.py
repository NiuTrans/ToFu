"""HTTP ingress and projection for durable orchestration run collections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lib.api_response import api_bad_request, api_ok
from lib.log import get_logger
from lib.orchestration.durable_run_field_registry import (
    durable_run_list_envelope_contract,
)
from lib.orchestration.http_endpoint_contract import (
    orchestration_http_endpoint,
)
from lib.orchestration.run_status import RUN_STATUS_ORDER, is_run_status
from lib.request_parser import query_str
from .orchestration_request_http import OrchestrationHttpPreparation


_DURABLE_RUN_LIST_ENVELOPE = durable_run_list_envelope_contract()
_STATUS_QUERY, _ORCHESTRATION_QUERY, _LIMIT_QUERY = \
    orchestration_http_endpoint('task-list').query_fields
DURABLE_RUN_PAGE_SIZE = _DURABLE_RUN_LIST_ENVELOPE['defaultLimit']
DURABLE_RUN_PAGE_MAX = _DURABLE_RUN_LIST_ENVELOPE['maxLimit']
logger = get_logger(__name__)


@dataclass(frozen=True)
class PreparedDurableRunListQuery:
    status: str
    orchestration_id: str
    limit: int

    @property
    def probe_limit(self) -> int:
        """Fetch one extra header so the response can project ``has_more``."""
        return self.limit + 1


def durable_run_list_parameters() -> list[dict]:
    return [
        {
            'name': _STATUS_QUERY,
            'in': 'query',
            'schema': {
                'type': 'string',
                'enum': list(RUN_STATUS_ORDER),
            },
            'description': 'Filter by canonical durable-run status.',
        },
        {
            'name': _ORCHESTRATION_QUERY,
            'in': 'query',
            'schema': {'type': 'string'},
            'description': 'Filter by persisted orchestration definition id.',
        },
        {
            'name': _LIMIT_QUERY,
            'in': 'query',
            'schema': {
                'type': 'integer', 'minimum': 1,
                'maximum': DURABLE_RUN_PAGE_MAX,
                'default': DURABLE_RUN_PAGE_SIZE,
            },
            'description': 'Newest durable-run headers to return.',
        },
    ]


def prepare_durable_run_list_query(
    args: Mapping[str, Any],
) -> OrchestrationHttpPreparation[PreparedDurableRunListQuery]:
    """Parse list filters and project invalid statuses identically."""
    status = query_str(args, _STATUS_QUERY)
    if status and not is_run_status(status):
        return OrchestrationHttpPreparation.reject(api_bad_request(
            'Invalid orchestration run status',
            statuses=list(RUN_STATUS_ORDER),
        ))
    raw_limit = query_str(args, _LIMIT_QUERY)
    try:
        limit = int(raw_limit) if raw_limit else DURABLE_RUN_PAGE_SIZE
    except (TypeError, ValueError) as exc:
        logger.debug('invalid run-list limit %r: %s', raw_limit, exc)
        limit = 0
    if limit < 1 or limit > DURABLE_RUN_PAGE_MAX:
        return OrchestrationHttpPreparation.reject(api_bad_request(
            'Invalid orchestration run list limit',
            minimum=1,
            maximum=DURABLE_RUN_PAGE_MAX,
        ))
    return OrchestrationHttpPreparation.accept(PreparedDurableRunListQuery(
        status=status,
        orchestration_id=query_str(args, _ORCHESTRATION_QUERY),
        limit=limit,
    ))


def durable_run_list_response(runs: list[dict], limit: int):
    contract = _DURABLE_RUN_LIST_ENVELOPE
    visible = list(runs[:limit])
    return api_ok(**{
        contract['itemsField']: visible,
        contract['pageField']: {
            contract['limitField']: limit,
            contract['hasMoreField']: len(runs) > limit,
            contract['nextLimitField']: (
                min(limit + contract['pageStep'], contract['maxLimit'])
                if len(runs) > limit and limit < contract['maxLimit']
                else None
            ),
        },
    })


__all__ = [
    'PreparedDurableRunListQuery',
    'DURABLE_RUN_PAGE_SIZE', 'DURABLE_RUN_PAGE_MAX',
    'durable_run_list_parameters', 'prepare_durable_run_list_query',
    'durable_run_list_response',
]
