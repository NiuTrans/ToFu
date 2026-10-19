"""Private receipt restoration is exercised through the ordinary live client."""
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import time
from uuid import uuid4

import pytest

from lib.storage.errors import StorageError
from lib.storage.protocol import canonical_json
from lib.storage.v2_client import StorageV2Client
from lib.storage_authority_sql.receipt_codec import command_receipt_key_v2, encode_receipt_response
from lib.storage_authority_sql.migration.tofudb_migration_target import OfflineMigrationSession
from tofu_db.wire import StorageV2Error

pytestmark = pytest.mark.unit


def _operator(session, operation, payload, command=False):
    response = session.request(operation, payload,
                               command_id=uuid4().hex if command else None,
                               deadline_unix_ms=int(time.time() * 1000) + 30_000)
    response.raise_for_error()
    return response.json()


@pytest.mark.parametrize('legacy_global', [False, True])
@pytest.mark.parametrize('include_owner', [True, False])
@pytest.mark.parametrize('body_size', [10, 12_000, 100_000])
def test_imported_receipt_replays_or_fences_without_executing_again(tmp_path, include_owner, body_size, legacy_global):
    binary = Path(os.environ.get('TOFUDB_DIFFERENTIAL_BIN', 'packages/tofu-db/target/debug/tofu-db')).resolve()
    if not binary.is_file():
        pytest.skip('native tofu-db binary is unavailable')
    target = tmp_path / 'target'
    initialized = subprocess.run([str(binary), 'init', '--data-dir', str(target)],
                                 check=True, capture_output=True, text=True, timeout=60)
    authority = next(value.split('=', 1)[1] for value in initialized.stdout.split()
                     if value.startswith('authority_uuid='))
    operation = 'record.put'
    command_id = 'historical-' + uuid4().hex
    payload = {'namespace': 'receipt-proof', 'key': 'must-stay-absent', 'value': {'new': True}}
    source_payload = {**payload, **({'user_id': 11} if include_owner else {})}
    original_response = {'historical': 'x' * body_size, 'note': '已执行'}
    encoded = encode_receipt_response(original_response)
    snapshot = {
        'tenant_id': 7, 'owner_user_id': 11,
        'command_key': command_receipt_key_v2(command_id).hex(),
        'operation': operation,
        'request_digest': hashlib.sha256(canonical_json({
            'operation': operation, 'payload': source_payload})).hexdigest(),
        'response_hex': encoded.hex(), 'committed_at_ms': 123456,
    }
    prefix = 'migration.legacy.receipt' if legacy_global else 'migration.command.receipt'
    if legacy_global:
        snapshot.pop('owner_user_id')
        snapshot['source_authority_uuid'] = str(uuid4())
    scope_field = 'source_authority_uuid' if legacy_global else 'owner_user_id'
    identity = {field: snapshot[field] for field in ('tenant_id', scope_field, 'command_key')}
    for inserted in (True, False):
        with OfflineMigrationSession(binary, target, authority, owner_id=11, tenant_id=7) as session:
            assert _operator(session, prefix + '.restore', snapshot, True) == {'inserted': inserted}
            assert _operator(session, prefix + '.get', identity) == snapshot
    with OfflineMigrationSession(binary, target, authority, owner_id=12, tenant_id=7) as session:
        if legacy_global:
            # The offline custodian may inspect tenant-global history; this
            # does not authorize an ordinary authenticated foreign-owner replay.
            assert _operator(session, prefix + '.get', identity) == snapshot
            foreign = {**identity, 'source_authority_uuid': str(uuid4())}
            # The raw offline session raises the native StorageV2Error; only the
            # supported StorageV2Client maps it onto the StorageError taxonomy.
            with pytest.raises(StorageV2Error):
                _operator(session, prefix + '.get', foreign)
        else:
            foreign = {**identity, 'owner_user_id': 12}
            assert _operator(session, prefix + '.get', foreign) is None

    secret = ('receipt-proof-' + uuid4().hex).encode()
    process = subprocess.Popen([
        str(binary), 'serve', '--data-dir', str(target), '--owner-id', '11',
        '--tenant-id', '7', '--search-projection-dir', str(tmp_path / 'search'),
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
       text=True, env={**os.environ, 'TOFU_STORAGE_TOKEN': secret.decode()})
    try:
        assert select.select([process.stdout], [], [], 30)[0], 'native readiness timeout'
        ready = json.loads(process.stdout.readline())
        client = StorageV2Client('127.0.0.1', ready['port'], secret, owner_id=11, tenant_id=7)
        if include_owner or not legacy_global:
            assert client.command(operation, payload, command_id) == original_response
        else:
            # A tenant-global receipt lacks an owner proof; the ownerless
            # digest must not authorize an ordinary authenticated replay.
            with pytest.raises(StorageError) as error:
                client.command(operation, payload, command_id)
            assert error.value.code == 'database_conflict'
        with pytest.raises(StorageError) as error:
            client.command(operation, {**payload, 'value': {'different': True}}, command_id)
        assert error.value.code == 'database_conflict'
        assert client.query('record.get', {'namespace': payload['namespace'], 'key': payload['key']}) is None
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
