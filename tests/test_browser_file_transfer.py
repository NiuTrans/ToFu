"""Executable contract for authenticated browser → server file transfer."""

from __future__ import annotations

import contextvars
import hashlib
import os
from pathlib import Path
import re

import pytest

import lib.browser.file_transfer as transfer_mod
from lib.browser.file_transfer import (
    BrowserFileTransferError,
    BrowserFileTransferStore,
    MAX_CHUNK_BYTES,
    MIN_TRANSFER_CHUNK_BYTES,
)
from lib.browser.log_safety import text_for_log, url_for_log

pytestmark = pytest.mark.unit


def test_browser_log_projection_never_persists_bearer_url_parts():
    signed_url = (
        'https://alice:password@[2001:db8::1]:8443/private/path-token.zip'
        '?X-Amz-Credential=credential&X-Amz-Signature=signature#fragment')

    projected = url_for_log(signed_url)
    assert projected == 'https://[2001:db8::1]:8443/…'
    assert all(secret not in projected for secret in (
        'alice', 'password', 'private', 'path-token', 'credential',
        'signature', 'fragment'))

    diagnostic = text_for_log(
        f'GET {signed_url} failed\r\nretry refused', max_chars=120)
    assert diagnostic == (
        'GET https://[2001:db8::1]:8443/… failed retry refused')
    assert '\n' not in diagnostic and '\r' not in diagnostic
    assert url_for_log('https://example.test:invalid/file') == '[invalid-url]'
    assert url_for_log('data:private') == 'data:[redacted]'


@pytest.fixture
def transfer_store(tmp_path, monkeypatch):
    clock = {'now': 1000.0}
    monkeypatch.setattr(
        transfer_mod,
        'fetched_path',
        lambda *parts: str(tmp_path.joinpath(*parts)),
    )
    monkeypatch.setattr(transfer_mod, 'audit_log', lambda *args, **kwargs: None)
    import lib.browser.access as access
    monkeypatch.setattr(access, 'require_access', lambda *args, **kwargs: 'x.test')
    store = BrowserFileTransferStore(
        ttl_seconds=10,
        max_active=3,
        max_per_owner=2,
        clock=lambda: clock['now'],
    )
    yield store, clock, tmp_path
    store.clear_for_tests()


def _create(store, *, owner='41', client='browser-a', max_bytes=1024):
    return store.create(
        owner_user_id=owner,
        client_id=client,
        profile='Work',
        source_url='https://x.test/download?version=latest',
        max_bytes=max_bytes,
    )


def _auth(created, *, owner='41', client='browser-a'):
    return {
        'owner_user_id': owner,
        'client_id': client,
        'token': created['transferToken'],
    }


def _start(store, created, **overrides):
    values = {
        'final_url': 'https://cdn.x.test/releases/citadel.zip',
        'response_status': 200,
        'content_type': 'application/zip',
        'content_disposition': 'attachment; filename="citadel-1.2.3.zip"',
        'content_length': None,
        'suggested_filename': 'citadel-1.2.3.zip',
    }
    values.update(overrides)
    return store.start(created['transferId'], **_auth(created), **values)


def test_transfer_is_bound_to_owner_device_and_one_time_token(transfer_store):
    store, _clock, _tmp_path = transfer_store
    created = _create(store)
    for changed in (
        {'owner_user_id': '42'},
        {'client_id': 'browser-b'},
        {'token': 'not-the-token'},
        {'token': 'é'},
    ):
        caller = _auth(created)
        caller.update(changed)
        with pytest.raises(BrowserFileTransferError) as raised:
            store.start(
                created['transferId'],
                **caller,
                final_url='https://x.test/file.zip',
                response_status=200,
            )
        assert raised.value.status == 403
        assert raised.value.code == 'browser_file_transfer_forbidden'

    canonical = _create(store, owner='0042', client='browser-b')
    store.start(
        canonical['transferId'],
        **_auth(canonical, owner='42', client='browser-b'),
        final_url='https://x.test/file.zip',
        response_status=200,
    )


def test_chunks_are_ordered_bounded_and_idempotent(transfer_store):
    store, _clock, _tmp_path = transfer_store
    created = _create(store, max_bytes=MAX_CHUNK_BYTES + 32)
    _start(store, created)
    payload = b'first chunk'
    digest = hashlib.sha256(payload).hexdigest()
    with pytest.raises(BrowserFileTransferError) as malformed_digest:
        store.append_chunk(
            created['transferId'], 0, payload,
            declared_sha256='é' * 64, **_auth(created),
        )
    assert malformed_digest.value.code == \
        'browser_file_transfer_chunk_digest_mismatch'
    first = store.append_chunk(
        created['transferId'], 0, payload,
        declared_sha256=digest, **_auth(created),
    )
    assert first == {
        'transferId': created['transferId'],
        'acceptedSequence': 0,
        'nextSequence': 1,
        'receivedBytes': len(payload),
        'duplicate': False,
    }
    duplicate = store.append_chunk(
        created['transferId'], 0, payload,
        declared_sha256=digest, **_auth(created),
    )
    assert duplicate['duplicate'] is True
    assert duplicate['receivedBytes'] == len(payload)

    with pytest.raises(BrowserFileTransferError) as out_of_order:
        store.append_chunk(
            created['transferId'], 2, b'skipped one', **_auth(created))
    assert out_of_order.value.code == 'browser_file_transfer_out_of_order'

    with pytest.raises(BrowserFileTransferError) as conflicting_retry:
        store.append_chunk(
            created['transferId'], 0, b'different', **_auth(created))
    assert conflicting_retry.value.code == 'browser_file_transfer_chunk_conflict'

    with pytest.raises(BrowserFileTransferError) as oversized:
        store.append_chunk(
            created['transferId'], 1, b'x' * (MAX_CHUNK_BYTES + 1),
            **_auth(created),
        )
    assert oversized.value.status == 413


