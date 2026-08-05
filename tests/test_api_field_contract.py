"""Field-level frontend↔backend contract — P1 (docs/TESTING_STRATEGY.md §4).

The path-contract guard (test_frontend_backend_contract.py) proves every
``/api`` path api.js calls RESOLVES to a live route. That is necessary but
not sufficient: the recurring drift class behind it is the route staying
put while its RESPONSE loses or renames a field the frontend reads — the
sidebar renders blank titles, the send flow loses ``taskId``, the model
picker loses ``model_id``. This file pins the SHAPE of the top-N core
endpoints the frontend consumes.

Design rules (anti negative-optimization, per the strategy doc):

* CONSUMER-DRIVEN, not golden-response: pin ONLY the fields the frontend
  reads. Extra response fields are ALLOWED — an intentional additive change
  never breaks the pin; a removed/renamed consumed field always does.
  (A full-response golden pin would break on every additive change and
  train everyone to blind-update it — the snapshot-abuse failure mode.)
* Shapes measured against the LIVE app via ``flask_client`` (the same
  routing + serialization the server uses), never hand-written from docs.
* The checker carries its own NEUTER proof: ``assert_shape`` must BITE on
  wrong shapes, or a vacuous checker would turn every pin into theatre.

Endpoints pinned (api.js consumption order):
  1. POST /api/v1/chat/send        — send flow (taskId/convId/userMessage…)
  2. GET  /api/v1/conversations    — sidebar list ({ok, items[]})
  3. GET  /api/v1/conversations/<id> — conversation open (messages[]…)
  4. GET  /api/v1/server-config    — model picker data source (providers[]…)
  5. POST /api/images/upload       — attachment upload ({ok, filename})
"""

from __future__ import annotations

import base64

import pytest

pytestmark = pytest.mark.api

_PNG_B64 = base64.b64encode(base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)).decode()


# ─── Shape checker ─────────────────────────────────────────────────────
# spec forms:
#   str/int/float/bool/list/dict  — isinstance check (bool excluded from int)
#   (t1, t2, …)                   — union of the above
#   {key: spec, …}                — dict with AT LEAST these keys (extras OK)
#   [spec]                        — list whose EVERY item matches spec
#   'any'                         — present, unconstrained

def _type_ok(value, spec) -> bool:
    if spec == 'any':
        return True
    if isinstance(spec, tuple):
        return any(_type_ok(value, s) for s in spec)
    if isinstance(spec, type):
        if spec is int:
            return isinstance(value, int) and not isinstance(value, bool)
        if spec is float:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return isinstance(value, spec)
    raise TypeError(f'bad leaf spec: {spec!r}')


def assert_shape(data, spec, path='$', _failures=None):
    """Assert ``data`` matches ``spec``; returns [] or a list of failure strings
    with dotted paths. Extra dict keys are allowed by design (consumer-driven
    pin — additive backend changes must not break it)."""
    failures = _failures if _failures is not None else []
    if isinstance(spec, dict):
        if not isinstance(data, dict):
            failures.append(f'{path}: expected dict, got {type(data).__name__}')
            return failures
        for key, sub in spec.items():
            if key not in data:
                failures.append(f'{path}.{key}: MISSING (consumed field dropped?)')
            else:
                assert_shape(data[key], sub, f'{path}.{key}', failures)
        return failures
    if isinstance(spec, list):
        if len(spec) != 1:
            raise TypeError('list spec must have exactly one item spec')
        if not isinstance(data, list):
            failures.append(f'{path}: expected list, got {type(data).__name__}')
            return failures
        for i, item in enumerate(data):
            assert_shape(item, spec[0], f'{path}[{i}]', failures)
        return failures
    if not _type_ok(data, spec):
        failures.append(
            f'{path}: expected {spec!r}, got {type(data).__name__}={data!r}'
            if isinstance(data, (str, int, float, bool)) else
            f'{path}: expected {spec!r}, got {type(data).__name__}')
    return failures


def _assert_endpoint_shape(data, spec, label):
    failures = assert_shape(data, spec)
    assert not failures, (
        f'{label} response shape drifted — a field the frontend reads was '
        f'removed/renamed/retyped. If the change is INTENTIONAL, update the '
        f'pin here AND the api.js consumer in the same commit:\n  ' +
        '\n  '.join(failures))


# ─── Checker NEUTER self-proof ─────────────────────────────────────────

