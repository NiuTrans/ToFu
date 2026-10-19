"""routes/api_v1/client_support.py — native client support-report intake.

The Android shell's "Report a problem" flow builds a bounded diagnostic blob
(``com.tofu.client.session.ClientReport``) and POSTs it here when it can reach
the connected Tofu server. This route validates and bounds that blob, stamps
server-side arrival metadata, and hands it to
:mod:`lib.client_report_store`, which persists it under
``<data_root>/client_reports/`` for the operator to read.

  * ``POST /api/v1/mobile/client-report`` — ingest one report.
  * ``GET  /api/v1/mobile/client-report`` — list the newest stored reports, so
    the operator can read what was sent without shelling into the host.

The report is untrusted client input: every field is length-clipped, the body
is size-capped, and no field is ever interpreted as a path or command. Nothing
here is authenticated beyond the shared :func:`require_auth` gate, because a
report carries evidence the sender already chose to disclose.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from quart import Blueprint, request

from lib import client_report_store
from lib.api_response import api_bad_request, api_ok, api_payload_too_large
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import (
    BadRequest,
    optional_dict,
    optional_int,
    optional_list,
    optional_str,
    parse_body,
)

from .auth import require_auth

logger = get_logger(__name__)

api_v1_client_support_bp = Blueprint('api_v1_client_support', __name__)

_REPORT_SCHEMA = 'tofu.client-report/v1'

# Bounds. The client already trims, but the server never trusts that: a
# compromised or future client must not be able to blow up memory or disk.
_MAX_BODY_BYTES = 1024 * 1024
_MAX_NOTE_CHARS = 8000
_MAX_EVENTS = 300
_MAX_WEB_CHARS = 512 * 1024
_MAX_EVENT_MSG = 2000


def _clip(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _clean_events(events: list[Any]) -> tuple[list[dict], bool]:
    truncated = len(events) > _MAX_EVENTS
    if truncated:
        events = events[-_MAX_EVENTS:]
    cleaned: list[dict] = []
    for event in events:
        cleaned.append({
            't': event.get('t'),
            'lvl': str(event.get('lvl', ''))[:4],
            'tag': str(event.get('tag', ''))[:64],
            'msg': str(event.get('msg', ''))[:_MAX_EVENT_MSG],
        })
    return cleaned, truncated


def _normalize(body: dict) -> tuple[dict, bool]:
    """Validate + bound an untrusted report body into a storable record."""
    truncated = False
    note, cut = _clip(optional_str(body, 'note'), _MAX_NOTE_CHARS)
    truncated = truncated or cut
    web, cut = _clip(optional_str(body, 'web'), _MAX_WEB_CHARS)
    truncated = truncated or cut
    events, cut = _clean_events(
        optional_list(body, 'events', item_type=dict, max_len=10_000),
    )
    truncated = truncated or cut
    record = {
        'schema': optional_str(body, 'schema', max_len=64) or _REPORT_SCHEMA,
        'reportId': optional_str(body, 'reportId', max_len=80),
        'clientCreatedAt': optional_str(body, 'createdAt', max_len=40),
        'receivedAt': int(time.time() * 1000),
        'note': note,
        'app': optional_dict(body, 'app'),
        'device': optional_dict(body, 'device'),
        'profile': optional_dict(body, 'profile'),
        'events': events,
        'web': web,
        'truncated': truncated,
    }
    return record, truncated


@api_v1_client_support_bp.route('/api/v1/mobile/client-report', methods=['POST'])
@require_auth
@api_meta(
    summary='Ingest a native client support report',
    description=(
        'Accepts the bounded diagnostic blob built by the Android shell\'s '
        '"Report a problem" flow and persists it under '
        '``<data_root>/client_reports/`` for the operator. Unknown fields are '
        'dropped; ``note``/``web``/``events`` are length-clipped.'),
    tags=['mobile'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'properties': {
                'schema': {'type': 'string'},
                'reportId': {'type': 'string'},
                'createdAt': {'type': 'string'},
                'note': {'type': 'string'},
                'app': {'type': 'object'},
                'device': {'type': 'object'},
                'profile': {'type': 'object'},
                'events': {'type': 'array', 'items': {'type': 'object'}},
                'web': {'type': 'string'},
            },
        },
    }}},
)
def mobile_client_report_post_v1():
    declared = request.content_length
    if declared is not None and declared > _MAX_BODY_BYTES:
        return api_payload_too_large(_MAX_BODY_BYTES)
    try:
        body = parse_body(strict=True, exact=(
            'schema', 'reportId', 'createdAt', 'note', 'app', 'device',
            'profile', 'events', 'web'))
    except BadRequest as error:
        return api_bad_request(str(error))
    if not body:
        return api_bad_request('empty report body')

    record, truncated = _normalize(body)
    if not record['reportId']:
        record['reportId'] = uuid.uuid4().hex[:12]
    record['remote'] = (request.remote_addr or '')[:64]

    stored = client_report_store.append_report(record)
    logger.info(
        '[client_support] report %s stored (%d bytes, truncated=%s)',
        record['reportId'], stored, truncated)
    return api_ok(reportId=record['reportId'], stored=stored,
                  truncated=truncated)


@api_v1_client_support_bp.route('/api/v1/mobile/client-report', methods=['GET'])
@require_auth
@api_meta(
    summary='List recent native client support reports',
    description=(
        'Newest-first view of the reports stored under '
        '``<data_root>/client_reports/`` so the operator can triage what '
        'testers sent. ``limit`` defaults to 20 and is capped at 200.'),
    tags=['mobile'],
    request_body=False,
)
def mobile_client_report_list_v1():
    # Clamp rather than reject: an over-large limit from a curious client
    # should return the cap, not a 400 that hides every report.
    limit = optional_int(
        {'limit': request.args.get('limit')}, 'limit', default=20, min=1)
    reports = client_report_store.read_recent(limit)
    return api_ok(reports=reports, count=len(reports),
                  path=str(client_report_store.current_path()))
