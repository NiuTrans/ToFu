"""Disposable lifecycle adapters for the shared active-conversation workload.

Reuses operator benchmark engines and production storage ports. Never installs
the process-wide storage runtime or attaches to the configured live authority.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import os
import subprocess
import time
from unittest.mock import patch

from benchmarks.conversation_acceptance import POLICY

from scripts.tofudb_perf_compare import LegacyEngine, TofuDbEngine
from scripts.tofudb_bench import _mount_type


class ConversationAuthority:
    def __init__(self, backend: str, binary: Path | None, directory: Path, native_client: str = "application"):
        self.backend = backend
        self.native_client = native_client
        self._embedded_client = None

        self.binary = binary
        self.declared_artifact = None
        self.artifact_before = None
        self.artifact_after = None
        self.engine = (TofuDbEngine(binary, authority_dir_parent=directory, client_mode=native_client)
                       if backend == 'tofudb' else LegacyEngine('sqlite', authority_dir_parent=directory))

    @property
    def _client(self):
        return self._embedded_client or self.engine._client

    @property
    def _process(self):
        if self.backend == 'tofudb':
            return self.engine._process
        runtime = self.engine._runtime
        return runtime._supervisor._process if runtime else None

    @property
    def log_path(self):
        return getattr(self.engine, 'log_path', None)

    def start(self):
        if self.backend == 'tofudb':
            from benchmarks.conversation_provenance import capture_declared_artifact
            self.declared_artifact = capture_declared_artifact(
                self.binary, embedded=self.native_client == 'embedded')
            self.engine._expected_sha256 = self.declared_artifact['binary_sha256']
        with patch.dict(os.environ, {'TOFU_STORAGE_RPC_DEADLINE_S': str(POLICY['rpc_deadline_seconds'])}):
            self.engine.start()
        self.artifact_before = self.artifact_snapshot()
        self._bind_embedded_client()
        self._check_deadline()

    def _bind_embedded_client(self):
        if self.backend == 'tofudb' and self.native_client == 'embedded':
            from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
            from scripts.tofudb_perf_compare import _TEST_SECRET, _OWNER_ID, _TENANT_ID
            self._embedded_client = EmbeddedStorageV2Client(self.engine._embedded, _TEST_SECRET,
                owner_id=_OWNER_ID, tenant_id=_TENANT_ID, timeout=POLICY['rpc_deadline_seconds'])
            readiness = self._embedded_client.health(deadline=30)
            self._embedded_client.set_readiness_budget(readiness.get('resourceBudget'))

    def _check_deadline(self):
        actual = getattr(self._client, '_timeout', None)
        if actual != POLICY['rpc_deadline_seconds']:
            raise RuntimeError(f'benchmark RPC deadline differs: {actual!r} != {POLICY["rpc_deadline_seconds"]}')

    @property
    def pid(self):
        return os.getpid() if self._embedded_client else getattr(self._process, 'pid', None)

    @property
    def alive(self):
        if self._embedded_client:
            return self.engine._embedded is not None and not self.engine._embedded.closed
        return self._process is not None and self._process.poll() is None

    def artifact_snapshot(self):
        """Observe the pinned engine, never the original mutable build path."""
        if self.backend != 'tofudb':
            return None
        from benchmarks.conversation_provenance import capture_artifact
        return capture_artifact(
            self.engine._binary,
            embedded_build_identity=getattr(self.engine, '_embedded_build_identity', None))

    def describe(self):
        path = self.engine._data_dir if self.backend == 'tofudb' else self.engine._project_root
        result = {'backend': self.backend, 'authority_directory': str(path),
                  'volume_device': os.stat(path).st_dev,
                  'filesystem': _mount_type(path), 'durability': 'strict',
                  'rpc_deadline_seconds': self._client._timeout, 'crash_scope': 'sigkill'}
        if self.backend == 'tofudb':
            identity = self.engine._build_identity
            source = self.engine._checkout_source_sha256
            result.update(build_identity=identity, checkout_source_sha256=source,
                          source_matches_checkout=bool(source) and identity.get('source_sha256') == source)
        if self.backend == 'tofudb':
            result['client_mode'] = self.native_client
            result['crash_scope'] = ('in-process-abandon' if self.native_client == 'embedded' else 'sigkill')
            if self.native_client == 'embedded':
                library = self.engine._binary.parent / 'libtofu_db.so'
                result['shared_library_sha256'] = hashlib.sha256(library.read_bytes()).hexdigest()
                result['embedded_build_identity'] = getattr(self.engine, '_embedded_build_identity', None)
        if self.backend == 'sqlite':
            metrics = self._client.metrics(deadline=5)
            result['sqlite_fastpath'] = metrics.get('fastpath', {})
            if result['sqlite_fastpath'].get('active'):
                raise RuntimeError('SQLite relocated its WAL outside the requested comparison volume')
            result['journal'] = 'WAL, synchronous=FULL'
        return result

    def crash_cycle(self):
        with patch.dict(os.environ, {'TOFU_STORAGE_RPC_DEADLINE_S': str(POLICY['rpc_deadline_seconds'])}):
            elapsed = self.engine.crash_cycle()
        self._bind_embedded_client()
        self._check_deadline()
        return elapsed

    def verify(self, timeout):
        if self.backend == 'sqlite':
            # Exact application readback already ran after SIGKILL/reopen.
            started = time.monotonic()
            result = self._client.maintenance('system.integrity_check', deadline=timeout)
            return {'integrity_returncode': 0 if result.get('ok') else 1,
                    'integrity_scope': 'post-crash-reopen-online', 'integrity_summary': result,
                    'verification_phase_ms': {'online_integrity': (time.monotonic() - started) * 1000}}
        started = time.monotonic()
        freeze_phases = self.freeze()
        freeze_ms = (time.monotonic() - started) * 1000
        started = time.monotonic()
        result = subprocess.run([str(self.engine._binary), 'verify', '--data-dir',
            str(self.engine._data_dir)], capture_output=True, text=True, timeout=timeout)
        return {'integrity_returncode': result.returncode, 'integrity_scope': ('offline-after-abandon' if self.native_client == 'embedded' else 'offline-after-sigkill'),
                'integrity_summary': result.stdout[:8192],
                'verification_phase_ms': {'freeze': freeze_ms, 'offline_integrity': (time.monotonic() - started) * 1000},
                'freeze_phase_ms': freeze_phases}

    def freeze(self):
        phases = {}
        started = time.monotonic()
        closer = getattr(self._client, 'close', None)
        if closer:
            closer()
        phases['client_close'] = (time.monotonic() - started) * 1000
        if self.backend == 'tofudb' and self.native_client == 'embedded':
            started = time.monotonic()
            if self.engine._embedded is not None:
                self.engine._embedded.abandon()
                self.engine._embedded = None
            phases['embedded_abandon'] = (time.monotonic() - started) * 1000
            return phases
        started = time.monotonic()
        process = self._process
        if process is not None:
            process.kill()
            process.wait(timeout=30)
        if self.backend == 'tofudb':
            self.engine._process = None
        phases['process_kill_join'] = (time.monotonic() - started) * 1000
        return phases

    def retain_failure(self):
        if self.backend == 'tofudb':
            keep = {self.engine._data_dir, self.engine._projection_dir.parent}
            self.engine._dirs = [p for p in self.engine._dirs if p not in keep]
        else:
            keep = {self.engine._project_root.parent}
            self.engine._project_root = None
        return [str(p) for p in keep]

    def stop(self):
        try:
            self.artifact_after = self.artifact_snapshot()
        except Exception as exc:
            # Preserve cleanup even when the artifact vanished or became unreadable.
            self.artifact_after = {'capture_error': f'{type(exc).__name__}: {exc}'}
        self.engine.stop()
