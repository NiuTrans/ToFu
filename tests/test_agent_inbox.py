"""tests/test_agent_inbox.py — unit tests for lib.agent_inbox.

Run with::

    python -m pytest tests/test_agent_inbox.py -v
"""

from __future__ import annotations

import threading
import time

import pytest

from lib import agent_inbox


@pytest.fixture(autouse=True)
def _isolate_inbox():
    """Each test gets a fresh global state (clears tombstones too)."""
    agent_inbox.reset_for_test()
    yield
    agent_inbox.reset_for_test()


# ── Basic enqueue / drain ────────────────────────────────────

def test_enqueue_then_drain_returns_item():
    agent_inbox.enqueue('t1', 'hello', agent_id='a1')
    items = agent_inbox.drain('t1')
    assert len(items) == 1
    assert items[0]['value'] == 'hello'
    assert items[0]['agent_id'] == 'a1'
    assert items[0]['priority'] == 'later'


def test_drain_empties_inbox():
    agent_inbox.enqueue('t1', 'one')
    agent_inbox.drain('t1')
    assert agent_inbox.peek('t1') == 0
    assert agent_inbox.drain('t1') == []


def test_peek_does_not_consume():
    agent_inbox.enqueue('t1', 'one')
    agent_inbox.enqueue('t1', 'two')
    assert agent_inbox.peek('t1') == 2
    assert agent_inbox.peek('t1') == 2  # idempotent


def test_has_pending():
    assert not agent_inbox.has_pending('t1')
    agent_inbox.enqueue('t1', 'x')
    assert agent_inbox.has_pending('t1')


def test_has_pending_can_probe_one_mode_without_consuming():
    agent_inbox.enqueue('t1', 'agent result', mode='swarm-update')
    assert not agent_inbox.has_pending('t1', modes=['user-steer'])
    agent_inbox.enqueue('t1', 'operator message', mode='user-steer')
    assert agent_inbox.has_pending('t1', modes=['user-steer'])
    assert agent_inbox.peek('t1') == 2


# ── Per-task isolation ───────────────────────────────────────

def test_drain_is_scoped_per_task():
    agent_inbox.enqueue('t1', 'a')
    agent_inbox.enqueue('t2', 'b')
    items_t1 = agent_inbox.drain('t1')
    assert [it['value'] for it in items_t1] == ['a']
    items_t2 = agent_inbox.drain('t2')
    assert [it['value'] for it in items_t2] == ['b']


def test_clear_drops_unread():
    agent_inbox.enqueue('t1', 'a')
    agent_inbox.enqueue('t1', 'b')
    n = agent_inbox.clear('t1')
    assert n == 2
    assert agent_inbox.peek('t1') == 0


def test_clear_returns_zero_when_empty():
    assert agent_inbox.clear('nonexistent') == 0


# ── consume (de-dup of already-delivered agents) ─────────────

def test_consume_removes_only_named_agents():
    agent_inbox.enqueue('t1', 'a', agent_id='a1')
    agent_inbox.enqueue('t1', 'b', agent_id='a2')
    agent_inbox.enqueue('t1', 'c', agent_id='a3')
    removed = agent_inbox.consume('t1', ['a1', 'a3'])
    assert removed == 2
    items = agent_inbox.drain('t1')
    assert [it['agent_id'] for it in items] == ['a2']


def test_consume_all_deletes_bucket():
    agent_inbox.enqueue('t1', 'a', agent_id='a1')
    agent_inbox.enqueue('t1', 'b', agent_id='a2')
    removed = agent_inbox.consume('t1', ['a1', 'a2'])
    assert removed == 2
    assert agent_inbox.peek('t1') == 0


def test_consume_unknown_agent_is_noop():
    agent_inbox.enqueue('t1', 'a', agent_id='a1')
    assert agent_inbox.consume('t1', ['nope']) == 0
    assert agent_inbox.peek('t1') == 1


