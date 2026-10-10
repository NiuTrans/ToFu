"""Envelope normalization for the legacy test ladder on the v4 surface.

The production API serves only v4: success bodies are ``{data, meta}`` and
errors are RFC 7807 problem documents. The wire envelope contract is owned by
``tests/api/test_api_v4_*``; the legacy ladder asserts handler payloads and
legacy error anatomy. These helpers project a v4 body back to the legacy
shape so the ladder keeps asserting domain behaviour, not transport framing:

- success: the ``data`` payload is flattened and ``ok: True`` defaulted, preserving explicit domain failure;
- problem: mapped to ``{'ok': False, 'error': {'code', 'message'}}`` with the
  original problem document preserved under ``problem``.
"""
from __future__ import annotations

from typing import Any


def is_v4_envelope(body: Any) -> bool:
    return isinstance(body, dict) and set(body) == {'data', 'meta'}


def is_problem_document(body: Any) -> bool:
    return (
        isinstance(body, dict)
        and 'type' in body
        and 'title' in body
        and 'status' in body
    )


def v1_shaped(body: Any) -> Any:
    """Project a v4 wire body to the legacy shape; pass others through."""
    if is_v4_envelope(body):
        data = body['data']
        if isinstance(data, dict):
            return {'ok': True, **data}
        return {'data': data, 'ok': True}
    if is_problem_document(body):
        code = str(body.get('code') or '').strip()
        if not code:
            status = int(body.get('status') or 500)
            code = {400: 'bad_request', 401: 'unauthorized',
                    403: 'forbidden', 404: 'not_found',
                    409: 'conflict', 413: 'payload_too_large',
                    429: 'rate_limited'}.get(status, 'internal_error')
        return {
            'ok': False,
            'error': {
                # Legacy consumers read both ``kind`` (typed envelope) and
                # ``code``; the problem document carries one stable value.
                'kind': code,
                'code': code,
                'message': str(body.get('detail') or body.get('title') or ''),
            },
            'problem': body,
        }
    return body


async def v1_get_json(response) -> Any:
    """``response.get_json()`` plus legacy-shape projection; see module doc."""
    return v1_shaped(await response.get_json())
