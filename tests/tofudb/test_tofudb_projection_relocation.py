"""Disposable native proof of projection relocation after authority WAL retirement.

The daemon seeds/checkpoints the same authoritative rows, then both transports
must reconstruct into a new absent leaf with truthful coverage and owner scope.
No production data, live API, manual WAL deletion or index copying is used.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import os
import selectors
import subprocess
import time

import pytest

from lib.storage_authority_sql.projection_paths import (
    prepare_search_projection_directory, resolve_search_projection_paths,
)
from tests.support.tofudb_binary import ensure_engine_binary
from tofu_db.embedded import EmbeddedRuntime
from tofu_db.wire import StorageV2Session

pytestmark = [pytest.mark.unit, pytest.mark.serial]
_SECRET = b'tofu-projection-relocation-test-secret'


def _request(session, operation, payload, *, command_id=None, denied=False):
    deadline = int(time.time() * 1000) + 15_000
    if isinstance(session, EmbeddedRuntime):
        response = session.execute_json(operation, payload, command_id=command_id,
                                        deadline_unix_ms=deadline)
        status, value = response['status'], response['payload']
        error = (response['error_code'], response['error_message'])
    else:
        response = session.request(operation, payload, command_id=command_id,
                                   deadline_unix_ms=deadline)
        status = response.status
        value = response.value() if status == 0 else None
        error = (response.error_code, response.error_message)
    if denied:
        assert status != 0, 'forged owner must not be admitted'
        return None
    assert status == 0, error
    return value


@contextmanager
def _daemon(binary, authority, projection, *, owner=11):
    prepare_search_projection_directory(projection, authority)
    with subprocess.Popen(
        [str(binary), 'serve', '--data-dir', str(authority), '--owner-id', str(owner),
         '--tenant-id', '7', '--search-projection-dir', str(projection)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, 'TOFU_STORAGE_TOKEN': _SECRET.decode('ascii')},
    ) as process:
        session = None
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ)
                assert ready.select(timeout=20), 'Daemon readiness timed out'
            line = process.stdout.readline()
            assert line, 'Daemon exited before readiness'
            readiness = json.loads(line)
            session = StorageV2Session(
                '127.0.0.1', int(readiness['port']), _SECRET,
                owner_id=owner, tenant_id=7, timeout=15.0)
            yield session
        finally:
            if session is not None:
                session.close()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            stderr = process.stderr.read().decode(errors='replace')
            assert process.returncode == 0, stderr[-3000:]
            assert 'authority shutdown checkpoint failed' not in stderr, stderr[-3000:]


@contextmanager
def _open(mode, binary, authority, projection, *, owner=11):
    if mode == 'daemon':
        with _daemon(binary, authority, projection, owner=owner) as session:
            yield session
    else:
        library = Path(os.environ.get('TOFUDB_EMBEDDED_LIB', binary.parent / 'libtofu_db.so'))
        if not library.is_file():
            pytest.skip('matching embedded library unavailable')
        prepare_search_projection_directory(projection, authority)
        with EmbeddedRuntime(library, authority, owner_id=owner, tenant_id=7,
                             projection_dir=projection, secret=_SECRET) as session:
            yield session


def _page(session, owner, query='relocationneedle', *, expected_ids=None):
    deadline = time.monotonic() + 30
    while True:
        result = _request(session, 'conversation.search', {
            'user_id': owner, 'query': query, 'limit': 20, 'page': True,
        })
        if result['coverage'] == 'ready':
            assert not result['incomplete'] and not result['coverage_reasons']
            if expected_ids is None or {item['id'] for item in result['items']} == expected_ids:
                return result
        assert time.monotonic() < deadline, result
        time.sleep(0.05)


def _seed(session, owner):
    _request(session, 'turn.append_settled', {
        'conversation_id': f'body-{owner}', 'user_id': owner,
        'actor': 'human', 'status': 'completed',
        'projection': {'content': f'Authoritative relocationneedle owner {owner}'},
        'lane_id': 'main', 'command_id': f'command-{owner}',
        'turn_id': f'turn-{owner}', 'created_at': 1700000001000, 'now': 1700000001000,
        'conversation_defaults': {'allowCreate': True, 'title': 'Body test',
                                  'createdAt': 1700000000000, 'settings': {}},
    }, command_id=f'command-{owner}')
    _request(session, 'conversation.create', {
        'conv_id': f'title-{owner}', 'user_id': owner,
        'title': f'relocationneedle title owner {owner}',
        'created_at': 1700000000000, 'updated_at': 1700000000000,
    }, command_id=f'title-command-{owner}')
    result = _page(session, owner)
    assert {item['id'] for item in result['items']} == {f'body-{owner}', f'title-{owner}'}
    return result


def _turns(session, owner):
    return _request(session, 'turn.list', {
        'conversation_id': f'body-{owner}', 'user_id': owner, 'lane_id': 'main'})


@pytest.mark.parametrize('mode', ['daemon', 'embedded'])
def test_native_projection_relocation_rebuilds_retired_wal_authority(tmp_path, mode):
    binary = ensure_engine_binary(Path(__file__).resolve().parents[2])
    if binary is None:
        pytest.skip('native engine binary unavailable')
    data_dir = tmp_path / 'data'
    original = resolve_search_projection_paths(data_dir, configured_directory='')
    original.authority_directory.mkdir(parents=True)
    subprocess.run([str(binary), 'init', '--data-dir', str(original.authority_directory)],
                   check=True, capture_output=True)
    expected, turns = {}, {}
    for owner in (11, 22):
        with _daemon(binary, original.authority_directory, original.directory, owner=owner) as session:
            expected[owner] = _seed(session, owner)
            turns[owner] = _turns(session, owner)
    # Graceful native shutdown, not test-side deletion, must retire mutation WAL.
    wals = list(original.authority_directory.glob('active*.wal'))
    assert wals and all(path.stat().st_size == 0 for path in wals), wals
    redirected = resolve_search_projection_paths(data_dir, configured_directory=str(tmp_path / 'local'))
    assert not redirected.directory.exists()
    for owner in (11, 22):
        with _open(mode, binary, redirected.authority_directory, redirected.directory, owner=owner) as session:
            assert _page(session, owner) == expected[owner]
            assert _turns(session, owner) == turns[owner]
            _request(session, 'conversation.search', {
                'user_id': 33, 'query': 'relocationneedle', 'page': True,
            }, denied=True)
    assert original.directory.exists(), 'Relocation must preserve the previous index'
    with _open(mode, binary, redirected.authority_directory, redirected.directory) as session:
        assert _page(session, 11) == expected[11]
        assert _turns(session, 11) == turns[11]


@pytest.mark.parametrize('mode', ['daemon', 'embedded'])
def test_native_projection_return_to_old_directory_after_other_index_drains(tmp_path, mode):
    binary = ensure_engine_binary(Path(__file__).resolve().parents[2])
    if binary is None:
        pytest.skip('native engine binary unavailable')
    original = resolve_search_projection_paths(tmp_path / 'data', configured_directory='')
    authority = original.authority_directory
    authority.mkdir(parents=True)
    subprocess.run([str(binary), 'init', '--data-dir', str(authority)],
                   check=True, capture_output=True)
    untouched = None
    for owner in (11, 22):
        with _open(mode, binary, authority, original.directory, owner=owner) as session:
            result = _seed(session, owner)
            if owner == 22:
                untouched = result
    alternate = resolve_search_projection_paths(
        tmp_path / 'data', configured_directory=str(tmp_path / 'alternate'))
    with _open(mode, binary, authority, alternate.directory) as session:
        assert len(_page(session, 11)['items']) == 2
        _request(session, 'turn.projection.update', {
            'conversation_id': 'body-11', 'user_id': 11, 'turn_id': 'turn-11',
            'expected_projection_revision': 1,
            'projection': {'content': 'replacementneedle edited in projection B'},
        }, command_id='edit-in-b')
        _request(session, 'conversation.delete', {
            'conv_id': 'title-11', 'user_id': 11,
        }, command_id='delete-in-b')

        _request(session, 'conversation.create', {
            'conv_id': 'created-in-b', 'user_id': 11,
            'title': 'replacementneedle new in projection B',
            'created_at': 1700000002000, 'updated_at': 1700000002000,
        }, command_id='create-in-b')
        # READY means B has consumed the shared authority work. There are no
        # remaining invalidations from which an unchanged A could recover.
        assert not _page(session, 11, expected_ids=set())['items']
        edited = _page(session, 11, 'replacementneedle',
                       expected_ids={'body-11', 'created-in-b'})
        turns = _turns(session, 11)
    wals = list(authority.glob('active*.wal'))
    assert wals and all(path.stat().st_size == 0 for path in wals), wals
    assert original.directory.exists(), 'A must not be removed during activation of B'
    # Owner 22 never moved: owner 11 activation must not wipe or reschedule it.
    with _open(mode, binary, authority, original.directory, owner=22) as session:
        assert _page(session, 22) == untouched
    for _ in range(2):
        with _open(mode, binary, authority, original.directory) as session:
            assert not _page(session, 11)['items'], 'old text/deleted title resurfaced from A'
            assert _page(session, 11, 'replacementneedle') == edited
            assert _turns(session, 11) == turns