def test_chunk_receipt_memory_is_bounded_by_transfer_budget(transfer_store):
    store, _clock, _tmp_path = transfer_store
    created = _create(store, max_bytes=2 * MIN_TRANSFER_CHUNK_BYTES)
    _start(store, created)
    store.append_chunk(created['transferId'], 0, b'a', **_auth(created))
    with pytest.raises(BrowserFileTransferError) as fragmented:
        store.append_chunk(
            created['transferId'], 1, b'b', **_auth(created))
    assert fragmented.value.code == \
        'browser_file_transfer_short_chunk_not_final'
    assert fragmented.value.status == 409

    declared = _create(
        store, owner='42', client='browser-b',
        max_bytes=2 * MIN_TRANSFER_CHUNK_BYTES)
    store.start(
        declared['transferId'],
        **_auth(declared, owner='42', client='browser-b'),
        final_url='https://cdn.x.test/releases/citadel.zip',
        response_status=200,
        content_type='application/zip',
        content_length=2 * MIN_TRANSFER_CHUNK_BYTES,
    )
    with pytest.raises(BrowserFileTransferError) as known_nonfinal:
        store.append_chunk(
            declared['transferId'], 0, b'a',
            **_auth(declared, owner='42', client='browser-b'))
    assert known_nonfinal.value.code == \
        'browser_file_transfer_chunk_too_small'


def test_declared_and_streamed_size_limits_fail_before_commit(transfer_store):
    store, _clock, tmp_path = transfer_store
    partial = _create(store)
    with pytest.raises(BrowserFileTransferError) as partial_response:
        _start(store, partial, response_status=206, content_length=10)
    assert partial_response.value.code == \
        'browser_file_transfer_partial_response'

    declared = _create(store, max_bytes=10)
    with pytest.raises(BrowserFileTransferError) as too_large:
        _start(store, declared, content_length=11)
    assert too_large.value.status == 413
    assert not list(tmp_path.glob('*.zip'))

    streamed = _create(store, owner='42', client='browser-b', max_bytes=10)
    store.start(
        streamed['transferId'],
        **_auth(streamed, owner='42', client='browser-b'),
        final_url='https://x.test/file.bin', response_status=200,
        content_type='application/octet-stream',
    )
    with pytest.raises(BrowserFileTransferError) as over_stream:
        store.append_chunk(
            streamed['transferId'], 0, b'01234567890',
            **_auth(streamed, owner='42', client='browser-b'),
        )
    assert over_stream.value.status == 413

    with pytest.raises(BrowserFileTransferError) as long_url:
        store.create(
            owner_user_id='43', client_id='browser-c', profile='Work',
            source_url='https://x.test/' + ('x' * 9000), max_bytes=10,
        )
    assert long_url.value.code == 'browser_file_transfer_invalid_url'
    for malformed_url in (
            'https://x.test:not-a-port/file',
            'https://x.test/file name.zip'):
        with pytest.raises(BrowserFileTransferError) as malformed:
            store.create(
                owner_user_id='43', client_id='browser-c', profile='Work',
                source_url=malformed_url, max_bytes=10,
            )
        assert malformed.value.code == 'browser_file_transfer_invalid_url'


def test_complete_is_atomic_and_server_receipt_is_authoritative(
        transfer_store, monkeypatch):
    store, _clock, tmp_path = transfer_store
    audit_events = []
    monkeypatch.setattr(
        transfer_mod, 'audit_log',
        lambda event, **fields: audit_events.append((event, fields)),
    )
    created = _create(store)
    _start(store, created, suggested_filename='extension-disagrees.exe')
    payload = b'PK\x03\x04authenticated archive bytes'
    store.append_chunk(created['transferId'], 0, payload, **_auth(created))
    public = store.complete(
        created['transferId'],
        total_bytes=len(payload), chunk_count=1,
        **_auth(created),
    )
    assert public['location'] == 'server_staging'
    assert public['sizeBytes'] == len(payload)
    assert public['sha256'] == hashlib.sha256(payload).hexdigest()
    assert set(public) == {
        'transferId', 'location', 'sizeBytes', 'sha256',
    }, 'URLs, filenames, headers, and server paths stay server-side'
    assert not list(tmp_path.glob('.browser-transfer-*.part'))

    receipt = store.consume_completed(
        created['transferId'], owner_user_id='41', client_id='browser-a')
    assert receipt['location'] == 'server_staging'
    assert not ({'sourceUrl', 'finalUrl', 'contentDisposition'} & set(receipt))
    assert Path(receipt['path']).read_bytes() == payload
    assert Path(receipt['path']).name.startswith(
        f'browser-transfer-{created["transferId"]}-citadel-1.2.3')
    assert Path(receipt['path']).suffix == '.zip'
    assert receipt['isAttachment'] is True
    assert audit_events and 'filename' not in audit_events[0][1]
    os.unlink(receipt['path'])


def test_failed_atomic_commit_remains_hidden_and_retryable(
        transfer_store, monkeypatch):
    store, _clock, tmp_path = transfer_store
    created = _create(store)
    _start(store, created)
    payload = b'commit fault injection'
    store.append_chunk(created['transferId'], 0, payload, **_auth(created))

    real_replace = transfer_mod.os.replace

    def fail_replace(_source, _destination):
        raise OSError('injected atomic rename failure')

    monkeypatch.setattr(transfer_mod.os, 'replace', fail_replace)
    with pytest.raises(BrowserFileTransferError) as failed:
        store.complete(
            created['transferId'], total_bytes=len(payload), chunk_count=1,
            **_auth(created),
        )
    assert failed.value.code == 'browser_file_transfer_storage_error'
    assert list(tmp_path.glob('.browser-transfer-*.part'))
    assert not list(tmp_path.glob('browser-transfer-*'))

    monkeypatch.setattr(transfer_mod.os, 'replace', real_replace)
    store.complete(
        created['transferId'], total_bytes=len(payload), chunk_count=1,
        **_auth(created),
    )
    receipt = store.consume_completed(
        created['transferId'], owner_user_id='41', client_id='browser-a')
    assert Path(receipt['path']).read_bytes() == payload
    os.unlink(receipt['path'])


