"""Executable contract for the owner-scoped slow-directory grep circuit.

The first live backend timeout remains visible and retains partial-result
semantics. Only equivalent later scans for the same authenticated owner,
target directory, and include glob are skipped; narrower paths, changed globs,
other owners, index hits, and ownerless legacy calls remain independent.
"""

from __future__ import annotations

import os

import pytest

from lib.project_mod import grep_timeout_circuit, read_tools, tools


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_circuit(monkeypatch):
    grep_timeout_circuit._reset_for_tests()
    monkeypatch.delenv('TOFU_GREP_TIMEOUT_COOLDOWN_S', raising=False)
    monkeypatch.delenv('TOFU_GREP_TIMEOUT_CIRCUIT_ENTRIES', raising=False)
    yield
    grep_timeout_circuit._reset_for_tests()


def test_policy_is_owner_path_and_include_scoped(tmp_path):
    root = str(tmp_path)
    child = str(tmp_path / 'child')
    os.mkdir(child)

    grep_timeout_circuit.record_timeout('owner-a', root, None, now=100.0)

    blocked = grep_timeout_circuit.check(
        'owner-a', root, None, now=101.0)
    assert blocked is not None
    assert blocked.remaining_seconds == pytest.approx(299.0)
    assert blocked.should_log
    assert grep_timeout_circuit.check(
        'owner-b', root, None, now=101.0) is None
    assert grep_timeout_circuit.check(
        'owner-a', child, None, now=101.0) is None
    assert grep_timeout_circuit.check(
        'owner-a', root, '*.py', now=101.0) is None
    assert grep_timeout_circuit.check(
        None, root, None, now=101.0) is None


def test_success_and_expiry_rearm_live_scan(tmp_path):
    root = str(tmp_path)
    grep_timeout_circuit.record_timeout(7, root, now=10.0)
    grep_timeout_circuit.record_success(7, root)
    assert grep_timeout_circuit.check(7, root, now=11.0) is None

    grep_timeout_circuit.record_timeout(7, root, now=20.0)
    assert grep_timeout_circuit.check(7, root, now=320.0) is None
    assert grep_timeout_circuit.snapshot(now=320.0)['activeEntries'] == 0


def test_resource_overrides_are_hard_bounded(monkeypatch):
    monkeypatch.setenv('TOFU_GREP_TIMEOUT_COOLDOWN_S', '999999')
    monkeypatch.setenv('TOFU_GREP_TIMEOUT_CIRCUIT_ENTRIES', '999999')
    assert grep_timeout_circuit.cooldown_seconds() == 900.0
    assert grep_timeout_circuit.entry_capacity() == 1024

    monkeypatch.setenv('TOFU_GREP_TIMEOUT_COOLDOWN_S', '0')
    grep_timeout_circuit.record_timeout('owner', '/tmp/example', now=1.0)
    assert grep_timeout_circuit.snapshot(now=1.0)['activeEntries'] == 0


def test_process_registry_evicts_oldest_entry_at_capacity(
        tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_GREP_TIMEOUT_CIRCUIT_ENTRIES', '2')
    roots = [str(tmp_path / name) for name in ('oldest', 'middle', 'newest')]
    for root in roots:
        grep_timeout_circuit.record_timeout('owner', root, now=10.0)

    snapshot = grep_timeout_circuit.snapshot(now=11.0)
    assert snapshot['activeEntries'] == 2
    assert snapshot['entryCapacity'] == 2
    assert grep_timeout_circuit.check(
        'owner', roots[0], now=11.0) is None
    assert grep_timeout_circuit.check(
        'owner', roots[1], now=11.0) is not None


def test_timed_out_live_walk_opens_circuit_without_hiding_escape_routes(
        tmp_path, monkeypatch):
    child = tmp_path / 'child'
    child.mkdir()
    calls = []

    monkeypatch.setattr(read_tools.tree_index, 'acquire', lambda _root: None)
    monkeypatch.setattr(read_tools.tree_index, 'warm', lambda _root: None)
    monkeypatch.setattr(read_tools, '_HAS_RG', True)
    monkeypatch.setattr(
        read_tools,
        '_format_grep_timeout',
        lambda *_args: 'FIRST TIMEOUT',
    )

    def backend(_cmd, _base, _timeout, _max_matches=None):
        calls.append(tuple(_cmd))
        if len(calls) == 1:
            return '', True
        return '', False

    monkeypatch.setattr(read_tools, '_run_grep_subprocess', backend)

    first = read_tools.tool_grep(
        str(tmp_path), 'first', owner_user_id='owner-a')
    assert first == 'FIRST TIMEOUT'
    assert len(calls) == 1

    skipped = read_tools.tool_grep(
        str(tmp_path), 'different-pattern', owner_user_id='owner-a')
    assert 'Grep scan skipped' in skipped
    assert 'No filesystem scan was dispatched' in skipped
    assert len(calls) == 1

    read_tools.tool_grep(
        str(tmp_path), 'narrow', rel_path='child', owner_user_id='owner-a')
    read_tools.tool_grep(
        str(tmp_path), 'glob', include='*.py', owner_user_id='owner-a')
    read_tools.tool_grep(
        str(tmp_path), 'other-owner', owner_user_id='owner-b')
    assert len(calls) == 4


def test_dispatch_threads_authenticated_owner_to_single_and_batch(
        tmp_path, monkeypatch):
    single = []
    batch = []
    monkeypatch.setattr(
        tools,
        'tool_grep',
        lambda *_args, **kwargs: single.append(kwargs) or 'single',
    )
    monkeypatch.setattr(
        tools,
        'tool_grep_batch',
        lambda *_args, **kwargs: batch.append(kwargs) or 'batch',
    )
    kwargs = {'task': {'_userId': 42}}

    assert tools._exec_grep_search(
        {'pattern': 'x'}, str(tmp_path), 'conv', 'task', kwargs) == 'single'
    assert single[0]['owner_user_id'] == 42

    assert tools._exec_grep_search(
        {'searches': [{'pattern': 'x'}]},
        str(tmp_path), 'conv', 'task', kwargs) == 'batch'
    assert batch[0]['owner_user_id'] == 42