def test_consume_empty_args_are_noops():
    agent_inbox.enqueue('t1', 'a', agent_id='a1')
    assert agent_inbox.consume('', ['a1']) == 0
    assert agent_inbox.consume('t1', []) == 0
    assert agent_inbox.peek('t1') == 1


def test_clear_tombstones_block_late_enqueues():
    """After clear(), late-arriving items must NOT recreate the inbox."""
    agent_inbox.enqueue('t1', 'a')
    agent_inbox.clear('t1')
    # Simulate a late sub-agent completion.
    agent_inbox.enqueue('t1', 'late', agent_id='straggler')
    assert agent_inbox.peek('t1') == 0
    assert agent_inbox.drain('t1') == []


# ── Priority ordering ────────────────────────────────────────

def test_priority_sorts_now_first_then_next_then_later():
    agent_inbox.enqueue('t1', 'A', priority='later')
    agent_inbox.enqueue('t1', 'B', priority='next')
    agent_inbox.enqueue('t1', 'C', priority='now')
    items = agent_inbox.drain('t1')
    assert [it['value'] for it in items] == ['C', 'B', 'A']


def test_fifo_within_same_priority():
    agent_inbox.enqueue('t1', 'first',  priority='later')
    agent_inbox.enqueue('t1', 'second', priority='later')
    agent_inbox.enqueue('t1', 'third',  priority='later')
    items = agent_inbox.drain('t1')
    assert [it['value'] for it in items] == ['first', 'second', 'third']


def test_unknown_priority_falls_back_to_later():
    agent_inbox.enqueue('t1', 'x', priority='turbo')  # invalid
    items = agent_inbox.drain('t1')
    assert items[0]['priority'] == 'later'


# ── max_items partial drain ──────────────────────────────────

def test_drain_with_max_items_returns_subset():
    for i in range(5):
        agent_inbox.enqueue('t1', f'msg{i}', priority='later')
    first = agent_inbox.drain('t1', max_items=2)
    assert [it['value'] for it in first] == ['msg0', 'msg1']
    assert agent_inbox.peek('t1') == 3  # remainder still there
    rest = agent_inbox.drain('t1')
    assert [it['value'] for it in rest] == ['msg2', 'msg3', 'msg4']


def test_drain_with_max_items_respects_priority():
    agent_inbox.enqueue('t1', 'low', priority='later')
    agent_inbox.enqueue('t1', 'hi',  priority='now')
    first = agent_inbox.drain('t1', max_items=1)
    assert first[0]['value'] == 'hi'
    second = agent_inbox.drain('t1')
    assert second[0]['value'] == 'low'


# ── Cap enforcement ──────────────────────────────────────────

def test_cap_drops_oldest_later_item():
    cap = agent_inbox.MAX_PER_TASK
    # Fill to cap-1 with 'later', then add one more 'later' → first dropped
    for i in range(cap):
        agent_inbox.enqueue('t1', f'L{i}', priority='later')
    assert agent_inbox.peek('t1') == cap
    agent_inbox.enqueue('t1', 'NEW', priority='later')
    assert agent_inbox.peek('t1') == cap
    items = agent_inbox.drain('t1')
    values = [it['value'] for it in items]
    # L0 dropped, L1..L(cap-1) + NEW remain
    assert 'L0' not in values
    assert values[-1] == 'NEW'


# ── Concurrency — many threads enqueueing ────────────────────

