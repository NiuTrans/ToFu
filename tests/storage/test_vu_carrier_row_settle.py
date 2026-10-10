"""Virtual-user carrier checkpoints always settle to an honest terminal state."""

from __future__ import annotations

import time
import unittest

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


def _carrier_task(task_id, conv_id, *, aborted=False, error=None,
                  status='running', finish_reason='stop'):
    return {
        'id': task_id, 'convId': conv_id, 'status': status,
        'content': 'simulated user reply', 'thinking': 'vu reasoning',
        'error': error, 'aborted': aborted,
        'finishReason': finish_reason, 'usage': None, 'toolRounds': [],
        'config': {'model': 'test-model'},
        '_userId': 1,
        '_vu_subtask': True, '_inline_messages': True,
        '_autopilotParent': 'parent-' + task_id[:4],
    }


def _seed_running_row(task_id, conv_id):
    """Seed the same semantic checkpoint a carrier writes mid-run."""
    from lib.storage import get_storage_client
    now_ms = int(time.time() * 1000)
    value = {
        'task_id': task_id, 'conv_id': conv_id,
        'user_id': 1,
        'content': 'partial', 'thinking': 'partial thinking', 'error': None,
        'status': 'running', 'tool_rounds': None, 'segments': None,
        'metadata': '{"model":"test-model"}',
        'created_at': now_ms, 'completed_at': now_ms,
    }
    get_storage_client(write=True).command(
        'task_results.checkpoint', {
            'key': task_id, 'value': value, 'expected_version': 0,
        }, None)


def _row_status(task_id):
    from lib.storage import get_storage_client
    row = get_storage_client().query(
        'record.get', {'namespace': 'task_results', 'key': task_id})
    value = (row or {}).get('value') or {}
    return value.get('status'), value.get('content'), value.get('thinking')


def _cleanup(*task_ids):
    from lib.storage import get_storage_client
    try:
        for tid in task_ids:
            get_storage_client(write=True).command(
                'record.delete', {'namespace': 'task_results', 'key': tid},
                f'test-delete-carrier:{tid}')
    except Exception:
        pass


class TestCarrierTerminalRow(unittest.TestCase):

    def setUp(self):
        uid = str(id(self))
        self.tid = 'tk-vu-' + uid
        self.conv = 'cv-vu-' + uid
        _cleanup(self.tid)

    def tearDown(self):
        _cleanup(self.tid)

    def test_completed_carrier_row_settles_done(self):
        """The normal path: carrier finished (fr=stop) → row flips to done
        and keeps the carrier's content/thinking (the zombie's 4661-char
        thinking would have been a 'running' row forever before this)."""
        from lib.tasks_pkg.manager import write_carrier_terminal_row

        _seed_running_row(self.tid, self.conv)
        self.assertEqual(_row_status(self.tid)[0], 'running')

        write_carrier_terminal_row(_carrier_task(self.tid, self.conv), 'done')

        status, content, thinking = _row_status(self.tid)
        self.assertEqual(status, 'done',
                         "carrier row stayed non-terminal — the zombie generator")
        self.assertEqual(content, 'simulated user reply')
        self.assertEqual(thinking, 'vu reasoning')

    def test_aborted_carrier_row_settles_aborted(self):
        """real_message_preempts_vu / parent_aborted → 'aborted', not 'done'."""
        from lib.tasks_pkg.manager import write_carrier_terminal_row

        _seed_running_row(self.tid, self.conv)
        write_carrier_terminal_row(
            _carrier_task(self.tid, self.conv, aborted=True, finish_reason=None),
            'aborted')
        self.assertEqual(_row_status(self.tid)[0], 'aborted')

    def test_error_carrier_row_settles_error(self):
        """Died before any finish reason → honest 'error', never a fake 'done'."""
        from lib.tasks_pkg.manager import write_carrier_terminal_row

        _seed_running_row(self.tid, self.conv)
        write_carrier_terminal_row(
            _carrier_task(self.tid, self.conv, status='running', finish_reason=None),
            'error')
        self.assertEqual(_row_status(self.tid)[0], 'error')

    def test_settle_is_idempotent_upsert(self):
        """A repeated settle updates one authoritative record without error."""
        from lib.tasks_pkg.manager import write_carrier_terminal_row
        from lib.storage import get_storage_client

        _seed_running_row(self.tid, self.conv)
        write_carrier_terminal_row(_carrier_task(self.tid, self.conv), 'done')
        write_carrier_terminal_row(_carrier_task(self.tid, self.conv), 'done')
        row = get_storage_client().query(
            'record.get', {'namespace': 'task_results', 'key': self.tid})
        self.assertIsNotNone(row)
        self.assertGreaterEqual(row['version'], 1)
        self.assertEqual(_row_status(self.tid)[0], 'done')