def test_complete_rechecks_partial_size_before_atomic_visibility(
        transfer_store):
    store, _clock, tmp_path = transfer_store
    created = _create(store)
    _start(store, created)
    payload = b'complete must match the bytes actually on disk'
    store.append_chunk(created['transferId'], 0, payload, **_auth(created))
    part_path = Path(store._transfers[created['transferId']].part_path)
    part_path.write_bytes(payload[:-1])

    with pytest.raises(BrowserFileTransferError) as corrupted:
        store.complete(
            created['transferId'], total_bytes=len(payload), chunk_count=1,
            **_auth(created),
        )
    assert corrupted.value.code == 'browser_file_transfer_storage_error'
    assert not list(tmp_path.glob('browser-transfer-*'))

    part_path.write_bytes(payload)
    store.complete(
        created['transferId'], total_bytes=len(payload), chunk_count=1,
        **_auth(created),
    )
    receipt = store.consume_completed(
        created['transferId'], owner_user_id='41', client_id='browser-a')
    assert Path(receipt['path']).read_bytes() == payload
    os.unlink(receipt['path'])


def test_redirect_and_completion_reenter_read_policy(
        transfer_store, monkeypatch):
    store, _clock, _tmp_path = transfer_store
    calls = []
    denied = {'value': False}

    def policy(_owner, url, **_kwargs):
        calls.append(url)
        if denied['value']:
            raise PermissionError('revoked')
        return 'x.test'

    import lib.browser.access as access
    monkeypatch.setattr(access, 'require_access', policy)
    created = _create(store)
    _start(store, created)
    store.append_chunk(created['transferId'], 0, b'abc', **_auth(created))
    denied['value'] = True
    with pytest.raises(BrowserFileTransferError) as revoked:
        store.complete(
            created['transferId'], total_bytes=3, chunk_count=1,
            **_auth(created),
        )
    assert revoked.value.code == 'browser_file_transfer_redirect_denied'
    assert calls[:2] == [
        'https://x.test/download?version=latest',
        'https://cdn.x.test/releases/citadel.zip',
    ]
    assert calls[-1] == 'https://x.test/download?version=latest'


def test_policy_revoked_after_commit_withholds_and_deletes_receipt(
        transfer_store, monkeypatch):
    store, _clock, tmp_path = transfer_store
    denied = {'value': False}

    def policy(*_args, **_kwargs):
        if denied['value']:
            raise PermissionError('revoked after commit')
        return 'x.test'

    import lib.browser.access as access
    monkeypatch.setattr(access, 'require_access', policy)
    created = _create(store)
    _start(store, created)
    store.append_chunk(created['transferId'], 0, b'abc', **_auth(created))
    store.complete(
        created['transferId'], total_bytes=3, chunk_count=1,
        **_auth(created),
    )
    assert list(tmp_path.glob('browser-transfer-*'))
    denied['value'] = True
    with pytest.raises(BrowserFileTransferError):
        store.consume_completed(
            created['transferId'], owner_user_id='41', client_id='browser-a')
    assert not list(tmp_path.glob('browser-transfer-*'))


def test_expiry_and_abort_reclaim_partial_files(transfer_store):
    store, clock, tmp_path = transfer_store
    expired = _create(store)
    _start(store, expired)
    store.append_chunk(expired['transferId'], 0, b'partial', **_auth(expired))
    assert list(tmp_path.glob('.browser-transfer-*.part'))
    clock['now'] += 11
    assert store.sweep_expired() == 1
    assert not list(tmp_path.glob('.browser-transfer-*.part'))

    aborted = _create(store)
    _start(store, aborted)
    assert store.abort(aborted['transferId'], **_auth(aborted)) is True
    assert not list(tmp_path.glob('.browser-transfer-*.part'))


def test_owner_and_global_capacity_are_explicit(transfer_store):
    store, _clock, _tmp_path = transfer_store
    _create(store, client='a')
    _create(store, client='b')
    with pytest.raises(BrowserFileTransferError) as owner_full:
        _create(store, client='c')
    assert owner_full.value.code == 'browser_file_transfer_owner_capacity'
    _create(store, owner='42', client='c')
    with pytest.raises(BrowserFileTransferError) as global_full:
        _create(store, owner='43', client='d')
    assert global_full.value.code == 'browser_file_transfer_capacity'


def test_browser_staging_has_a_measured_directory_budget(
        transfer_store, monkeypatch):
    store, clock, tmp_path = transfer_store
    monkeypatch.setattr(
        transfer_mod, '_browser_staging_budget_bytes', lambda: 12)
    monkeypatch.setattr(
        transfer_mod, '_browser_staging_ttl_seconds', lambda: 10_000)
    old = tmp_path / 'browser-transfer-old-file.zip'
    old.write_bytes(b'12345678')
    os.utime(old, (clock['now'] - 1000, clock['now'] - 1000))
    orphan = tmp_path / '.browser-transfer-crashed.part'
    orphan.write_bytes(b'xx')
    os.utime(orphan, (clock['now'] - 200, clock['now'] - 200))

    created = _create(store, max_bytes=8)
    assert created['maxBytes'] == 8
    assert not old.exists(), 'oldest reconstructible staging is reclaimed first'
    assert not orphan.exists(), 'crash-orphaned partial files follow transfer TTL'
    with pytest.raises(BrowserFileTransferError) as beyond_budget:
        _create(store, owner='42', client='browser-b', max_bytes=13)
    assert beyond_budget.value.code == 'browser_file_transfer_staging_capacity'