def test_concurrent_enqueue_no_drops_or_dups():
    """Stay under MAX_PER_TASK so we can verify nothing is dropped or duplicated."""
    THREADS = 8
    N = (agent_inbox.MAX_PER_TASK // THREADS) - 1  # safely below cap

    def worker(thread_id: int):
        for i in range(N):
            agent_inbox.enqueue('t1', f't{thread_id}-{i}', priority='later')

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    items = agent_inbox.drain('t1')
    assert len(items) == THREADS * N, f'expected {THREADS * N}, got {len(items)}'
    assert len({it['value'] for it in items}) == THREADS * N, 'duplicates detected'


# ── Empty / invalid task_id ──────────────────────────────────

def test_enqueue_empty_task_id_is_silent_noop():
    agent_inbox.enqueue('', 'x')
    assert agent_inbox.peek('') == 0
    assert agent_inbox.drain('') == []


def test_durable_commit_precedes_inbox_visibility_and_tombstone_skips_commit():
    observed_depths = []

    def commit():
        observed_depths.append(len(agent_inbox._inboxes.get('t1', [])))
        return {'revision': 2}

    accepted, result = agent_inbox.enqueue_after_durable_commit(
        't1', 'steer', commit, priority='next', mode='user-steer')
    assert accepted is True
    assert result == {'revision': 2}
    assert observed_depths == [0]
    assert agent_inbox.peek('t1') == 1

    agent_inbox.clear('closed')
    committed = []
    accepted, result = agent_inbox.enqueue_after_durable_commit(
        'closed', 'late', lambda: committed.append(True), mode='user-steer')
    assert accepted is False
    assert result is None
    assert committed == []


# ── format_swarm_update payload ──────────────────────────────

def test_format_swarm_update_basic_shape():
    payload = agent_inbox.format_swarm_update(
        agent_id='a1', role='researcher', status='completed',
        elapsed_seconds=12.34, tokens=4567,
        preview='Found 3 candidates: foo, bar, baz.',
        output_file='data/swarm/t1/a1.log',
        remaining_running=1, remaining_pending=0,
    )
    assert payload.startswith('<swarm-update>')
    assert payload.endswith('</swarm-update>')
    assert '<agent-id>a1</agent-id>' in payload
    assert '<role>researcher</role>' in payload
    assert '<status>completed</status>' in payload
    assert '<elapsed-seconds>12.3</elapsed-seconds>' in payload
    assert '<tokens>4567</tokens>' in payload
    assert '<output-file>data/swarm/t1/a1.log</output-file>' in payload
    assert 'running="1"' in payload and 'pending="0"' in payload
    assert 'Found 3 candidates' in payload


def test_format_swarm_update_truncates_long_preview():
    long_preview = 'X' * 500
    payload = agent_inbox.format_swarm_update(
        agent_id='a1', role='r', status='completed',
        elapsed_seconds=1.0, tokens=10,
        preview=long_preview,
    )
    # Preview field must be capped near 200 chars (rstrip + ellipsis)
    import re
    m = re.search(r'<preview>(.*)</preview>', payload, re.DOTALL)
    assert m
    inner = m.group(1)
    assert len(inner) <= 201  # 200 + '…'
    assert inner.endswith('…')


def test_format_swarm_update_xml_escapes_special_chars():
    payload = agent_inbox.format_swarm_update(
        agent_id='a&1', role='r<x>', status='completed',
        elapsed_seconds=1.0, tokens=10,
        preview='hello & <world>',
    )
    assert 'a&amp;1' in payload
    assert 'r&lt;x&gt;' in payload
    assert 'hello &amp; &lt;world&gt;' in payload


def test_format_swarm_update_with_error():
    payload = agent_inbox.format_swarm_update(
        agent_id='a1', role='r', status='failed',
        elapsed_seconds=1.0, tokens=10, preview='',
        error='ConnectionError: timed out after 30s',
    )
    assert '<error>ConnectionError' in payload
    assert '<status>failed</status>' in payload


# ── stats() snapshot ─────────────────────────────────────────

def test_stats_reflects_current_depth():
    agent_inbox.enqueue('t1', 'a')
    agent_inbox.enqueue('t1', 'b')
    agent_inbox.enqueue('t2', 'c')
    s = agent_inbox.stats()
    assert s == {'t1': 2, 't2': 1}
    agent_inbox.drain('t1')
    s = agent_inbox.stats()
    assert s == {'t2': 1}
