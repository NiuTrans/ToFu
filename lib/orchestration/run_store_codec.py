"""Row and JSON codecs shared by durable-run database repositories."""

from __future__ import annotations

import json

from lib.log import get_logger
from lib.orchestration.durable_run_field_registry import (
    project_durable_run_snapshot,
)
from lib.orchestration.run_status import (
    INITIAL_RUN_STATUS,
    is_terminal_run_status,
)
from lib.orchestration.run_store_port import OrchestrationRunStoreError


logger = get_logger(__name__)


def encode_run_json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise OrchestrationRunStoreError(
            'failed to encode durable orchestration payload') from error


def decode_run_json(raw, default, *, strict: bool = False):
    if raw is None or raw == '':
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as error:
        if strict:
            raise OrchestrationRunStoreError(
                'failed to decode durable orchestration payload') from error
        logger.debug('[OrchRuns] JSON decode failed: %s', error)
        return default


def decode_run_error(raw):
    """Decode JSON errors while preserving legacy plain-string values."""
    if not (raw or ''):
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as error:
        logger.debug(
            '[OrchRuns] error column not JSON, returning verbatim: %s',
            error,
        )
        return raw


def row_to_run_header(row, *, include_definition: bool) -> dict:
    decoded = {
        'id': row['id'],
        'orch_id': row['orch_id'] or '',
        'name': row['name'] or '',
        'status': row['status'] or INITIAL_RUN_STATUS,
        'terminal': is_terminal_run_status(row['status']),
        'final': row['final'] or '',
        'error': decode_run_error(row['error']),
        'created_by': row['created_by'] or '',
        'created_at': row['created_at'] or 0,
        'updated_at': row['updated_at'] or 0,
        'finished_at': row['finished_at'] or 0,
    }
    if include_definition:
        definition = decode_run_json(row['definition'], {}, strict=True)
        if not isinstance(definition, dict):
            raise OrchestrationRunStoreError(
                'durable orchestration definition is not an object')
        decoded['definition'] = definition
        decoded['input'] = row['input'] or ''
    return project_durable_run_snapshot(
        decoded, detail=include_definition)


__all__ = [
    'encode_run_json', 'decode_run_json', 'decode_run_error',
    'row_to_run_header',
]
