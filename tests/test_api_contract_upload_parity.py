#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/upload.py
envelope migration (api-contract epic pt_931e16c4, batch 8).

10 ad-hoc sites, all dict payloads:

  * upload/parse successes   jsonify({'ok': True, …}) / jsonify({'success': True, …})
                              → api_ok({...})   (the parse bodies carry
                              ``success`` not ``ok`` — +ok is purely additive)
  * result passthroughs      jsonify(response_data) / jsonify(resp)
                              → api_ok(...)
  * image-gen error          jsonify(errbody), status_code (400|503|500,
                              runtime-variable) → api_payload(errbody, status_code)
                              (body already carries ok:False — byte-identical)

Multipart FormData requests are §4 carve-outs on the REQUEST side only —
every RESPONSE here is a JSON envelope. Layers: PARITY + SHIPPED-SOURCE.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TARGET = os.path.join(_ROOT, 'routes', 'upload.py')

pytestmark = pytest.mark.unit


def _make_app():
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    return Quart(__name__)


async def _resolve(resp):
    response, status = resp
    body = await response.get_data(as_text=True)
    return status, (json.loads(body) if body else {})


def _sites():
    from lib.api_response import api_ok, api_payload
    upload_ok = {'ok': True, 'url': '/api/images/f.png', 'filename': 'f.png'}
    gen_err = {'ok': False, 'error': 'rate limited', 'error_type': 'rate_limited',
               'rate_limited': True, 'block_reason': '', 'text': '',
               'history_resolved': 0, 'provider_status_code': 429}
    gen_ok = {'ok': True, 'text': '', 'mime_type': 'image/png', 'model': 'm',
              'provider_id': 'p', 'history_resolved': 0,
              'image_url': '/api/images/gen_1.png', 'filename': 'gen_1.png',
              'file_size': 123}
    pdf_ok = {'success': True, 'filename': 'a.pdf', 'fileSize': 10,
              'text': 'hello', 'totalPages': 1, 'textLength': 5,
              'method': 'pymupdf', 'isScanned': False}
    vlm_resp = {'status': 'done', 'progress': 100, 'filename': 'a.pdf',
                'result': 'text', 'textLength': 4}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('upload-b64-ok', dict(upload_ok), 200,
         lambda: api_ok(dict(upload_ok)), False),
        ('upload-form-ok', dict(upload_ok), 200,
         lambda: api_ok(dict(upload_ok)), False),
        ('imagegen-err-503', dict(gen_err), 503,
         lambda: api_payload(dict(gen_err), 503), True),
        ('imagegen-err-400', dict(gen_err, error_type='client_error'), 400,
         lambda: api_payload(dict(gen_err, error_type='client_error'), 400),
         True),
        ('imagegen-ok', dict(gen_ok), 200, lambda: api_ok(dict(gen_ok)),
         False),
        ('models', {'models': [{'model': 'm', 'available': True}]}, 200,
         lambda: api_ok({'models': [{'model': 'm', 'available': True}]}),
         False),
        ('pdf-parse-ok', dict(pdf_ok), 200, lambda: api_ok(dict(pdf_ok)),
         False),
        ('vlm-start', {'taskId': 't1'}, 200,
         lambda: api_ok({'taskId': 't1'}), False),
        ('vlm-status', dict(vlm_resp), 200, lambda: api_ok(dict(vlm_resp)),
         False),
        ('vlm-tasks', {'tasks': [{'taskId': 't1'}]}, 200,
         lambda: api_ok({'tasks': [{'taskId': 't1'}]}), False),
        ('doc-parse-ok', dict(pdf_ok, filename='a.docx'), 200,
         lambda: api_ok(dict(pdf_ok, filename='a.docx')), False),
    ]


def test_envelope_parity():
    """status identical; legacy keys byte-identical; additions ⊆
    {ok, request_id} (+error on error sites); ok flag follows legacy body."""
    from flask import jsonify
    app = _make_app()

    async def _t():
        async with app.test_request_context('/test'):
            for label, legacy_body, legacy_status, new, is_error in _sites():
                leg_status, leg_body = await _resolve(
                    (jsonify(legacy_body), legacy_status))
                new_status, new_body = await _resolve(new())

                assert new_status == leg_status, (
                    f'{label}: status {new_status} != legacy {leg_status}')
                new_body.pop('request_id', None)
                for k, v in leg_body.items():
                    assert k in new_body and new_body[k] == v, (
                        f'{label}: legacy key {k!r} lost/changed')
                added = set(new_body) - set(leg_body)
                allowed = {'ok', 'error'} if is_error else {'ok'}
                assert added <= allowed, (
                    f'{label}: unexpected added keys {added}')
                expected_ok = leg_body.get('ok', not is_error)
                assert new_body.get('ok') is expected_ok, (
                    f'{label}: ok flag wrong')

    asyncio.run(_t())


def test_shipped_source_converted():
    """routes/upload.py carries no ad-hoc jsonify( and no flask jsonify
    import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/upload.py still builds responses with bare jsonify( — '
        'convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/upload.py still imports jsonify')
    assert 'api_payload(' in src, (
        'expected api_payload( CALLS in upload.py (the runtime-status '
        'image-gen error site) — paren needle')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
