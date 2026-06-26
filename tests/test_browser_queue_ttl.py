"""Tests for browser command-queue TTL alignment + cancellation.

Covers the 2026-06 fix that stopped the queue from delivering "zombie"
commands long after the caller timed out:

  * delivery cutoff is the CALLER's own timeout (not a magic 60s);
  * a command the caller gave up on is marked ``cancelled`` and never
    handed to the extension;
  * ``_cleanup_stale`` evicts at ``timeout + _STALE_GRACE`` (not 90s).

These exercise the queue module directly (it speaks ``threading.Event``),
so no real extension / Flask app is needed.
"""

import threading
import time

import pytest

from lib.browser import queue as q


@pytest.fixture(autouse=True)
def _clean_queue():
    """Reset module-global queue state around each test."""
    with q._commands_lock:
        q._commands.clear()
    yield
    with q._commands_lock:
        q._commands.clear()


def _make_cmd(cmd_id, *, created_at, timeout, picked_up=False, cancelled=False,
              target_client=None):
    """Insert a synthetic command into the queue and return it."""
    cmd = {
        'id': cmd_id,
        'type': 'list_tabs',
        'params': {},
        'event': threading.Event(),
        'result': None,
        'error': None,
        'created_at': created_at,
        'picked_up': picked_up,
        'target_client': target_client,
        'timeout': timeout,
        'cancelled': cancelled,
    }
    with q._commands_lock:
        q._commands[cmd_id] = cmd
    return cmd


@pytest.mark.unit
class TestDeliveryCutoff:
    def test_fresh_command_is_delivered(self):
        _make_cmd('c1', created_at=time.time(), timeout=30)
        pending = q.get_pending_commands()
        assert [c['id'] for c in pending] == ['c1']

    def test_command_past_caller_timeout_not_delivered(self):
        # Created 31s ago with a 30s budget → caller has given up → never deliver.
        _make_cmd('old', created_at=time.time() - 31, timeout=30)
        pending = q.get_pending_commands()
        assert pending == []

    def test_short_timeout_cuts_off_before_old_60s_default(self):
        # A 5s-budget command at 6s old must NOT be delivered, even though the
        # old hardcoded cutoff (60s) would still have shipped it.
        _make_cmd('short', created_at=time.time() - 6, timeout=5)
        assert q.get_pending_commands() == []

    def test_long_timeout_still_delivered_past_60s(self):
        # A genuinely long-budget command (e.g. full-page screenshot, 60s) at
        # 45s old is still live and must be delivered — the old 60s magic
        # number happened to work here, but the cutoff now tracks the budget.
        _make_cmd('long', created_at=time.time() - 45, timeout=60)
        assert [c['id'] for c in q.get_pending_commands()] == ['long']

    def test_cancelled_command_not_delivered(self):
        _make_cmd('cx', created_at=time.time(), timeout=30, cancelled=True)
        assert q.get_pending_commands() == []

    def test_delivered_command_is_marked_picked_up(self):
        _make_cmd('p1', created_at=time.time(), timeout=30)
        q.get_pending_commands()
        with q._commands_lock:
            assert q._commands['p1']['picked_up'] is True
        # Second call must not re-deliver it.
        assert q.get_pending_commands() == []


@pytest.mark.unit
class TestCallerTimeoutCancellation:
    def test_timeout_marks_command_cancelled_then_evicts(self):
        # send_browser_command must register a connected client first, so fake
        # one and use a tiny timeout to keep the test fast.
        client = 'client-ttl-test'
        q.mark_poll(client)
        result, error = q.send_browser_command('list_tabs', timeout=0.2,
                                                client_id=client)
        assert result is None
        assert error and 'timed out' in error
        # The command must have been removed from the queue on giveup.
        with q._commands_lock:
            assert all(c['type'] != 'list_tabs' for c in q._commands.values())

    def test_late_poll_after_timeout_skips_cancelled_command(self):
        """A poll that races in just as the caller times out must not run it."""
        client = 'client-race'
        q.mark_poll(client)

        delivered = {}

        def _waiter():
            # 0.3s budget → gives up, marks cancelled, pops the command.
            r, e = q.send_browser_command('list_tabs', timeout=0.3, client_id=client)
            delivered['result'] = (r, e)

        t = threading.Thread(target=_waiter)
        t.start()
        # Let the command get enqueued, then wait until the caller has given up.
        time.sleep(0.5)
        t.join(timeout=2)
        # After giveup the command is gone, so a poll sees nothing to deliver.
        assert q.get_pending_commands(client_id=client) == []
        assert delivered['result'][0] is None


@pytest.mark.unit
class TestCleanupStale:
    def test_cleanup_evicts_after_timeout_plus_grace(self):
        cmd = _make_cmd('s1', created_at=time.time() - (30 + q._STALE_GRACE + 1),
                        timeout=30)
        q._cleanup_stale()
        with q._commands_lock:
            assert 's1' not in q._commands
        # Evicting a never-resolved command sets an error + fires the event so a
        # blocked caller unblocks instead of hanging.
        assert cmd['event'].is_set()
        assert cmd['error']

    def test_cleanup_keeps_command_within_grace(self):
        # 30s budget, 10s old → well within timeout+grace → must survive.
        _make_cmd('s2', created_at=time.time() - 10, timeout=30)
        q._cleanup_stale()
        with q._commands_lock:
            assert 's2' in q._commands
