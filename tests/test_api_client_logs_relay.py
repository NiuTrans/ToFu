"""Route tests for POST /api/v1/logs/client — the browser-console relay sink
(epic pt_cfdfd30c8699407b, 2026-08-05).

The frontend relay (static/js/core/client_log_relay.js) batch-POSTs the
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _app():
    from quart import Quart, g

    from lib.api_keys import local_admin_context
    from routes.api_v1.logs import api_v1_logs_bp

    app = Quart(__name__)
    app.config['TESTING'] = True

    @app.before_request
    async def _grant():
        g.auth_ctx = local_admin_context()
        g.rate_decision = None

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


def _post(app, payload):
    async def go():
        r = await app.test_client().post('/api/v1/logs/client', json=payload)
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


def test_entries_cap_rejects_oversized_batches():
    code, _ = _post(_app(), {
        'session': 's',
        'entries': [{'lv': 'info', 'msg': 'x'}] * 201,
    })
    assert code == 400


def test_kill_switch_drops_everything(monkeypatch, frontend_log_sink):
    monkeypatch.setenv('TOFU_CLIENT_LOG_RELAY', '0')
    code, body = _post(_app(), {
        'session': 's', 'entries': [{'lv': 'error', 'msg': 'should not land'}],
    })
    assert code == 200
    assert body.get('disabled') is True and body.get('relayed') == 0
    assert frontend_log_sink == []
