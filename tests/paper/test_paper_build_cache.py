"""Artifact publication, owner boundaries, active leases and cache budgets."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from lib.paper.builds.budget import BuildBudget, build_budget
from lib.paper.builds.cache import LocalBuildCache
from lib.paper.builds.compiler import CompiledBuild, artifact_metadata
from lib.paper.builds.errors import BuildError

pytestmark = pytest.mark.unit


def _build(tmp_path: Path, *, owner=7, document='a' * 64, status='success'):
    build_id = uuid.uuid4().hex
    directory = tmp_path / build_id
    directory.mkdir()
    artifacts = []
    if status == 'success':
        pdf = directory / 'document.pdf'
        pdf.write_bytes(b'%PDF-1.4\nimmutable fixture\n%%EOF\n')
        artifacts.append(artifact_metadata(pdf))
    receipt = {'schema_version': 1, 'owner_user_id': owner, 'document_id': document,
               'build_id': build_id, 'target': 'host', 'source_digest': 'b' * 64,
               'version_verified': True, 'main_file': 'main.tex', 'engine': 'pdflatex',
               'toolchain': 'test-engine', 'status': status, 'started_at': 1,
               'completed_at': 2, 'artifacts': artifacts, 'diagnostics': []}
    return CompiledBuild(receipt, directory)


def _cache(tmp_path, records=4, size=16384):
    return LocalBuildCache(tmp_path / 'cache', BuildBudget(1, 4, size, records))


def test_failed_build_and_obsolete_success_do_not_replace_current(tmp_path):
    cache = _cache(tmp_path)
    good = _build(tmp_path)
    cache.publish(good)
    cache.publish(_build(tmp_path, status='failed'))
    cache.publish(_build(tmp_path), make_current=False)
    assert cache.latest(7, 'a' * 64) == good.receipt


def test_unverified_remote_version_cannot_replace_verified_build(tmp_path):
    cache = _cache(tmp_path)
    good = _build(tmp_path)
    cache.publish(good)
    uncertain = _build(tmp_path)
    uncertain.receipt.update(version_verified=False, source_digest=None, target='overleaf')
    cache.publish(uncertain)
    assert cache.latest(7, 'a' * 64) == good.receipt
    with cache.lease(7, 'a' * 64, uncertain.receipt['build_id']) as preview:
        assert preview.receipt['version_verified'] is False


def test_immutable_publication_is_idempotent_and_refuses_changed_artifact(tmp_path):
    cache = _cache(tmp_path)
    good = _build(tmp_path)
    cache.publish(good)
    assert cache.publish(good) == good.receipt
    changed = CompiledBuild({**good.receipt, 'completed_at': 3}, good.directory)
    with pytest.raises(BuildError, match='immutable'):
        cache.publish(changed)
    tampered = _build(tmp_path)
    (tampered.directory / 'document.pdf').write_bytes(b'changed')
    with pytest.raises(BuildError, match='changed before publication'):
        cache.publish(tampered)


def test_cached_pdf_is_owner_scoped_and_integrity_checked(tmp_path):
    cache = _cache(tmp_path)
    good = _build(tmp_path)
    cache.publish(good)
    with cache.open_artifact(7, 'a' * 64, good.receipt['build_id'], 'document.pdf') as (stream, meta):
        assert stream.read().startswith(b'%PDF-')
        assert meta == good.receipt['artifacts'][0]
    with pytest.raises(BuildError, match='unavailable'):
        with cache.open_artifact(8, 'a' * 64, good.receipt['build_id'], 'document.pdf'):
            pytest.fail('cross-owner read')
    with pytest.raises(BuildError, match='unavailable'):
        with cache.open_artifact(7, 'a' * 64, good.receipt['build_id'], '../../receipt.json'):
            pytest.fail('path escape')
    with cache.lease(7, 'a' * 64, good.receipt['build_id']) as stored:
        (stored.directory / 'document.pdf').write_bytes(b'changed')
    with pytest.raises(BuildError, match='integrity'):
        with cache.open_artifact(7, 'a' * 64, good.receipt['build_id'], 'document.pdf'):
            pytest.fail('tampered read')


def test_leased_reader_survives_pressure_and_admission_refuses_over_budget(tmp_path):
    cache = _cache(tmp_path, records=1)
    first = _build(tmp_path)
    cache.publish(first)
    with cache.lease(7, 'a' * 64, first.receipt['build_id']):
        with pytest.raises(BuildError, match='cache is in use'):
            cache.publish(_build(tmp_path, document='c' * 64))
        assert cache.prune()['records'] == 1
    next_build = _build(tmp_path, document='c' * 64)
    cache.publish(next_build)
    assert cache.latest(7, 'a' * 64) is None
    assert cache.latest(7, 'c' * 64) == next_build.receipt
    assert not (cache.root / '7' / ('a' * 64)).exists()


def test_stage_lease_excludes_active_work_and_reclaims_crash_leftovers(tmp_path):
    cache = _cache(tmp_path)
    build_id = uuid.uuid4().hex
    with cache.stage(7, 'a' * 64, build_id) as stage:
        (stage / 'active').write_text('working')
        cache.prune()
        assert (stage / 'active').exists()
        orphan = stage.with_name(uuid.uuid4().hex + '.work')
        orphan.mkdir()
        (orphan / '.lease').touch()
        cache.prune()
        assert not orphan.exists()
    assert not stage.exists()


def test_reader_reservations_survive_cache_recreation_and_expire_without_a_worker(tmp_path, monkeypatch):
    from lib.paper.builds import viewers
    from lib.paper.builds.contract import limit
    clock = [viewers.time.time()]
    monkeypatch.setattr(viewers.time, 'time', lambda: clock[0])
    cache = _cache(tmp_path, records=1)
    first = _build(tmp_path)
    cache.publish(first)
    document, build = first.receipt['document_id'], first.receipt['build_id']
    identity = uuid.uuid4().hex
    request = {'viewer_id': identity, 'operation': 'retain'}
    assert cache.viewer(7, document, build, request)['retained']
    other_process = LocalBuildCache(cache.root, cache.budget)
    with pytest.raises(BuildError, match='cache is in use'):
        other_process.publish(_build(tmp_path, document='c' * 64))
    clock[0] += 200
    assert other_process.viewer(7, document, build, {**request, 'operation': 'renew'})['retained']
    clock[0] += 100
    with pytest.raises(BuildError, match='cache is in use'):
        cache.publish(_build(tmp_path, document='c' * 64))
    clock[0] += limit('viewer_lease_seconds')
    assert not cache.viewer(7, document, build, {**request, 'operation': 'renew'})['retained']
    replacement = _build(tmp_path, document='c' * 64)
    other_process.publish(replacement)
    assert cache.latest(7, document) is None


def test_reader_growth_preserves_cache_bytes_release_and_owner_scope(tmp_path):
    cache = _cache(tmp_path, records=1)
    first = _build(tmp_path)
    cache.publish(first)
    document, build = first.receipt['document_id'], first.receipt['build_id']
    reserved_bytes = cache.prune()['bytes']
    identities = [uuid.uuid4().hex for _ in range(128)]
    for identity in identities:
        assert cache.viewer(7, document, build, {'viewer_id': identity, 'operation': 'retain'})['retained']
    assert reserved_bytes < cache.prune()['bytes'] <= cache.budget.cache_bytes
    assert len(json.loads((cache._build(7, document, build) / '.viewers.json').read_text())) == 128
    with pytest.raises(BuildError):
        cache.viewer(8, document, build, {'viewer_id': identities[0], 'operation': 'release'})
    for identity in identities:
        release = {'viewer_id': identity, 'operation': 'release'}
        assert not cache.viewer(7, document, build, release)['retained']
        assert not cache.viewer(7, document, build, {**release, 'operation': 'renew'})['retained']
    cache.publish(_build(tmp_path, document='c' * 64))
    assert cache.latest(7, document) is None


def test_reader_reservation_excludes_eviction_in_another_process(tmp_path):
    import subprocess
    import sys
    cache = _cache(tmp_path, records=1)
    first, second = _build(tmp_path), _build(tmp_path, document='c' * 64)
    cache.publish(first)
    identity = uuid.uuid4().hex
    cache.viewer(7, first.receipt['document_id'], first.receipt['build_id'],
                 {'viewer_id': identity, 'operation': 'retain'})
    child = subprocess.run([sys.executable, '-c', '''
import json, sys
from pathlib import Path
from lib.paper.builds.cache import LocalBuildCache
from lib.paper.builds.budget import BuildBudget
from lib.paper.builds.compiler import CompiledBuild
from lib.paper.builds.errors import BuildError
cache = LocalBuildCache(Path(sys.argv[1]), BuildBudget(1, 4, 16384, 1))
try:
    cache.publish(CompiledBuild(json.loads(sys.argv[2]), Path(sys.argv[3])))
except BuildError as exc:
    assert exc.code == 'cache_limit'
else:
    raise AssertionError('the other process evicted an active reader')
''', str(cache.root), json.dumps(second.receipt), str(second.directory)],
        capture_output=True, text=True, timeout=15)
    assert child.returncode == 0, child.stderr
    cache.viewer(7, first.receipt['document_id'], first.receipt['build_id'],
                 {'viewer_id': identity, 'operation': 'release'})
    cache.publish(second)
    assert cache.latest(7, 'c' * 64) == second.receipt


def test_corrupt_reader_metadata_protects_only_until_expiry_and_is_counted(tmp_path):
    import os
    import time
    from lib.paper.builds.contract import limit
    cache = _cache(tmp_path, records=1)
    first = _build(tmp_path)
    cache.publish(first)
    baseline = cache.prune()['bytes']
    path = cache._build(7, first.receipt['document_id'], first.receipt['build_id']) / '.viewers.json'
    path.write_bytes(b'?' * 3072)
    assert cache.prune()['bytes'] == baseline + 3072
    with pytest.raises(BuildError, match='cache is in use'):
        cache.publish(_build(tmp_path, document='c' * 64))
    expired = time.time() - limit('viewer_lease_seconds') - 1
    os.utime(path, (expired, expired))
    cache.publish(_build(tmp_path, document='c' * 64))
    assert cache.latest(7, first.receipt['document_id']) is None


def test_reader_update_failure_preserves_prior_reservation(tmp_path, monkeypatch):
    from lib.paper.builds import viewers
    cache = _cache(tmp_path, records=1)
    first = _build(tmp_path)
    cache.publish(first)
    document, build = first.receipt['document_id'], first.receipt['build_id']
    request = {'viewer_id': uuid.uuid4().hex, 'operation': 'retain'}
    cache.viewer(7, document, build, request)
    original = viewers.os.replace
    def fail_metadata(source, target):
        if Path(target).name == '.viewers.json':
            raise OSError('injected reader metadata failure')
        return original(source, target)
    monkeypatch.setattr(viewers.os, 'replace', fail_metadata)
    with pytest.raises(BuildError, match='Cannot update') as failure:
        cache.viewer(7, document, build, {**request, 'operation': 'renew'})
    assert isinstance(failure.value.__cause__, OSError)
    with pytest.raises(BuildError, match='cache is in use'):
        cache.publish(_build(tmp_path, document='c' * 64))
    assert not list(cache.root.rglob('.viewers.pending'))


def test_budget_overrides_are_finite_and_clamped():
    budget = build_budget({
        'TOFU_PAPER_BUILD_WORKERS': '999999',
        'TOFU_PAPER_BUILD_QUEUE_CAPACITY': '999999',
        'TOFU_PAPER_BUILD_CACHE_MAX_MIB': '999999',
        'TOFU_PAPER_BUILD_CACHE_RECORDS': '999999',
    })
    assert budget == BuildBudget(4, 16, 2048 * 1024 * 1024, 128)


def test_failed_build_under_pressure_preserves_last_good_and_single_slot_can_update(tmp_path):
    cache = _cache(tmp_path, records=1)
    good = _build(tmp_path)
    cache.publish(good)
    with pytest.raises(BuildError, match='cache is in use'):
        cache.publish(_build(tmp_path, status='failed'))
    assert cache.latest(7, 'a' * 64) == good.receipt
    next_build = _build(tmp_path)
    cache.publish(next_build)
    assert cache.latest(7, 'a' * 64) == next_build.receipt
    assert cache.prune()['records'] == 1


def test_pointer_publication_failure_rolls_back_without_deleting_prior_success(tmp_path, monkeypatch):
    cache = _cache(tmp_path, records=1)
    good = _build(tmp_path)
    cache.publish(good)
    from lib.paper.builds import cache as cache_module
    original = cache_module.os.replace

    def fail_pointer(source, destination):
        if Path(destination).name == 'latest.json':
            raise OSError('injected pointer failure')
        return original(source, destination)

    monkeypatch.setattr(cache_module.os, 'replace', fail_pointer)
    with pytest.raises(OSError, match='injected'):
        cache.publish(_build(tmp_path))
    assert cache.latest(7, 'a' * 64) == good.receipt
    assert cache.prune()['records'] == 1