class TestAssertShapeBites:
    """A shape checker that cannot fail is worse than none (snapshot-theatre).
    Each case must produce a failure naming the right path."""

    def test_missing_consumed_field_named(self):
        failures = assert_shape({'ok': True}, {'ok': bool, 'taskId': str})
        assert len(failures) == 1 and '$.taskId' in failures[0]
        assert 'MISSING' in failures[0]

    def test_wrong_type_named(self):
        failures = assert_shape({'msgCount': '3'}, {'msgCount': int})
        assert len(failures) == 1 and '$.msgCount' in failures[0]

    def test_bool_is_not_int(self):
        assert assert_shape({'n': True}, {'n': int}), 'bool passed as int'

    def test_extra_keys_allowed(self):
        assert assert_shape({'ok': True, 'brand_new_field': 1}, {'ok': bool}) == []

    def test_list_items_checked_individually(self):
        failures = assert_shape(
            {'items': [{'id': 'a'}, {'id': 2}]}, {'items': [{'id': str}]})
        assert len(failures) == 1 and '$.items[1].id' in failures[0]

    def test_union_leaf(self):
        assert assert_shape({'v': None}, {'v': (str, type(None))}) == []
        assert assert_shape({'v': 1}, {'v': (str, type(None))})


# ─── Endpoint pins (consumer-driven) ───────────────────────────────────

_CHAT_SEND_SPEC = {
    'ok': bool,
    'taskId': str,
    'convId': str,
    'title': str,
    'isNew': bool,
    'msgCount': int,
    'userMessage': {
        'role': str,
        'content': str,
        'timestamp': int,
    },
}

_CONV_LIST_SPEC = {
    'ok': bool,
    'items': [{
        'id': str,
        'title': str,
        'rev': int,
        'msgCount': int,
        'createdAt': int,
        'updatedAt': int,
        'settings': dict,
    }],
}

_CONV_GET_SPEC = {
    'ok': bool,
    'id': str,
    'title': str,
    'rev': int,
    'messages': [{
        'role': str,
        'content': str,
        'timestamp': int,
    }],
    'settings': dict,
}

_SERVER_CONFIG_SPEC = {
    'ok': bool,
    'providers': [{
        'id': str,
        'name': str,
        'base_url': str,
        'enabled': bool,
        'models': [{
            'model_id': str,
            'capabilities': [str],
        }],
    }],
    'dropdown_models': 'any',
    'models': 'any',
}

_IMAGE_UPLOAD_SPEC = {
    'ok': bool,
    'filename': str,
}


def _send_probe_message(flask_client, conv_id):
    resp = flask_client.post('/api/v1/chat/send', json={
        'convId': conv_id,
        'message': {'text': 'field-contract probe'},
        'config': {'model': 'field-contract-model'},
    })
    return resp


class TestFieldContract:
    """Live-app shape pins for the top-N endpoints api.js consumes."""

    def test_chat_send_response_shape(self, flask_client):
        resp = _send_probe_message(flask_client, 'field-contract-conv-1')
        assert resp.status_code == 200, (
            f'chat/send rejected the canonical body: {resp.status_code} '
            f'{resp.get_json()}')
        _assert_endpoint_shape(resp.get_json(), _CHAT_SEND_SPEC,
                               'POST /api/v1/chat/send')

    def test_conversations_list_shape(self, flask_client):
        _send_probe_message(flask_client, 'field-contract-conv-2')  # seed one
        resp = flask_client.get('/api/v1/conversations')
        assert resp.status_code == 200
        data = resp.get_json()
        _assert_endpoint_shape(data, _CONV_LIST_SPEC,
                               'GET /api/v1/conversations')
        assert any(i.get('id') == 'field-contract-conv-2'
                   for i in data.get('items', [])), (
            'the seeded conversation is absent from the list payload')

    def test_conversation_get_shape(self, flask_client):
        _send_probe_message(flask_client, 'field-contract-conv-3')
        resp = flask_client.get('/api/v1/conversations/field-contract-conv-3')
        assert resp.status_code == 200
        _assert_endpoint_shape(resp.get_json(), _CONV_GET_SPEC,
                               'GET /api/v1/conversations/<id>')

    def test_server_config_model_picker_shape(self, flask_client):
        resp = flask_client.get('/api/v1/server-config')
        assert resp.status_code == 200
        _assert_endpoint_shape(resp.get_json(), _SERVER_CONFIG_SPEC,
                               'GET /api/v1/server-config (model picker)')

    def test_image_upload_shape(self, flask_client):
        resp = flask_client.post('/api/images/upload', json={
            'base64': _PNG_B64, 'mediaType': 'image/png',
        })
        assert resp.status_code == 200, (
            f'image upload failed: {resp.status_code} {resp.get_json()}')
        _assert_endpoint_shape(resp.get_json(), _IMAGE_UPLOAD_SPEC,
                               'POST /api/images/upload')