def test_live_disk_reserve_is_checked_at_reservation_and_write(
        transfer_store, monkeypatch):
    store, _clock, _tmp_path = transfer_store
    monkeypatch.setattr(
        transfer_mod, '_live_staging_headroom', lambda _growth: False)
    with pytest.raises(BrowserFileTransferError) as reservation:
        _create(store)
    assert reservation.value.code == 'browser_file_transfer_disk_headroom'
    assert reservation.value.status == 503

    monkeypatch.setattr(
        transfer_mod, '_live_staging_headroom', lambda _growth: True)
    created = _create(store)
    _start(store, created)
    monkeypatch.setattr(
        transfer_mod, '_live_staging_headroom', lambda _growth: False)
    with pytest.raises(BrowserFileTransferError) as write:
        store.append_chunk(created['transferId'], 0, b'x', **_auth(created))
    assert write.value.code == 'browser_file_transfer_disk_headroom'
    assert write.value.status == 507


def test_failed_reclamation_is_still_counted_against_budget(
        transfer_store, monkeypatch):
    store, _clock, tmp_path = transfer_store
    monkeypatch.setattr(
        transfer_mod, '_browser_staging_budget_bytes', lambda: 12)
    old = tmp_path / 'browser-transfer-cannot-delete.zip'
    old.write_bytes(b'12345678')
    os.utime(old, (0, 0))
    real_unlink = transfer_mod.os.unlink

    def guarded_unlink(path):
        if os.path.realpath(path) == os.path.realpath(old):
            raise PermissionError('injected cleanup denial')
        return real_unlink(path)

    monkeypatch.setattr(transfer_mod.os, 'unlink', guarded_unlink)
    with pytest.raises(BrowserFileTransferError) as full:
        _create(store, max_bytes=8)
    assert full.value.code == 'browser_file_transfer_staging_capacity'
    assert old.exists(), 'failed deletion must remain charged to the budget'


def test_fresh_staging_receipt_is_not_evicted_to_admit_new_work(
        transfer_store, monkeypatch):
    store, clock, tmp_path = transfer_store
    monkeypatch.setattr(
        transfer_mod, '_browser_staging_budget_bytes', lambda: 12)
    fresh = tmp_path / 'browser-transfer-fresh.bin'
    fresh.write_bytes(b'12345678')
    os.utime(fresh, (clock['now'], clock['now']))

    with pytest.raises(BrowserFileTransferError) as full:
        _create(store, max_bytes=8)
    assert full.value.code == 'browser_file_transfer_staging_capacity'
    assert fresh.exists(), 'new work must not invalidate a fresh receipt path'


def test_browser_staging_artifact_count_is_explicitly_bounded(
        transfer_store, monkeypatch):
    store, clock, tmp_path = transfer_store
    monkeypatch.setattr(
        transfer_mod, '_browser_staging_budget_bytes', lambda: 1024)
    monkeypatch.setattr(transfer_mod, 'MAX_STAGING_ARTIFACTS', 2)
    first = tmp_path / 'browser-transfer-first.bin'
    second = tmp_path / 'browser-transfer-second.bin'
    first.write_bytes(b'a')
    second.write_bytes(b'b')
    os.utime(first, (clock['now'] - 1000, clock['now'] - 1000))
    os.utime(second, (clock['now'] - 999, clock['now'] - 999))

    _create(store, max_bytes=1)
    assert not first.exists()
    assert second.exists()


def test_fetch_transport_consumes_only_server_completed_receipt(
        transfer_store, monkeypatch):
    store, _clock, _tmp_path = transfer_store
    monkeypatch.setattr(transfer_mod, 'file_transfer_store', store)

    import lib.browser.queue as queue
    import lib.browser.protocol as protocol
    import lib.browser.access as access
    monkeypatch.setattr(
        queue, 'is_extension_connected',
        lambda client_id, *, owner_user_id: (
            client_id == 'browser-a' and owner_user_id == '41'),
    )
    monkeypatch.setattr(
        protocol, 'require_capabilities',
        lambda client_id, required: {
            'client_id': client_id, 'profile': 'Work',
            'capabilities': ['file_export'],
        },
    )
    monkeypatch.setattr(access, 'require_access', lambda *args, **kwargs: 'x.test')

    payload = b'PK\x03\x04file'
    seen = {}

    def send(command, params, *, timeout, client_id, owner_user_id):
        seen.update(command=command, params=params, timeout=timeout,
                    client_id=client_id, owner_user_id=owner_user_id)
        created = {
            'transferId': params['transferId'],
            'transferToken': params['transferToken'],
        }
        _start(store, created, content_length=len(payload))
        store.append_chunk(params['transferId'], 0, payload,
                           **_auth(created))
        receipt = store.complete(
            params['transferId'], total_bytes=len(payload), chunk_count=1,
            **_auth(created),
        )
        return receipt, None

    monkeypatch.setattr(queue, 'send_browser_command', send)
    receipt = transfer_mod.fetch_file_via_browser(
        'https://x.test/download?version=latest',
        max_bytes=1024, timeout=12,
        client_id='browser-a', owner_user_id='41',
    )
    assert seen['command'] == 'fetch_file_to_server'
    assert seen['timeout'] == 35
    assert seen['params']['timeoutMs'] == 30_000
    assert receipt['location'] == 'server_staging'
    assert Path(receipt['path']).read_bytes() == payload
    os.unlink(receipt['path'])


