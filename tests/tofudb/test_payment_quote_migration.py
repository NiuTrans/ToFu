"""Offline restore preserves unbound checkout quotes and their independent IDs."""

from pathlib import Path
import subprocess

import pytest

from lib.storage_authority_sql.migration import tofudb_migration_import as importer
from lib.storage_authority_sql.migration.tofudb_migration_target import OfflineMigrationSession
from tests.support.tofudb_binary import ensure_engine_binary

pytestmark = pytest.mark.api


def test_pending_quotes_restore_reopen_and_replay_without_provider_claims(tmp_path):
    binary = ensure_engine_binary(Path(__file__).resolve().parents[2])
    if binary is None:
        pytest.skip('native engine unavailable')
    target = tmp_path / 'authority'
    initialized = subprocess.run([str(binary), 'init', '--data-dir', str(target)],
        check=True, capture_output=True, text=True, timeout=120)
    authority = next(word.split('=', 1)[1] for word in initialized.stdout.split()
                     if word.startswith('authority_uuid='))
    documents = [dict(id=f'pay_{i}', user_id='usr_quote', provider='stripe', provider_id='',
        amount_minor=200, currency='USD', credit_micro=200_000_000, status='pending',
        created_at=100, settled_at=0, raw={}) for i in range(2)]
    for inserted in (True, False):
        with OfflineMigrationSession(binary, target, authority, owner_id=2, tenant_id=1) as session:
            for document in documents:
                result = importer._command(session, 'migration.billing.payment.restore',
                                           {'document': document})
                assert result == {'inserted': inserted}
                assert importer._query(session, 'migration.billing.payment.get',
                                       {'id': document['id']}) == document
            for table in ('payments', 'payment_id_claims', 'payment_created_index'):
                page = importer._query(session, 'migration.billing.payment.scan',
                                       {'table': table, 'cursor': None})
                assert sorted(row['id'] for row in page['rows']) == ['pay_0', 'pay_1']
            claims = importer._query(session, 'migration.billing.payment.scan',
                                     {'table': 'payment_provider_claims', 'cursor': None})
            assert claims['rows'] == []
