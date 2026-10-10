"""Route tests for POST /api/v1/logs/client — the browser-console relay sink
(epic pt_cfdfd30c8699407b, 2026-08-05).

The frontend relay (frontend/src/runtime/sections/core/client_log_relay.js)
batch-POSTs the
patched console stream here so live-view diagnostics land in
logs/frontend.log instead of dying in the user's devtools. These tests pin
the envelope, the level mapping, the log-injection guard (newline
collapse), the duplicate-fold suffix, the entries cap, and the server-side
kill switch.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _app(*, major=1):
    from quart import g

    from lib.api_keys import local_admin_context
    from lib.app_factory import create_base_app
    from routes.api_v1.logs import api_v1_logs_bp

    app = create_base_app(__name__, {'TESTING': True})

    @app.before_request
    async def _grant():
        g.auth_ctx = local_admin_context()
        g.rate_decision = None

    if major == 4:
        from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
        app.register_blueprint(attach_v4_envelope(
            copy_blueprint_to_v4(api_v1_logs_bp, name='relay_v4_test')))
    else:
        app.register_blueprint(api_v1_logs_bp)
    return app


class _ListHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self._sink = sink

    def emit(self, record):
        self._sink.append(record)


@pytest.fixture
def frontend_log_sink():
    records = []
    fe = logging.getLogger('frontend')
    handler = _ListHandler(records)
    old_level = fe.level
    fe.setLevel(logging.INFO)
    fe.addHandler(handler)
    try:
        yield records
    finally:
        fe.removeHandler(handler)
        fe.setLevel(old_level)


def _post(app, payload, *, major=1):
    async def go():
        r = await app.test_client().post(f'/api/v{major}/logs/client', json=payload)
        return r.status_code, await r.get_json()
    return _run(go())


def test_relay_writes_entries_to_the_frontend_logger(frontend_log_sink):
    code, body = _post(_app(), {
        'session': 'abc123',
        'url': 'http://localhost:15000/',
        'entries': [
            {'t': 1, 'lv': 'info', 'msg': '[loadConvMsgs] 📊 Phase2 reconcile conv=x → branch=KEEP_LOCAL'},
            {'t': 2, 'lv': 'warn', 'msg': 'something fishy'},
            {'t': 3, 'lv': 'error', 'msg': 'it broke'},
        ],
    })
    assert code == 200, body
    assert body.get('ok') is True and body.get('relayed') == 3
    msgs = [r.getMessage() for r in frontend_log_sink]
    assert any('[client:abc123]' in m and 'KEEP_LOCAL' in m for m in msgs)
    levels = {r.levelno for r in frontend_log_sink}
    assert logging.INFO in levels and logging.WARNING in levels and logging.ERROR in levels


def test_newlines_are_collapsed_so_one_entry_is_one_log_line(frontend_log_sink):
    code, body = _post(_app(), {
        'session': 's', 'entries': [{'lv': 'info', 'msg': 'line one\nline two\r\nline three'}],
    })
    assert code == 200, body
    assert len(frontend_log_sink) == 1
    msg = frontend_log_sink[0].getMessage()
    assert '\n' not in msg and '\r' not in msg, (
        'a client-supplied line must never forge extra log records')


def test_active_v4_accepts_browser_envelope(frontend_log_sink):
    code, body = _post(_app(major=4), {
        'session': 'browser', 'url': 'http://localhost:15000/',
        'entries': [{'t': 1, 'lv': 'info', 'msg': 'v4 diagnostic'}],
    }, major=4)
    assert code == 200, body
    assert body['data']['relayed'] == 1
    assert len(frontend_log_sink) == 1
    assert 'v4 diagnostic' in frontend_log_sink[0].getMessage()


def test_duplicate_fold_suffix_is_rendered(frontend_log_sink):
    code, _ = _post(_app(), {
        'session': 's', 'entries': [{'lv': 'info', 'msg': 'same line', 'n': 7}],
    })
    assert code == 200
    assert '(×7)' in frontend_log_sink[0].getMessage()


def test_missing_entries_is_a_clean_400():
    code, body = _post(_app(), {'session': 's'})
    assert code == 400
    assert 'entries' in str(body)


@pytest.mark.parametrize('field,value', [
    ('unexpected', True), ('url', {'bad': 'type'}), ('session', 123),
])
def test_invalid_envelope_is_rejected_without_logging(field, value, frontend_log_sink):
    code, body = _post(_app(), {
        'entries': [{'lv': 'info', 'msg': 'must not be relayed'}], field: value,
    })
    assert code == 400, body
    assert frontend_log_sink == []


def test_session_cannot_inject_log_lines_and_page_url_is_not_logged(frontend_log_sink):
    code, body = _post(_app(), {
        'session': 'one\r\ntwo', 'url': 'https://example.test/?token=private',
        'entries': [{'lv': 'info', 'msg': 'visible diagnostic'}],
    })
    assert code == 200, body
    assert len(frontend_log_sink) == 1
    message = frontend_log_sink[0].getMessage()
    assert '\r' not in message and '\n' not in message
    assert 'visible diagnostic' in message
    assert 'private' not in message and 'example.test' not in message


def test_entries_cap_truncates_to_the_newest_200(frontend_log_sink):
    # A burst must not be rejected: the browser ring buffer re-POSTs until it
    # gets a 2xx, so a 400 looped the same oversized batch forever and lost
    # every line.  The documented 200-entry cap keeps the NEWEST entries.
    code, body = _post(_app(), {
        'session': 's',
        'entries': [{'lv': 'info', 'msg': f'line {i}'} for i in range(250)],
    })
    assert code == 200, body
    assert body.get('relayed') == 200
    assert body.get('dropped') == 50
    msgs = [r.getMessage() for r in frontend_log_sink]
    assert any('line 249' in m for m in msgs)
    assert any('line 50' in m for m in msgs)
    assert not any('line 49' in m and 'truncated' not in m for m in msgs)
    assert any('truncated' in m and 'dropped 50' in m for m in msgs)


def test_kill_switch_drops_everything(monkeypatch, frontend_log_sink):
    monkeypatch.setenv('TOFU_CLIENT_LOG_RELAY', '0')
    code, body = _post(_app(), {
        'session': 's', 'entries': [{'lv': 'error', 'msg': 'should not land'}],
    })
    assert code == 200
    assert body.get('disabled') is True and body.get('relayed') == 0
    assert frontend_log_sink == []
