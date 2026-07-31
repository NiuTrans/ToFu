#!/usr/bin/env python3
"""tests/test_chat_created_at_surface.py — the server-authoritative task-start
(``createdAt``) surface used to keep the elapsed timer from restarting at 0.

WHY
---
The frontend elapsed timer (``core/health_stream_timer.js``) seeds its start
from the client-side connect instant, so a page refresh / reconnect to a task
STILL running server-side would restart the displayed elapsed from 0. The
backend already holds the real start (``task['created_at']``, set by
``TaskRuntime.create``). These tests pin that the two reconnect transports —
``chat_poll`` (JSON) and the SSE ``state`` snapshot — both surface it as
``createdAt`` in epoch **milliseconds**, so ``_seedStreamTimerStart`` can rewind
the timer to the true start.

Both tests drive the REAL production code:
  * an in-memory task registered in the shared ``tasks`` registry with a known
    ``created_at`` (a fixed time in the past), and
  * the REAL ``/api/v1/chat/poll/<id>`` handler + REAL ``/api/chat/stream/<id>``
    SSE endpoint (fresh-connection state-snapshot path).

Revert-proofing: a NEUTER test strips the createdAt-emit block from the poll
source and asserts the field then vanishes — proving the test guards the real
emit, not an incidental field.

Run standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/created_at.db \
        python3 tests/test_chat_created_at_surface.py
or via pytest.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/created_at_unittest.db')

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT_PY = os.path.join(REPO, 'routes', 'chat.py')

# A fixed start well in the past so the ms conversion is unambiguous.
_FAKE_CREATED_AT = 1_700_000_000.0
_EXPECTED_MS = int(_FAKE_CREATED_AT * 1000)


def _register_task(task_id, conv_id, *, status='running'):
    """Register a minimal in-memory chat task with a known created_at."""
    from lib.tasks_pkg import tasks, tasks_lock
    task = {
        'id': task_id, 'convId': conv_id, 'status': status,
        'content': 'partial answer so far', 'thinking': '',
        'error': None, 'toolRounds': [],
        'created_at': _FAKE_CREATED_AT,
        'events': [], 'events_lock': __import__('threading').Lock(),
    }
    with tasks_lock:
        tasks[task_id] = task
    return task


def _drop_task(task_id):
    from lib.tasks_pkg import tasks, tasks_lock
    with tasks_lock:
        tasks.pop(task_id, None)


def _load_server_app():
    from lib import auth_mode as _auth_mode
    _auth_mode.reset_for_tests()
    _auth_mode.set_mode('open', set_by='created-at-test')
    spec = importlib.util.spec_from_file_location('server', os.path.join(REPO, 'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    return mod.app


class TestCreatedAtSurface(unittest.TestCase):

    def setUp(self):
        from lib.database import init_db
        init_db()
        self._prev_auth = os.environ.pop('TOFU_AUTH_MODE', None)

    def tearDown(self):
        from lib import auth_mode as _auth_mode
        _auth_mode.reset_for_tests()
        if self._prev_auth is not None:
            os.environ['TOFU_AUTH_MODE'] = self._prev_auth
        else:
            os.environ['TOFU_AUTH_MODE'] = 'private'
        _auth_mode.reset_for_tests()

    def test_chat_poll_carries_created_at_ms(self):
        """The in-memory chat_poll response surfaces createdAt in epoch ms."""
        tid = 'tk-poll-createdat'
        _register_task(tid, 'cv-poll-createdat')
        app = _load_server_app()
        captured = {}

        async def _t():
            async with app.test_client() as client:
                r = await client.get(f'/api/v1/chat/poll/{tid}')
                captured['status'] = r.status_code
                captured['json'] = await r.get_json()

        try:
            asyncio.run(_t())
        finally:
            _drop_task(tid)

        self.assertEqual(captured['status'], 200, captured)
        body = captured['json'] or {}
        self.assertIn('createdAt', body,
                      f'chat_poll response missing createdAt: {body}')
        self.assertEqual(body['createdAt'], _EXPECTED_MS,
                         'createdAt must be task.created_at * 1000 (epoch ms)')

    def test_sse_state_snapshot_carries_created_at_ms(self):
        """The fresh-connection SSE `state` snapshot surfaces createdAt (ms)."""
        tid = 'tk-sse-createdat'
        # 'running' would hold the SSE loop open; register 'done' so the
        # endpoint sends the state snapshot then terminates cleanly, but we
        # keep created_at as the running task's true start.
        _register_task(tid, 'cv-sse-createdat', status='done')
        app = _load_server_app()
        captured = {}

        async def _t():
            async with app.test_client() as client:
                r = await client.get(f'/api/chat/stream/{tid}')
                captured['body'] = (await r.get_data()).decode('utf-8', errors='replace')

        try:
            asyncio.run(_t())
        finally:
            _drop_task(tid)

        state_ev = None
        for line in captured.get('body', '').splitlines():
            line = line.strip()
            if not line.startswith('data:'):
                continue
            payload = line[len('data:'):].strip()
            if not payload:
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if ev.get('type') == 'state':
                state_ev = ev
                break

        self.assertIsNotNone(
            state_ev, f'no state snapshot in SSE body: {captured.get("body","")[:300]!r}')
        self.assertIn('createdAt', state_ev,
                      f'SSE state snapshot missing createdAt: {state_ev}')
        self.assertEqual(state_ev['createdAt'], _EXPECTED_MS,
                         'state.createdAt must be task.created_at * 1000 (epoch ms)')

    def test_neuter_poll_emit_removes_field(self):
        """NEUTER: strip the createdAt-emit block from chat_poll's source and
        prove the field disappears — the test guards the real emit."""
        # The emit moved out of routes/chat.py: the poll site now lives in
        # routes/chat_poll_abort.py and the two SSE state snapshots in
        # lib/chat_dispatch.py. Scan all three files.
        poll_src = open(os.path.join(REPO, 'routes', 'chat_poll_abort.py'),
                        encoding='utf-8').read()
        dispatch_src = open(os.path.join(REPO, 'lib', 'chat_dispatch.py'),
                            encoding='utf-8').read()
        # The in-memory poll emit block, matched verbatim so a real refactor
        # that renames it fails loudly rather than silently passing.
        marker = "        _created = task.get('created_at')\n"
        self.assertIn(marker, poll_src,
                      'expected createdAt emit block not found in '
                      'routes/chat_poll_abort.py (has the surface been renamed?)')
        # Count occurrences: poll + fresh-state + resume-state = 3 sites.
        self.assertGreaterEqual(
            poll_src.count("r['createdAt'] = int(_created * 1000)")
            + dispatch_src.count("state['createdAt'] = int(_created * 1000)")
            + dispatch_src.count("_state['createdAt'] = int(_created * 1000)"), 3,
            'expected createdAt emitted on poll + fresh-state + resume-state paths')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_chat_created_at_surface.__main__', init_schema=False)
    unittest.main(verbosity=2)
