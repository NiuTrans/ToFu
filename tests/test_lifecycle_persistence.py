#!/usr/bin/env python3
"""Persistence race and corruption guards for lifecycle approval authority."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import lib.lifecycle_approval as la
from lib.json_store import JsonStoreReadError, write_json_atomic


pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(la, '_APPROVALS_FILE', str(tmp_path / 'approvals.json'))
    monkeypatch.setattr(la, '_STATE_FILE', str(tmp_path / 'state.json'))
    return tmp_path


def test_expiry_sweep_cannot_overwrite_concurrent_creation(store, monkeypatch):
    expired_id = 'expired-before-sweep'
    write_json_atomic(la._APPROVALS_FILE, {'records': [{
        'id': expired_id,
        'action': 'restart',
        'status': 'pending',
        'requested_at': 1,
        'expires_at': 2,
    }]})

    original_sweep = la._sweep_expired
    sweep_holds_transaction = threading.Event()
    release_sweep = threading.Event()
    creator_done = threading.Event()
    errors = []

    def _paused_sweep(records, now):
        changed = original_sweep(records, now)
        if threading.current_thread().name == 'expiry-reader':
            sweep_holds_transaction.set()
            if not release_sweep.wait(timeout=10):
                raise RuntimeError('test sweep barrier timed out')
        return changed

    monkeypatch.setattr(la, '_sweep_expired', _paused_sweep)

    def _read():
        try:
            la.get(expired_id)
        except Exception as error:  # pragma: no cover - assertion reports it
            errors.append(error)

    created = {}

    def _create():
        try:
            created.update(la.create_request('shutdown'))
        except Exception as error:  # pragma: no cover - assertion reports it
            errors.append(error)
        finally:
            creator_done.set()

    reader = threading.Thread(target=_read, name='expiry-reader')
    creator = threading.Thread(target=_create, name='approval-creator')
    reader.start()
    assert sweep_holds_transaction.wait(timeout=10)
    creator.start()
    # The creator may be scheduled at any point; the final document assertion
    # is the contract. This small wait makes the old unlocked read/save race
    # deterministic without depending on it in the fixed implementation.
    creator_done.wait(timeout=0.2)
    release_sweep.set()
    reader.join(timeout=10)
    creator.join(timeout=10)

    assert not reader.is_alive() and not creator.is_alive()
    assert errors == []
    document = json.loads(Path(la._APPROVALS_FILE).read_text(encoding='utf-8'))
    by_id = {record['id']: record for record in document['records']}
    assert by_id[expired_id]['status'] == 'expired'
    assert created['id'] in by_id


_ACCEPT_WORKER = r'''
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, sys.argv[1])
import lib.lifecycle_approval as la

la._APPROVALS_FILE = sys.argv[2]
la._STATE_FILE = sys.argv[3]
token, ready_path, start_path, result_path = sys.argv[4:]
Path(ready_path).touch()
deadline = time.monotonic() + 20
while not Path(start_path).exists():
    if time.monotonic() > deadline:
        raise RuntimeError('timed out waiting for acceptance barrier')
    time.sleep(0.01)
result = la.consume_restart(token)
Path(result_path).write_text(json.dumps(result), encoding='utf-8')
'''


def test_two_processes_cannot_accept_two_restart_tokens(store):
    pytest.importorskip('fcntl')
    tokens = []
    for _ in range(2):
        record = la.create_request('restart')
        la.decide(record['id'], True)
        tokens.append(record['id'])

    start = store / 'start'
    processes = []
    ready_paths = []
    result_paths = []
    for index, token in enumerate(tokens):
        ready = store / f'ready-{index}'
        result = store / f'result-{index}.json'
        ready_paths.append(ready)
        result_paths.append(result)
        processes.append(subprocess.Popen(
            [sys.executable, '-c', _ACCEPT_WORKER, ROOT,
             la._APPROVALS_FILE, la._STATE_FILE, token, str(ready),
             str(start), str(result)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ))

    deadline = time.monotonic() + 30
    while not all(path.exists() for path in ready_paths):
        if time.monotonic() > deadline:
            for process in processes:
                process.kill()
            pytest.fail('restart acceptance workers did not reach barrier')
        time.sleep(0.01)
    start.touch()
    outputs = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs

    results = [json.loads(path.read_text(encoding='utf-8'))
               for path in result_paths]
    assert sum(bool(result[0]) for result in results) == 1
    loser = next(result for result in results if not result[0])
    assert loser[1] == 'cooldown'
    assert loser[2] > 0

    document = json.loads(
        Path(la._APPROVALS_FILE).read_text(encoding='utf-8'))
    assert isinstance(document.get('last_restart_at'), (int, float))
    statuses = [record['status'] for record in document['records']
                if record['id'] in tokens]
    assert statuses.count('consumed') == 1
    assert statuses.count('approved') == 1


def test_corrupt_approval_store_fails_closed_without_rewrite(store):
    original = b'{broken lifecycle authority\n'
    Path(la._APPROVALS_FILE).write_bytes(original)

    assert la.get('anything') is None
    with pytest.raises(JsonStoreReadError):
        la.create_request('restart')
    with pytest.raises(JsonStoreReadError):
        la.consume_restart('anything')
    assert Path(la._APPROVALS_FILE).read_bytes() == original


def test_unknown_root_fields_survive_every_mutation(store):
    write_json_atomic(la._APPROVALS_FILE, {
        'records': [],
        'future_schema_field': {'keep': True},
    })

    record = la.create_request('restart')
    la.decide(record['id'], True)
    ok, why, remaining = la.consume_restart(record['id'])

    assert (ok, why, remaining) == (True, '', 0)
    document = json.loads(
        Path(la._APPROVALS_FILE).read_text(encoding='utf-8'))
    assert document['future_schema_field'] == {'keep': True}


def test_delayed_older_stamp_cannot_shorten_newer_cooldown(store):
    la.stamp_restart(now=2000)
    la.stamp_restart(now=1000)

    approval_document = json.loads(
        Path(la._APPROVALS_FILE).read_text(encoding='utf-8'))
    state_document = json.loads(
        Path(la._STATE_FILE).read_text(encoding='utf-8'))
    assert approval_document['last_restart_at'] == 2000
    assert state_document['last_restart_at'] == 2000
