"""Kernel process evidence retains ownership without trusting procfs absence."""
import uuid

import pytest

from lib.storage import worker_process as owners

pytestmark = pytest.mark.unit


def identity():
    return owners.worker_process_id(uuid.uuid4().hex, legacy_prefix='test')


def test_live_process_pid_reuse_and_foreign_namespace():
    worker = identity()
    assert owners.parse_worker_process(worker) is not None
    assert owners.process_claim_protected(worker)
    fields = worker.split(':')
    fields[-2] = str(int(fields[-2]) + 1)
    assert not owners.process_claim_protected(':'.join(fields))
    fields = worker.split(':')
    fields[2] = str(int(fields[2]) + 1)
    assert owners.process_claim_protected(':'.join(fields))


def test_missing_or_inaccessible_procfs_is_not_process_exit(monkeypatch):
    worker = identity()
    def missing(pid):
        raise FileNotFoundError()
    monkeypatch.setattr(owners, '_process_stat', missing)
    assert owners.process_claim_protected(worker)
    def denied(pid):
        raise PermissionError()
    monkeypatch.setattr(owners.os, 'pidfd_open', denied, raising=False)
    assert owners.process_claim_protected(worker)


def test_legacy_and_malformed_owner_do_not_acquire_process_authority():
    worker = identity()
    for invalid in ('legacy', worker + ':extra', worker.replace('/v1:', '/v2:'),
                    worker.rsplit(':', 1)[0] + ':INVALID'):
        assert owners.parse_worker_process(invalid) is None
        assert not owners.process_claim_protected(invalid)
