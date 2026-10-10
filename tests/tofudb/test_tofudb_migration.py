"""Explicit resumable migration/rollback gate tests (docs/STORAGE.md).

Populates a real legacy SQLite authority through the production Sidecar
semantic operations, migrates it into a real ``tofu-db serve`` authority via
``scripts/tofudb_migrate.py`` (storage.v2 wire protocol), and proves:

* per-domain exact counts and content digests match on both authorities;
* the legacy authority file is byte-identical afterwards (rollback: legacy
  is never mutated, the target directory is disposable);
* a killed mid-migration run resumes from its checkpoint cursor and
  converges to the identical final digests as an uninterrupted run;
* a re-run over an already-migrated target replays zero operations and
  changes nothing.

Skips when no tofu-db binary is resolvable (TOFUDB_DIFFERENTIAL_BIN, then
the checked-in prebuilt targets).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.tofudb_migrate import FAULT_ENV, FAULT_EXIT_CODE  # noqa: E402
from tests.support.tofudb_binary import ensure_engine_binary  # noqa: E402

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SECRET = b'tofudb-migration-test-secret-0001'  # 34 ASCII bytes
_OWNER_ID = 11
_TENANT_ID = 7
_CLI_TIMEOUT_S = 300


def _resolve_tofudb_binary() -> Path:
    binary = ensure_engine_binary(_PROJECT_ROOT)
    if binary is None:
        pytest.skip('no tofu-db binary available (set TOFUDB_DIFFERENTIAL_BIN)')
    return binary


class _Daemon:
    """One running ``tofu-db serve`` authority with a disposable data dir."""

    def __init__(self, binary: Path):
        self.data_dir = Path(tempfile.mkdtemp(prefix='tofudb-mig-authority-'))
        self._projection_parent = Path(
            tempfile.mkdtemp(prefix='tofudb-mig-projection-'))
        self._process: subprocess.Popen | None = None
        self.port = 0
        subprocess.run(
            [str(binary), 'init', '--data-dir', str(self.data_dir)],
            check=True, capture_output=True, text=True, timeout=120,
        )
        env = dict(os.environ)
        env['TOFU_STORAGE_TOKEN'] = _SECRET.decode('ascii')
        self._process = subprocess.Popen(
            [
                str(binary), 'serve',
                '--data-dir', str(self.data_dir),
                '--owner-id', str(_OWNER_ID),
                '--tenant-id', str(_TENANT_ID),
                '--search-projection-dir',
                str(self._projection_parent / 'projection'),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
        )
        try:
            readiness_line = self._process.stdout.readline().strip()
            if not readiness_line:
                raise RuntimeError('tofu-db serve exited before readiness')
            self.port = int(json.loads(readiness_line)['port'])
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self._process is not None:
            if self._process.stdin:
                self._process.stdin.close()  # EOF = supervised shutdown
            try:
                self._process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=10)
            self._process = None
        shutil.rmtree(self.data_dir, ignore_errors=True)
        shutil.rmtree(self._projection_parent, ignore_errors=True)


def _run_migration(
    *,
    legacy_db: Path,
    daemon: _Daemon,
    checkpoint: Path,
    report_path: Path,
    fault_after_ops: int | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if fault_after_ops is not None:
        env[FAULT_ENV] = str(fault_after_ops)
    else:
        env.pop(FAULT_ENV, None)
    return subprocess.run(
        [
            sys.executable, str(_PROJECT_ROOT / 'scripts/tofudb_migrate.py'),
            '--db', str(legacy_db),
            '--port', str(daemon.port),
            '--secret', _SECRET.decode('ascii'),
            '--owner-id', str(_OWNER_ID),
            '--tenant-id', str(_TENANT_ID),
            '--checkpoint', str(checkpoint),
            '--report', str(report_path),
        ],
        cwd=_PROJECT_ROOT, env=env, capture_output=True, text=True,
        timeout=_CLI_TIMEOUT_S, check=False,
    )


def _report(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope='module')
def legacy_authority(tmp_path_factory):
    """One real legacy authority populated via production Sidecar ops."""
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test

    project_root = tmp_path_factory.mktemp('tofudb-mig-legacy')
    runtime = StorageRuntime(
        StorageSupervisor(
            project_root=project_root, backend='sqlite', startup_timeout=60),
        auto_restart=False,
    )
    install_runtime_for_test(runtime)
    runtime.start()
    try:
        from lib.storage import get_storage_client
        client = get_storage_client(write=True)

        def command(operation, payload, command_id):
            return client.command(operation, payload, command_id, deadline=15.0)

        # credential domain: plain, touched, disabled, and revoked rows.
        boundary = {'owner_user_id': _OWNER_ID, 'tenant_id': 'personal'}
        credentials = [
            ('mig-cred-alpha', 'a' * 64, False, False),
            ('mig-cred-beta', 'b' * 64, True, False),
            ('mig-cred-gamma', 'c' * 64, False, True),
        ]
        for index, (credential_id, secret_hash, touch, revoke) in enumerate(
                credentials):
            command('credential.create', {
                **boundary,
                'credential_id': credential_id,
                'account_user_id': '',
                'name': f'Migration key {index}',
                'prefix': 'tf_mig_',
                'secret_hash': secret_hash,
                'scopes': ['read', 'write'],
                'rate_limit_rpm': 60 + index,
                'rate_limit_tpd': 1000 + index,
                'created_at': 100.5 + index,
                'expires_at': 30_000_000_000.0,
                'metadata': {'origin': 'migration-test', 'index': index},
            }, f'mig-fixture-cred-create-{index}')
            if touch:
                command('credential.touch', {
                    **boundary, 'credential_id': credential_id,
                    'used_at': 250.0, 'touch_if_before': 0.0,
                }, 'mig-fixture-cred-touch')
            if revoke:
                command('credential.revoke', {
                    **boundary, 'credential_id': credential_id,
                    'revoked_at': 450.0,
                }, 'mig-fixture-cred-revoke')
        command('credential.update', {
            **boundary, 'credential_id': 'mig-cred-beta',
            'updates': {'disabled': True, 'rate_limit_rpm': 70},
        }, 'mig-fixture-cred-disable')

        # billing domain: two wallets with multi-entry ledger histories.
        command('billing.wallet.apply', {
            'user_id': 'mig-user-a', 'amount_micro': 10000, 'kind': 'topup',
            'ref_type': 'payment', 'ref_id': 'initial', 'note': 'topup',
            'ledger_id': 'mig-ledger-a-topup', 'occurred_at': 100,
            'allow_negative': False,
        }, 'mig-fixture-billing-a-topup')
        command('billing.wallet.apply', {
            'user_id': 'mig-user-a', 'amount_micro': -1500, 'kind': 'reserve',
            'ref_type': 'reserve', 'ref_id': 'task-a', 'note': 'reserve',
            'ledger_id': 'mig-ledger-a-reserve', 'occurred_at': 102,
            'allow_negative': False,
        }, 'mig-fixture-billing-a-reserve')
        command('billing.wallet.apply', {
            'user_id': 'mig-user-a', 'amount_micro': 25, 'kind': 'bonus',
            'ref_type': '', 'ref_id': '', 'note': 'audit',
            'ledger_id': 'mig-ledger-a-bonus', 'occurred_at': 104,
            'allow_negative': False,
        }, 'mig-fixture-billing-a-bonus')
        command('billing.wallet.apply', {
            'user_id': 'mig-user-b', 'amount_micro': 500, 'kind': 'redeem',
            'ref_type': 'code', 'ref_id': 'welcome', 'note': '',
            'ledger_id': 'mig-ledger-b-redeem', 'occurred_at': 100,
            'allow_negative': False,
        }, 'mig-fixture-billing-b-redeem')

        # record domain: values with distinct version ladder heights.
        namespace = 'migtest'
        for key, puts in (('alpha', 1), ('beta', 2), ('gamma', 3)):
            for version in range(puts):
                command('record.put', {
                    'namespace': namespace,
                    'key': key,
                    'value': {
                        'key': key, 'version': version + 1,
                        'nested': {'list': [1, True, None], 'text': 'x' * 64},
                    },
                }, f'mig-fixture-record-{key}-{version}')
    finally:
        runtime.stop()
        install_runtime_for_test(None)

    databases = sorted(project_root.rglob('*.db'))
    assert len(databases) == 1, f'expected one legacy authority: {databases}'
    return databases[0]


@pytest.fixture(scope='module')
def reference_migration(legacy_authority, tmp_path_factory):
    """Uninterrupted reference migration + report (digest oracle)."""
    binary = _resolve_tofudb_binary()
    daemon = _Daemon(binary)
    workdir = tmp_path_factory.mktemp('tofudb-mig-reference')
    checkpoint = workdir / 'checkpoint.json'
    report_path = workdir / 'report.json'
    try:
        completed = _run_migration(
            legacy_db=legacy_authority, daemon=daemon,
            checkpoint=checkpoint, report_path=report_path,
        )
        assert completed.returncode == 0, (
            completed.stdout + completed.stderr)
        yield {
            'daemon': daemon,
            'checkpoint': checkpoint,
            'report_path': report_path,
            'report': _report(report_path),
        }
    finally:
        daemon.stop()


def _assert_clean_report(report: dict) -> None:
    assert report['ok'] is True
    assert set(report['domains']) == {'credential', 'billing', 'record'}
    for name, domain in report['domains'].items():
        assert domain['legacy_count'] > 0, name
        assert domain['legacy_count'] == domain['target_count'], name
        assert domain['legacy_sha256'] == domain['target_sha256'], name
        assert domain['match'] is True, name


def test_migration_end_to_end_digests_and_legacy_immutability(
        legacy_authority, reference_migration):
    """Legacy read-only bytes are untouched; every domain digest matches."""
    del reference_migration  # module fixture already migrated + verified
    before = _file_sha256(legacy_authority)

    binary = _resolve_tofudb_binary()
    daemon = _Daemon(binary)
    workdir = Path(tempfile.mkdtemp(prefix='tofudb-mig-e2e-'))
    try:
        completed = _run_migration(
            legacy_db=legacy_authority, daemon=daemon,
            checkpoint=workdir / 'checkpoint.json',
            report_path=workdir / 'report.json',
        )
        assert completed.returncode == 0, (
            completed.stdout + completed.stderr)
        report = _report(workdir / 'report.json')
        _assert_clean_report(report)
        # The disposable target holds real replayed state.
        assert any(daemon.data_dir.iterdir())
    finally:
        daemon.stop()
        shutil.rmtree(workdir, ignore_errors=True)
    # Rollback guarantee: the legacy authority was never mutated.
    assert _file_sha256(legacy_authority) == before


def test_migration_rerun_is_idempotent(legacy_authority,
                                       reference_migration):
    """A second run over a migrated target replays zero operations."""
    daemon = reference_migration['daemon']
    workdir = Path(tempfile.mkdtemp(prefix='tofudb-mig-rerun-'))
    report_path = workdir / 'report.json'
    try:
        completed = _run_migration(
            legacy_db=legacy_authority, daemon=daemon,
            checkpoint=reference_migration['checkpoint'],
            report_path=report_path,
        )
        assert completed.returncode == 0, (
            completed.stdout + completed.stderr)
        rerun = _report(report_path)
        _assert_clean_report(rerun)
        for name, domain in rerun['domains'].items():
            assert domain['resumed_from_checkpoint'] is True, name
            assert domain['replayed_ops'] == 0, name
            reference_domain = reference_migration['report']['domains'][name]
            assert domain['target_sha256'] == reference_domain['target_sha256']
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_migration_resume_after_fault_converges(legacy_authority,
                                                reference_migration,
                                                tmp_path):
    """Kill mid-migration via the bounded fault hook; resume converges."""
    binary = _resolve_tofudb_binary()
    daemon = _Daemon(binary)
    checkpoint = tmp_path / 'checkpoint.json'
    try:
        interrupted = _run_migration(
            legacy_db=legacy_authority, daemon=daemon,
            checkpoint=checkpoint, report_path=tmp_path / 'interrupted.json',
            # Lands after the credential domain committed its checkpoint,
            # mid-way through billing replay.
            fault_after_ops=7,
        )
        assert interrupted.returncode == FAULT_EXIT_CODE, (
            interrupted.stdout + interrupted.stderr)
        assert 'injected fault' in interrupted.stderr
        assert checkpoint.is_file()  # durable cursor survived the kill

        resumed = _run_migration(
            legacy_db=legacy_authority, daemon=daemon,
            checkpoint=checkpoint, report_path=tmp_path / 'resumed.json',
        )
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        report = _report(tmp_path / 'resumed.json')
        _assert_clean_report(report)
        # Convergence: identical final digests to the uninterrupted oracle.
        for name, domain in report['domains'].items():
            reference_domain = reference_migration['report']['domains'][name]
            assert domain['target_sha256'] == reference_domain[
                'target_sha256'], name
    finally:
        daemon.stop()