def test_extensionless_file_probe_reuses_the_same_response(
        transfer_store, monkeypatch):
    """fetch_url may discover a file, but must not issue a second target GET."""
    store, _clock, _tmp_path = transfer_store
    monkeypatch.setattr(transfer_mod, 'file_transfer_store', store)
    import lib.browser.access as access
    import lib.browser.fetch as browser_fetch
    import lib.browser.protocol as protocol

    monkeypatch.setattr(access, 'require_access', lambda *args, **kwargs: 'x.test')
    monkeypatch.setattr(
        protocol, 'client_protocol',
        lambda _client_id: {'profile': 'Work', 'capabilities': ['file_export']},
    )
    monkeypatch.setattr(
        protocol, 'require_capabilities',
        lambda _client_id, _required: {'profile': 'Work'},
    )
    monkeypatch.setattr(
        browser_fetch, 'is_extension_connected', lambda *args, **kwargs: True)
    payload = b'PK\x03\x04single signed response'
    commands = []

    def send(command, params, **_route):
        commands.append((command, params))
        wire = params['fileTransfer']
        created = {
            'transferId': wire['transferId'],
            'transferToken': wire['transferToken'],
        }
        _start(store, created, content_length=len(payload))
        store.append_chunk(created['transferId'], 0, payload, **_auth(created))
        return store.complete(
            created['transferId'], total_bytes=len(payload), chunk_count=1,
            **_auth(created),
        ), None

    monkeypatch.setattr(browser_fetch, 'send_browser_command', send)
    url = 'https://x.test/download?version=latest&signature=once'
    handoffs = []

    def remember(source_url, transfer_id):
        handoffs.append((source_url, transfer_id))
        return True

    assert browser_fetch.fetch_url_via_browser(
        url, client_id='browser-a', owner_user_id='41',
        on_file_transfer=remember) is None
    assert len(commands) == 1
    assert commands[0][0] == 'fetch_url'
    assert commands[0][1]['fileTransfer']['transferId']
    assert commands[0][1]['fileTransfer']['timeoutMs'] == 30_000
    assert handoffs == [(url, commands[0][1]['fileTransfer']['transferId'])]
    receipt = store.consume_completed(
        handoffs[0][1], owner_user_id='41', client_id='browser-a')
    assert receipt and Path(receipt['path']).read_bytes() == payload
    os.unlink(receipt['path'])


def test_fetch_without_exact_handoff_never_allocates_file_staging(
        transfer_store, monkeypatch):
    store, _clock, tmp_path = transfer_store
    monkeypatch.setattr(transfer_mod, 'file_transfer_store', store)
    import lib.browser.access as access
    import lib.browser.fetch as browser_fetch
    import lib.browser.protocol as protocol

    monkeypatch.setattr(access, 'require_access', lambda *args, **kwargs: 'x.test')
    monkeypatch.setattr(
        protocol, 'client_protocol',
        lambda _client_id: {'profile': 'Work', 'capabilities': ['file_export']},
    )
    monkeypatch.setattr(
        protocol, 'require_capabilities',
        lambda _client_id, _required: {'profile': 'Work'},
    )
    monkeypatch.setattr(
        browser_fetch, 'is_extension_connected', lambda *args, **kwargs: True)

    commands = []

    def send(command, params, **_route):
        commands.append((command, params))
        return None, 'Resource is a file; exact handoff was not requested'

    monkeypatch.setattr(browser_fetch, 'send_browser_command', send)
    assert browser_fetch.fetch_url_via_browser(
        'https://x.test/download?once',
        client_id='browser-a', owner_user_id='41') is None
    assert commands and 'fileTransfer' not in commands[0][1]
    assert not store._transfers
    assert not list(tmp_path.glob('browser-transfer-*'))


def test_extension_without_file_guard_is_never_sent_fetch_url(monkeypatch):
    import lib.browser.access as access
    import lib.browser.fetch as browser_fetch
    import lib.browser.protocol as protocol

    monkeypatch.setattr(access, 'require_access', lambda *args, **kwargs: 'x.test')
    monkeypatch.setattr(
        protocol, 'client_protocol', lambda _client_id: {'profile': 'Work'})
    monkeypatch.setattr(
        protocol, 'require_capabilities',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError('file_export missing')),
    )
    monkeypatch.setattr(
        browser_fetch, 'is_extension_connected', lambda *args, **kwargs: True)
    commands = []
    monkeypatch.setattr(
        browser_fetch, 'send_browser_command',
        lambda *args, **kwargs: commands.append((args, kwargs)),
    )

    assert browser_fetch.fetch_url_via_browser(
        'https://x.test/maybe-download',
        client_id='old-browser', owner_user_id='41') is None
    assert commands == []
    status = browser_fetch.last_browser_fallback(user_id='41')
    assert 'upgrade required' in status['detail'].lower()


