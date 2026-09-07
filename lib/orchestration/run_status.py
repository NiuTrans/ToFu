"""Canonical lifecycle vocabulary for durable orchestration runs.

The database writer, HTTP replay adapter and browser all need to agree on
which statuses are terminal.  Keeping the vocabulary framework-free lets the
authoring contract publish the same protocol without importing persistence.
"""

from __future__ import annotations

from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration.wire_formats import RUN_STATUS_FORMAT as RUN_STATUS_SCHEMA

RUN_STATUS_ORDER = (
    'pending', 'running', 'paused', 'done', 'error', 'aborted',
)
INITIAL_RUN_STATUS = RUN_STATUS_ORDER[0]
VALID_RUN_STATUSES = frozenset(RUN_STATUS_ORDER)
TERMINAL_RUN_STATUSES = frozenset({'done', 'error', 'aborted'})
RUN_STATUS_CATEGORIES = {
    'pending': 'queued',
    'running': 'active',
    'paused': 'blocked',
    'done': 'success',
    'error': 'failure',
    'aborted': 'cancelled',
}


def is_run_status(status: str | None) -> bool:
    """Return whether ``status`` belongs to the published lifecycle."""
    return isinstance(status, str) and status in VALID_RUN_STATUSES


def is_terminal_run_status(status: str | None) -> bool:
    """Return whether no further run events are expected for ``status``."""
    return str(status or '') in TERMINAL_RUN_STATUSES


def run_status_contract() -> dict:
    """Return a detached, transport-safe lifecycle protocol snapshot."""
    return {
        'schema': RUN_STATUS_SCHEMA,
        'initial': INITIAL_RUN_STATUS,
        'statuses': list(RUN_STATUS_ORDER),
        'terminal': [
            status for status in RUN_STATUS_ORDER
            if status in TERMINAL_RUN_STATUSES
        ],
        # Semantic categories let every client present lifecycle states
        # consistently without duplicating knowledge of concrete statuses.
        'categories': {
            status: RUN_STATUS_CATEGORIES[status]
            for status in RUN_STATUS_ORDER
        },
    }


def run_status_contract_schema() -> dict:
    """Return an OpenAPI-compatible schema derived from the live protocol."""
    return contract_snapshot_schema(run_status_contract())


__all__ = [
    'RUN_STATUS_SCHEMA', 'RUN_STATUS_ORDER', 'INITIAL_RUN_STATUS',
    'RUN_STATUS_CATEGORIES',
    'VALID_RUN_STATUSES', 'TERMINAL_RUN_STATUSES', 'is_run_status',
    'is_terminal_run_status', 'run_status_contract',
    'run_status_contract_schema',
]