def test_task_handoff_claims_exact_transfer_not_newest_same_url(
        transfer_store, monkeypatch):
    store, clock, _tmp_path = transfer_store
    monkeypatch.setattr(transfer_mod, 'file_transfer_store', store)
    import lib.browser.queue as queue
    import lib.search_bridge as search_bridge

    monkeypatch.setattr(
        queue, 'get_connected_clients',
        lambda *, owner_user_id: [{
            'client_id': 'browser-a', 'profile': 'Work', 'last_poll': 1,
        }] if owner_user_id == '41' else [],
    )
    url = 'https://x.test/download?version=latest'

    first = _create(store)
    _start(store, first)
    store.append_chunk(first['transferId'], 0, b'first', **_auth(first))
    store.complete(
        first['transferId'], total_bytes=5, chunk_count=1, **_auth(first))
    clock['now'] += 1
    second = _create(store)
    _start(store, second)
    store.append_chunk(second['transferId'], 0, b'second', **_auth(second))
    store.complete(
        second['transferId'], total_bytes=6, chunk_count=1, **_auth(second))

    with search_bridge.bind_search_browser(
            user_id='41', client_id='browser-a'):
        first_context = contextvars.copy_context()
        second_context = contextvars.copy_context()
        assert first_context.run(
            search_bridge._remember_bound_browser_file,
            url, first['transferId']) is True
        assert second_context.run(
            search_bridge._remember_bound_browser_file,
            url, second['transferId']) is True
        claimed_first = first_context.run(
            search_bridge.claim_bound_browser_file, url)
        claimed_second = second_context.run(
            search_bridge.claim_bound_browser_file, url)
        assert claimed_first['transferId'] == first['transferId']
        assert claimed_second['transferId'] == second['transferId']
        assert Path(claimed_first['path']).read_bytes() == b'first'
        assert Path(claimed_second['path']).read_bytes() == b'second'
        os.unlink(claimed_first['path'])
        os.unlink(claimed_second['path'])


def test_unclaimed_task_handoff_is_reclaimed_at_binding_exit(
        transfer_store, monkeypatch):
    store, _clock, tmp_path = transfer_store
    monkeypatch.setattr(transfer_mod, 'file_transfer_store', store)
    import lib.browser.queue as queue
    import lib.search_bridge as search_bridge

    monkeypatch.setattr(
        queue, 'get_connected_clients',
        lambda *, owner_user_id: [{
            'client_id': 'browser-a', 'profile': 'Work', 'last_poll': 1,
        }] if owner_user_id == '41' else [],
    )
    created = _create(store)
    _start(store, created)
    store.append_chunk(created['transferId'], 0, b'unclaimed', **_auth(created))
    store.complete(
        created['transferId'], total_bytes=9, chunk_count=1, **_auth(created))
    assert list(tmp_path.glob('browser-transfer-*'))

    with search_bridge.bind_search_browser(
            user_id='41', client_id='browser-a'):
        assert search_bridge._remember_bound_browser_file(
            'https://x.test/download?version=latest',
            created['transferId'],
        ) is True
    assert not list(tmp_path.glob('browser-transfer-*'))
    with pytest.raises(BrowserFileTransferError) as missing:
        store.consume_completed(
            created['transferId'], owner_user_id='41', client_id='browser-a')
    assert missing.value.status == 404


def test_completed_browser_file_stops_legacy_text_fallback_before_second_get(
        transfer_store, monkeypatch):
    store, _clock, tmp_path = transfer_store
    monkeypatch.setattr(transfer_mod, 'file_transfer_store', store)
    import lib.config_dir as config_dir
    import lib.browser.fetch as browser_fetch
    import lib.browser.queue as queue
    import lib.search_bridge as search_bridge
    import lib.tasks_pkg.handlers.search._core as core

    monkeypatch.setattr(
        config_dir, 'fetched_path',
        lambda *parts: str(tmp_path.joinpath(*parts)),
    )

    monkeypatch.setattr(
        queue, 'get_connected_clients',
        lambda *, owner_user_id: [{
            'client_id': 'browser-a', 'profile': 'Work', 'last_poll': 1,
        }] if owner_user_id == '41' else [],
    )
    url = 'https://x.test/download?signature=single-use'
    created = _create(store)
    _start(store, created)
    payload = b'PK\x03\x04only browser response'
    store.append_chunk(created['transferId'], 0, payload, **_auth(created))
    store.complete(
        created['transferId'], total_bytes=len(payload), chunk_count=1,
        **_auth(created))

    def completed_browser_fetch(
            source_url, *, on_file_transfer, **_kwargs):
        assert on_file_transfer(source_url, created['transferId']) is True
        return None

    monkeypatch.setattr(
        browser_fetch, 'fetch_url_via_browser', completed_browser_fetch)
    provider = search_bridge._ChatuiBrowserProvider(
        user_id='41', client_id='browser-a', profile='Work', bound=True)
    monkeypatch.setattr(provider, 'is_connected', lambda: True)
    follow_up_gets = []

    def legacy_text_pipeline(source_url, **_kwargs):
        # This mirrors tofu-search's legacy provider contract: ordinary
        # Exceptions are followed by another network transport.  A completed
        # browser handoff must escape that contract; surface a regression
        # explicitly instead of laundering the subject call in this test.
        try:
            provider.fetch_url(source_url)
        except Exception as error:
            pytest.fail(
                'completed browser handoff was caught as an ordinary '
                f'exception: {error!r}')
        follow_up_gets.append(source_url)
        return None

    monkeypatch.setattr(core, 'fetch_page_content', legacy_text_pipeline)
    monkeypatch.setattr(
        core, 'fetch_url_bytes',
        lambda source_url: follow_up_gets.append(source_url),
    )

    with search_bridge.bind_search_browser(
            user_id='41', client_id='browser-a'):
        result = core._fetch_url_one(url, '', fetch_reason='download')

    assert follow_up_gets == []
    assert result['reason'] == 'asset'
    assert result['transport'] == 'browser_authenticated'
    assert Path(result['saved_path']).read_bytes() == payload
    os.unlink(result['saved_path'])


def test_missing_exact_handoff_fails_closed_without_replay(monkeypatch):
    import lib.search_bridge as search_bridge
    import lib.tasks_pkg.handlers.search._core as core

    url = 'https://x.test/download?signature=already-consumed'
    follow_up_gets = []
    monkeypatch.setattr(
        search_bridge, 'claim_bound_browser_file', lambda _url: None)
    monkeypatch.setattr(
        core, 'fetch_url_bytes',
        lambda source_url: follow_up_gets.append(source_url),
    )

    assert core._stage_binary_asset(
        url, browser_claim_url=url) is None
    assert follow_up_gets == []


def test_file_handoff_signal_crosses_tofu_provider_failure_catch(monkeypatch):
    import lib.browser.fetch as browser_fetch
    import lib.browser.queue as queue
    import lib.search_bridge as search_bridge
    import tofu_search.fetch.http as tofu_http

    monkeypatch.setattr(
        queue, 'get_connected_clients',
        lambda *, owner_user_id: [{
            'client_id': 'browser-a', 'profile': 'Work', 'last_poll': 1,
        }] if owner_user_id == '41' else [],
    )
    url = 'https://x.test/download?signature=single-use'

    def completed_browser_fetch(
            source_url, *, on_file_transfer, **_kwargs):
        assert on_file_transfer(source_url, 'completed-transfer-id') is True
        return None

    monkeypatch.setattr(
        browser_fetch, 'fetch_url_via_browser', completed_browser_fetch)
    provider = search_bridge._ChatuiBrowserProvider(
        user_id='41', client_id='browser-a', profile='Work', bound=True)
    monkeypatch.setattr(provider, 'is_connected', lambda: True)
    monkeypatch.setattr(tofu_http, 'get_browser_provider', lambda: provider)

    with search_bridge.bind_search_browser(
            user_id='41', client_id='browser-a'):
        with search_bridge.browser_file_handoff_boundary():
            with pytest.raises(search_bridge.BrowserFileHandoffReady) as ready:
                tofu_http.try_browser_fetch(url, 50_000)
        assert ready.value.source_url == url
        assert ready.value.transfer_id == 'completed-transfer-id'


def test_text_provider_outside_file_owner_does_not_request_staging(
        monkeypatch):
    import lib.browser.fetch as browser_fetch
    import lib.search_bridge as search_bridge

    calls = []

    def text_browser_fetch(source_url, *, on_file_transfer, **_kwargs):
        calls.append((source_url, on_file_transfer))
        return 'rendered page text'

    monkeypatch.setattr(
        browser_fetch, 'fetch_url_via_browser', text_browser_fetch)
    provider = search_bridge._ChatuiBrowserProvider(
        user_id='41', client_id='browser-a', profile='Work', bound=True)
    monkeypatch.setattr(provider, 'is_connected', lambda: True)

    assert provider.fetch_url('https://x.test/page') == 'rendered page text'
    assert calls == [('https://x.test/page', None)]


def test_login_capture_retry_keeps_file_transfer_on_same_response(
        transfer_store, monkeypatch):
    store, _clock, _tmp_path = transfer_store
    monkeypatch.setattr(transfer_mod, 'file_transfer_store', store)
    import lib.browser.access as access
    import lib.browser.cookie_capture as cookie_capture
    import lib.browser.fetch as browser_fetch
    import lib.browser.protocol as protocol

    monkeypatch.setattr(access, 'require_access', lambda *args, **kwargs: 'x.test')
    monkeypatch.setattr(
        protocol, 'client_protocol',
        lambda _client_id: {'profile': 'Work', 'capabilities': ['file_export']},
    )
    monkeypatch.setattr(
        protocol, 'require_capabilities',
        lambda _client_id, _required: {'profile': 'Work'},
    )
    monkeypatch.setattr(
        browser_fetch, 'is_extension_connected', lambda *args, **kwargs: True)
    monkeypatch.setattr(
        cookie_capture, 'handle_login_wall', lambda *args, **kwargs: True)
    monkeypatch.setattr(
        cookie_capture, 'looks_like_login_wall', lambda *args, **kwargs: True)

    payload = b'PK\x03\x04post-login-once'
    commands = []

    def send(command, params, **_route):
        commands.append((command, params))
        if len(commands) == 1:
            return {
                'url': 'https://login.x.test/sign-in',
                'title': 'Login',
                'text': 'Please login ' * 30,
            }, None
        wire = params['fileTransfer']
        created = {
            'transferId': wire['transferId'],
            'transferToken': wire['transferToken'],
        }
        _start(store, created, content_length=len(payload))
        store.append_chunk(created['transferId'], 0, payload, **_auth(created))
        return store.complete(
            created['transferId'], total_bytes=len(payload), chunk_count=1,
            **_auth(created),
        ), None

    monkeypatch.setattr(browser_fetch, 'send_browser_command', send)
    handoffs = []
    url = 'https://x.test/download?signature=single-use'
    result = browser_fetch.fetch_url_via_browser(
        url,
        client_id='browser-a', owner_user_id='41',
        on_file_transfer=lambda source, transfer_id: (
            handoffs.append((source, transfer_id)) or True),
    )
    assert result is None
    assert [command for command, _params in commands] == [
        'fetch_url', 'fetch_url']
    assert all(params.get('fileTransfer') for _command, params in commands)
    assert commands[0][1]['fileTransfer']['transferId'] != \
        commands[1][1]['fileTransfer']['transferId']
    assert handoffs == [(url, commands[1][1]['fileTransfer']['transferId'])]
    receipt = store.consume_completed(
        handoffs[0][1], owner_user_id='41', client_id='browser-a')
    assert Path(receipt['path']).read_bytes() == payload
    os.unlink(receipt['path'])


def test_search_binary_fallback_surfaces_browser_to_server_receipt(
        monkeypatch):
    import lib.config_dir as config_dir
    import lib.search_bridge as search_bridge
    import lib.tasks_pkg.handlers.search._core as core

    filename = f'browser-transfer-test-{os.getpid()}.zip'
    path = config_dir.fetched_path(filename)
    payload = b'PK\x03\x04browser authenticated'
    Path(path).write_bytes(payload)
    os.chmod(path, 0o600)
    receipt = {
        'location': 'server_staging', 'path': path,
        'contentType': 'application/zip', 'isAttachment': True,
        'sizeBytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
    }
    monkeypatch.setattr(core, 'fetch_url_bytes', lambda _url: None)
    monkeypatch.setattr(
        search_bridge, 'fetch_bound_browser_file',
        lambda _url, **_kwargs: receipt,
    )
    try:
        result = core._stage_binary_asset(
            'https://x.test/download?version=latest')
        assert result['location'] == 'server_staging'
        assert result['transport'] == 'browser_authenticated'
        assert result['saved_path'] == path
        assert 'authenticated browser session into server staging' in \
            result['page_content']
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def test_search_handoff_rejects_same_size_staging_corruption(monkeypatch):
    import lib.config_dir as config_dir
    import lib.search_bridge as search_bridge
    import lib.tasks_pkg.handlers.search._core as core

    filename = f'browser-transfer-corrupt-{os.getpid()}.zip'
    path = config_dir.fetched_path(filename)
    original = b'original browser bytes'
    Path(path).write_bytes(b'X' * len(original))
    os.chmod(path, 0o600)
    receipt = {
        'location': 'server_staging', 'path': path,
        'contentType': 'application/zip', 'isAttachment': True,
        'sizeBytes': len(original),
        'sha256': hashlib.sha256(original).hexdigest(),
    }
    monkeypatch.setattr(core, 'fetch_url_bytes', lambda _url: None)
    monkeypatch.setattr(
        search_bridge, 'fetch_bound_browser_file',
        lambda _url, **_kwargs: receipt,
    )
    result = core._stage_binary_asset(
        'https://x.test/download?version=latest')
    assert result is None
    assert not os.path.exists(path)


def test_extension_contract_never_confuses_device_and_server_locations():
    source = Path('browser_extension/background.js').read_text(encoding='utf-8')
    start = source.index('async function cmdFetchFileToServer')
    end = source.index('\nasync function ', start + 20)
    export_block = source[start:end]
    assert 'chrome.downloads.' not in export_block
    assert '_response: response' in source, (
        'an extensionless attachment probe must hand the same Response stream '
        'to server transfer; a second signed GET is not acceptable')
    probe_block = source[
        source.index('async function _refuseFileResponseBeforeNavigation'):
        source.index('function _transferHeaders')
    ]
    assert 'error.tofuFileProbeDecision = true' in probe_block, (
        'a classified file-transfer error must fail closed instead of '
        'falling through to tab navigation')
    assert 'Could not safely classify response before navigation' in probe_block
    assert 'return !_isTextualResponseType(contentType)' in source
    assert "contentEncoding === 'identity'" in export_block
    assert 'sendPendingChunk' in export_block and 'pendingBytes' in export_block
    assert "location !== 'server_staging'" in export_block
    assert "location: 'device_downloads'" in source
    assert source.index('await _refuseFileResponseBeforeNavigation',
                        source.index('async function cmdFetchUrl')) < \
        source.index("chrome.tabs.create({ url: 'about:blank'",
                     source.index('async function cmdFetchUrl'))

    from lib.browser.protocol import ALL_CAPABILITIES
    assert 'file_export' in ALL_CAPABILITIES
    match = re.search(
        r'const BROWSER_CAPABILITIES\s*=\s*\[(.*?)\]\s*;', source, re.S)
    assert match
    advertised = set(re.findall(r"'([^']+)'", match.group(1)))
    assert advertised == set(ALL_CAPABILITIES)


def test_extension_streams_one_target_response_in_coalesced_chunks():
    from tests._browser_extension_probe import run_extension_probe

    probe = run_extension_probe('fileTransfer')
    assert probe['targetFetches'] == 1
    assert probe['chunkLengths'] == [16 * 1024, 5 * 1024]
    assert probe['chunkSequences'] == [0, 1]
    assert probe['controls'][0]['kind'] == 'start'
    assert probe['controls'][-1] == {
        'kind': 'complete',
        'body': {'totalBytes': 21 * 1024, 'chunkCount': 2},
    }
    assert probe['result']['location'] == 'server_staging'
    assert not any(operation.startswith('tabs.create')
                   for operation in probe['operations'])


def test_extension_diagnostics_strip_url_capabilities():
    from tests._browser_extension_probe import run_extension_probe

    probe = run_extension_probe('diagnosticUrl')
    assert probe == {
        'projected': 'https://files.example.test/…',
        'embedded': 'failed at https://files.example.test/… retry',
        'uppercaseProtected': True,
        'malformed': '[invalid-url]',
    }


def test_extension_failure_cleanup_has_its_own_deadline():
    from tests._browser_extension_probe import run_extension_probe

    probe = run_extension_probe('fileTransferCleanup')
    assert probe['deletes'] == 1
    assert probe['cleanupAborted'] is True
    assert probe['scheduled'] == [30000, 5000]
    assert probe['error'] == 'probe stream failure'


def test_extension_classification_and_stream_share_one_deadline():
    from tests._browser_extension_probe import run_extension_probe

    probe = run_extension_probe('fileTransferDeadline')
    assert probe['scheduled'] == [8000, 24000]
    assert probe['cleared'] == [1, 2]
    assert probe['result']['location'] == 'server_staging'


def test_file_transfer_routes_are_bridge_credential_paths():
    from routes.api_v1.auth import _is_bridge_path

    assert _is_bridge_path(
        '/api/browser/file-transfers/deadbeef/chunks/0') is True
    assert _is_bridge_path('/api/browser/file-transfers/deadbeef') is True
